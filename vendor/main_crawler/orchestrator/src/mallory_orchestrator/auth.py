"""Password + emailed-OTP gate for the dashboard.

The console this protects can stop a crawl, delete the corpus and read every stored page, and it is
reachable by anyone who can open the SSH tunnel. So: two factors, one account, no self-service.

DESIGN NOTES, each of which is a thing that goes wrong without it:

* **Nothing reversible is stored.** The password is a scrypt hash with a per-row salt; the OTP is
  stored as SHA-256 and compared with `compare_digest`; the session token is stored hashed too. A
  copy of `orchestrator.db` yields no password, no live code and no usable session.

* **The OTP goes to the ACCOUNT's addresses, never to an address from the form.** Otherwise the
  second factor is whatever the attacker types, which is not a second factor.

* **The password step returns a challenge id, not a session.** A correct password alone leaves the
  browser holding nothing it can use.

* **Wrong password and unknown user take the same path and the same time.** A login form that
  answers "no such user" faster than "wrong password" enumerates the account list. There is exactly
  one account here, so that leak is small, but the fix is three lines.

* **Auth is OFF unless `DASH_AUTH=1`.** Enabled-but-misconfigured raises at startup instead of
  quietly serving an open dashboard — a security control that fails open is worse than none, because
  the operator believes it is on.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from . import mailer
from .db import SessionLocal, get_db
from .models import LoginChallenge, Session as SessionRow, User

log = logging.getLogger("auth")

COOKIE = "mallory_session"
SESSION_TTL = dt.timedelta(hours=int(os.environ.get("DASH_SESSION_HOURS", "12")))
# Sign out after this long with no OPERATOR activity (polls do not count — see current_user).
# 0 disables it and leaves only the absolute lifetime above.
_IDLE_MIN = int(os.environ.get("DASH_IDLE_MINUTES", "30"))
IDLE_TIMEOUT = dt.timedelta(minutes=_IDLE_MIN) if _IDLE_MIN > 0 else None
OTP_TTL = dt.timedelta(minutes=int(os.environ.get("DASH_OTP_MINUTES", "10")))
OTP_MAX_ATTEMPTS = 5
# Failed PASSWORD attempts before the account is locked, and for how long. Deliberately generous on
# time and tight on count: an operator who fat-fingers twice is fine, a script that tries 500 is not.
PW_MAX_FAILURES = 5
PW_LOCKOUT = dt.timedelta(minutes=15)

# scrypt parameters. n=2**15 costs ~60 ms per verification here — irrelevant for one interactive
# login, and the reason an offline attack on a stolen hash is not worth running.
#
# `maxmem` is NOT a cost knob, it is a ceiling OpenSSL enforces, and it defaults to exactly 32 MiB —
# which is precisely what 128*n*r needs at these settings, so the call fails with "memory limit
# exceeded" without it. It is stored alongside the cost parameters because verification reads them
# back verbatim; a stored set that omits it would hash fine today and fail to verify tomorrow.
KDF = {"n": 2 ** 15, "r": 8, "p": 1, "dklen": 32, "maxmem": 128 * (2 ** 15) * 8 * 2}


def enabled() -> bool:
    return os.environ.get("DASH_AUTH", "").strip() in ("1", "true", "yes", "on")


def _otp_recipients() -> list[str]:
    """Where a code goes. Env-driven so the temporary address can be dropped without a code change
    or a redeploy — which is the whole point, since one of the two is explicitly temporary."""
    raw = os.environ.get("DASH_OTP_EMAILS", "")
    return [a.strip() for a in raw.split(",") if a.strip()]


# ── hashing ───────────────────────────────────────────────────────────────────────────────────

def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **KDF)
    return dk.hex(), salt.hex()


def verify_password(user: User, password: str) -> bool:
    try:
        params = dict(user.kdf_params or {}) or KDF
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(user.password_salt), **params)
    except Exception:  # noqa: BLE001 — a corrupt row must read as "wrong password", not a 500
        return False
    return hmac.compare_digest(dk.hex(), user.password_hash)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


# ── bootstrap ─────────────────────────────────────────────────────────────────────────────────

def ensure_admin() -> None:
    """Create or update the single account from the environment. Called at startup.

    The password lives in the environment, never in this file: a credential in the repo is a
    credential in every clone, every image layer and every `git log`. Re-running this UPDATES the
    stored hash, so rotating the password is an env change plus a restart.
    """
    if not enabled():
        return
    username = os.environ.get("DASH_ADMIN_USER", "").strip()
    password = os.environ.get("DASH_ADMIN_PASSWORD", "")
    emails = _otp_recipients()
    if not username or not password:
        raise RuntimeError(
            "DASH_AUTH=1 but DASH_ADMIN_USER/DASH_ADMIN_PASSWORD are not set — refusing to start "
            "with an unprotected dashboard")
    if not emails:
        raise RuntimeError("DASH_AUTH=1 but DASH_OTP_EMAILS is empty — there is nowhere to send a code")

    with SessionLocal() as db:
        pw_hash, salt = hash_password(password)
        u = db.get(User, username)
        if u is None:
            db.add(User(username=username, email=",".join(emails), password_hash=pw_hash,
                        password_salt=salt, kdf="scrypt", kdf_params=KDF, disabled=False,
                        created_at=_now()))
            log.info("created dashboard account %r", username)
        else:
            u.password_hash, u.password_salt, u.kdf_params = pw_hash, salt, KDF
            u.email = ",".join(emails)
            u.disabled = False
            u.failed_logins, u.locked_until = 0, None
        # One account, no signup — so any OTHER row is a leftover and would be a second way in.
        for row in db.scalars(select(User)).all():
            if row.username != username:
                log.warning("removing stray dashboard account %r", row.username)
                db.delete(row)
        db.commit()


# ── session ───────────────────────────────────────────────────────────────────────────────────

def current_user(request: Request, db: OrmSession) -> str | None:
    """The signed-in user, or None. Enforces BOTH the absolute lifetime and the idle timeout.

    THE IDLE TIMEOUT CANNOT BE DRIVEN BY REQUESTS. This dashboard polls itself every few seconds, so
    "last request" is never stale while the tab is open — an unattended screen would stay signed in
    indefinitely, which is the exact thing an idle timeout exists to prevent. So the poll marks
    itself with `X-Poll: 1` and does NOT refresh the clock; only a request the operator actually
    caused does. A missing header therefore counts as activity, which is the safe default: a new
    client that does not know about the header keeps someone signed in rather than logging them out
    mid-action.
    """
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    row = db.get(SessionRow, _sha(tok))
    if row is None:
        return None
    now = _now()
    if row.expires_at <= now:
        db.delete(row)
        db.commit()
        return None
    if IDLE_TIMEOUT and row.last_seen_at and (now - row.last_seen_at) > IDLE_TIMEOUT:
        log.info("session for %r expired after %s idle", row.username, IDLE_TIMEOUT)
        db.delete(row)
        db.commit()
        return None
    if request.headers.get("x-poll") != "1":
        row.last_seen_at = now
        db.commit()
    return row.username


def _issue_session(db: OrmSession, username: str, response: Response, ua: str | None) -> None:
    token = secrets.token_urlsafe(32)
    db.add(SessionRow(token_hash=_sha(token), username=username, created_at=_now(),
                      expires_at=_now() + SESSION_TTL, last_seen_at=_now(),
                      user_agent=(ua or "")[:300]))
    u = db.get(User, username)
    if u:
        u.last_login_at = _now()
        u.failed_logins, u.locked_until = 0, None
    db.commit()
    response.set_cookie(
        COOKIE, token, max_age=int(SESSION_TTL.total_seconds()), httponly=True, samesite="lax",
        # Secure would be correct on HTTPS; this dashboard is reached as http://localhost through an
        # SSH tunnel, and a Secure cookie is simply never sent over that, locking the operator out.
        secure=False, path="/")


# ── routes ────────────────────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class VerifyBody(BaseModel):
    challenge: str
    code: str


@router.post("/login")
def login(body: LoginBody, request: Request, db: OrmSession = Depends(get_db)) -> dict:
    """Step 1 — check the password, mail a code, return the challenge id.

    Never returns a session. Never says which of username/password was wrong.
    """
    generic = HTTPException(401, "Incorrect user id or password")
    u = db.get(User, body.username.strip())
    if u is None or u.disabled:
        # Spend the same work as a real verification so a missing user is not measurably faster.
        hashlib.scrypt(b"x", salt=b"y" * 16, **KDF)
        raise generic
    if u.locked_until and u.locked_until > _now():
        wait = int((u.locked_until - _now()).total_seconds() // 60) + 1
        raise HTTPException(429, f"Too many failed attempts. Try again in {wait} minute(s).")
    if not verify_password(u, body.password):
        u.failed_logins = (u.failed_logins or 0) + 1
        if u.failed_logins >= PW_MAX_FAILURES:
            u.locked_until = _now() + PW_LOCKOUT
            u.failed_logins = 0
            log.warning("account %r locked after %d failed attempts", u.username, PW_MAX_FAILURES)
        db.commit()
        raise generic

    code = f"{secrets.randbelow(1_000_000):06d}"
    cid = secrets.token_urlsafe(24)
    recipients = [a for a in (u.email or "").split(",") if a.strip()] or _otp_recipients()

    results = mailer.send(
        recipients,
        "Mallory dashboard sign-in code",
        f"Your sign-in code is {code}\n\n"
        f"It expires in {int(OTP_TTL.total_seconds() // 60)} minutes and can be used once.\n"
        f"Requested from {request.client.host if request.client else 'unknown'}.\n\n"
        f"If this was not you, the password on the crawler dashboard is known to someone else —\n"
        f"change DASH_ADMIN_PASSWORD and restart the orchestrator.\n")
    delivered = [a for a, err in results.items() if err is None]
    failed = {a: err for a, err in results.items() if err}

    db.add(LoginChallenge(id=cid, username=u.username, otp_hash=_sha(code),
                          expires_at=_now() + OTP_TTL, attempts=0, consumed=False,
                          created_at=_now(), sent_to=", ".join(mailer.mask(a) for a in delivered),
                          delivery_error=("; ".join(f"{mailer.mask(a)}: {e}"
                                                    for a, e in failed.items()) or None)))
    # Sweep expired challenges here rather than on a timer — this is the only route that makes them.
    for old in db.scalars(select(LoginChallenge).where(LoginChallenge.expires_at < _now())).all():
        db.delete(old)
    db.commit()

    if not delivered:
        # The code exists but reached nobody. Saying "check your email" here would send the operator
        # to look for a message that was never sent.
        raise HTTPException(502, f"Could not send the code: {'; '.join(failed.values())[:300]}")
    return {"challenge": cid, "sent_to": [mailer.mask(a) for a in delivered],
            "expires_in_s": int(OTP_TTL.total_seconds()),
            "partial_failure": [mailer.mask(a) for a in failed] or None}


@router.post("/verify")
def verify(body: VerifyBody, request: Request, response: Response,
           db: OrmSession = Depends(get_db)) -> dict:
    """Step 2 — check the code, issue the session cookie."""
    ch = db.get(LoginChallenge, body.challenge.strip())
    if ch is None or ch.consumed or ch.expires_at <= _now():
        raise HTTPException(401, "That code has expired. Start again.")
    if ch.attempts >= OTP_MAX_ATTEMPTS:
        raise HTTPException(429, "Too many incorrect codes. Start again.")
    ch.attempts += 1
    db.commit()
    if not hmac.compare_digest(ch.otp_hash, _sha(body.code.strip())):
        left = OTP_MAX_ATTEMPTS - ch.attempts
        raise HTTPException(401, f"Incorrect code. {left} attempt(s) left."
                            if left > 0 else "Too many incorrect codes. Start again.")
    ch.consumed = True                 # single use, even within its TTL
    db.commit()
    _issue_session(db, ch.username, response, request.headers.get("user-agent"))
    return {"ok": True, "username": ch.username}


@router.post("/logout")
def logout(request: Request, response: Response, db: OrmSession = Depends(get_db)) -> dict:
    tok = request.cookies.get(COOKIE)
    if tok:
        row = db.get(SessionRow, _sha(tok))
        if row:
            db.delete(row)
            db.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request, db: OrmSession = Depends(get_db)) -> dict:
    if not enabled():
        return {"auth": False, "user": None}
    return {"auth": True, "user": current_user(request, db)}

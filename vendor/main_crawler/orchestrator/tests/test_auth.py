"""The login gate. Every test here is a way the gate could fail OPEN or lock the operator out."""
from __future__ import annotations

import datetime as dt
import importlib

import pytest


@pytest.fixture()
def auth(monkeypatch, tmp_path):
    """A fresh auth module bound to a throwaway SQLite file, with the mailer captured."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'orch.db'}")
    monkeypatch.setenv("DASH_AUTH", "1")
    monkeypatch.setenv("DASH_ADMIN_USER", "admin")
    monkeypatch.setenv("DASH_ADMIN_PASSWORD", "s3cret-pw")
    monkeypatch.setenv("DASH_OTP_EMAILS", "a@example.org, b@example.org")

    from mallory_orchestrator import config, db as db_mod
    importlib.reload(config)
    importlib.reload(db_mod)
    from mallory_orchestrator import auth as auth_mod, mailer, models
    importlib.reload(auth_mod)
    # `models.Base.metadata`, NOT the freshly-reloaded `db_mod.Base.metadata`. Reloading `db` builds
    # a brand-new DeclarativeBase whose metadata is empty, so create_all() on it makes no tables at
    # all; and reloading `models` to repopulate it fails the second time round with "Table 'sources'
    # is already defined". The tables the models actually registered are the ones to create, and a
    # mapped class works against any engine the session is bound to.
    models.Base.metadata.create_all(db_mod.engine)

    sent: list[tuple[list[str], str]] = []

    def fake_send(recipients, subject, text):
        sent.append((list(recipients), text))
        return {a: None for a in recipients}

    monkeypatch.setattr(mailer, "send", fake_send)
    monkeypatch.setattr(auth_mod.mailer, "send", fake_send)
    auth_mod.sent = sent          # handed to the tests
    auth_mod._models = models
    auth_mod._db = db_mod
    return auth_mod


def _code_from(text: str) -> str:
    import re
    return re.search(r"\b(\d{6})\b", text).group(1)


class Req:
    """Enough of a Starlette request for these routes."""

    def __init__(self, cookies=None):
        self.cookies = cookies or {}
        self.headers = {"user-agent": "pytest"}
        self.client = type("C", (), {"host": "127.0.0.1"})()


class Resp:
    def __init__(self):
        self.cookies_set = {}

    def set_cookie(self, name, value, **kw):
        self.cookies_set[name] = value

    def delete_cookie(self, name, **kw):
        self.cookies_set.pop(name, None)


def test_password_is_not_stored_in_any_recoverable_form(auth):
    auth.ensure_admin()
    with auth._db.SessionLocal() as db:
        u = db.get(auth._models.User, "admin")
    assert "s3cret-pw" not in (u.password_hash + u.password_salt)
    assert auth.verify_password(u, "s3cret-pw")
    assert not auth.verify_password(u, "s3cret-pw ")
    assert not auth.verify_password(u, "wrong")


def test_correct_password_alone_does_not_log_you_in(auth):
    """The whole point of the second factor: step 1 hands back a challenge, never a session."""
    auth.ensure_admin()
    with auth._db.SessionLocal() as db:
        out = auth.login(auth.LoginBody(username="admin", password="s3cret-pw"), Req(), db)
    assert "challenge" in out
    assert "token" not in out and "session" not in out
    # and the code went to BOTH configured addresses
    assert auth.sent[0][0] == ["a@example.org", "b@example.org"]


def test_otp_completes_the_login_and_is_single_use(auth):
    auth.ensure_admin()
    with auth._db.SessionLocal() as db:
        ch = auth.login(auth.LoginBody(username="admin", password="s3cret-pw"), Req(), db)
    code = _code_from(auth.sent[-1][1])

    resp = Resp()
    with auth._db.SessionLocal() as db:
        assert auth.verify(auth.VerifyBody(challenge=ch["challenge"], code=code),
                           Req(), resp, db)["ok"]
    token = resp.cookies_set[auth.COOKIE]

    # the cookie authenticates...
    with auth._db.SessionLocal() as db:
        assert auth.current_user(Req({auth.COOKIE: token}), db) == "admin"
    # ...and the SAME code cannot be replayed
    with auth._db.SessionLocal() as db:
        with pytest.raises(Exception):
            auth.verify(auth.VerifyBody(challenge=ch["challenge"], code=code), Req(), Resp(), db)


def test_wrong_otp_is_rejected_and_attempts_are_capped(auth):
    auth.ensure_admin()
    with auth._db.SessionLocal() as db:
        ch = auth.login(auth.LoginBody(username="admin", password="s3cret-pw"), Req(), db)
    real = _code_from(auth.sent[-1][1])
    wrong = "000000" if real != "000000" else "111111"

    for _ in range(auth.OTP_MAX_ATTEMPTS):
        with auth._db.SessionLocal() as db:
            with pytest.raises(Exception):
                auth.verify(auth.VerifyBody(challenge=ch["challenge"], code=wrong),
                            Req(), Resp(), db)
    # burnt through the attempt budget — even the CORRECT code is now refused
    with auth._db.SessionLocal() as db:
        with pytest.raises(Exception):
            auth.verify(auth.VerifyBody(challenge=ch["challenge"], code=real), Req(), Resp(), db)


def test_expired_challenge_is_refused(auth):
    auth.ensure_admin()
    with auth._db.SessionLocal() as db:
        ch = auth.login(auth.LoginBody(username="admin", password="s3cret-pw"), Req(), db)
    code = _code_from(auth.sent[-1][1])
    with auth._db.SessionLocal() as db:
        row = db.get(auth._models.LoginChallenge, ch["challenge"])
        row.expires_at = auth._now() - dt.timedelta(seconds=1)
        db.commit()
    with auth._db.SessionLocal() as db:
        with pytest.raises(Exception):
            auth.verify(auth.VerifyBody(challenge=ch["challenge"], code=code), Req(), Resp(), db)


def test_bad_password_never_sends_a_code(auth):
    """A code mailed on a WRONG password would let anyone spam the operator's inbox, and would leak
    that the username exists."""
    auth.ensure_admin()
    with auth._db.SessionLocal() as db:
        with pytest.raises(Exception):
            auth.login(auth.LoginBody(username="admin", password="nope"), Req(), db)
    assert auth.sent == []


def test_unknown_user_and_wrong_password_are_indistinguishable(auth):
    auth.ensure_admin()
    msgs = []
    for user, pw in (("admin", "nope"), ("nobody", "nope")):
        with auth._db.SessionLocal() as db:
            try:
                auth.login(auth.LoginBody(username=user, password=pw), Req(), db)
            except Exception as e:  # noqa: BLE001
                msgs.append(getattr(e, "detail", str(e)))
    assert msgs[0] == msgs[1], "the error text distinguishes a real account from a fake one"


def test_repeated_failures_lock_the_account(auth):
    auth.ensure_admin()
    for _ in range(auth.PW_MAX_FAILURES):
        with auth._db.SessionLocal() as db:
            with pytest.raises(Exception):
                auth.login(auth.LoginBody(username="admin", password="nope"), Req(), db)
    # now even the RIGHT password is refused while the lockout stands
    with auth._db.SessionLocal() as db:
        with pytest.raises(Exception) as e:
            auth.login(auth.LoginBody(username="admin", password="s3cret-pw"), Req(), db)
    assert "Too many" in getattr(e.value, "detail", "")


def test_logout_kills_the_session_server_side(auth):
    auth.ensure_admin()
    with auth._db.SessionLocal() as db:
        ch = auth.login(auth.LoginBody(username="admin", password="s3cret-pw"), Req(), db)
    code = _code_from(auth.sent[-1][1])
    resp = Resp()
    with auth._db.SessionLocal() as db:
        auth.verify(auth.VerifyBody(challenge=ch["challenge"], code=code), Req(), resp, db)
    token = resp.cookies_set[auth.COOKIE]

    with auth._db.SessionLocal() as db:
        auth.logout(Req({auth.COOKIE: token}), Resp(), db)
    # holding the cookie is no longer enough — the row is gone, not just the browser copy
    with auth._db.SessionLocal() as db:
        assert auth.current_user(Req({auth.COOKIE: token}), db) is None


def test_expired_session_stops_authenticating(auth):
    auth.ensure_admin()
    with auth._db.SessionLocal() as db:
        ch = auth.login(auth.LoginBody(username="admin", password="s3cret-pw"), Req(), db)
    code = _code_from(auth.sent[-1][1])
    resp = Resp()
    with auth._db.SessionLocal() as db:
        auth.verify(auth.VerifyBody(challenge=ch["challenge"], code=code), Req(), resp, db)
    token = resp.cookies_set[auth.COOKIE]
    with auth._db.SessionLocal() as db:
        row = db.get(auth._models.Session, auth._sha(token))
        row.expires_at = auth._now() - dt.timedelta(seconds=1)
        db.commit()
    with auth._db.SessionLocal() as db:
        assert auth.current_user(Req({auth.COOKIE: token}), db) is None


def test_session_token_is_not_stored_verbatim(auth):
    """A stolen copy of the DB must not hand over a live session."""
    auth.ensure_admin()
    with auth._db.SessionLocal() as db:
        ch = auth.login(auth.LoginBody(username="admin", password="s3cret-pw"), Req(), db)
    code = _code_from(auth.sent[-1][1])
    resp = Resp()
    with auth._db.SessionLocal() as db:
        auth.verify(auth.VerifyBody(challenge=ch["challenge"], code=code), Req(), resp, db)
    token = resp.cookies_set[auth.COOKIE]
    with auth._db.SessionLocal() as db:
        rows = db.query(auth._models.Session).all()
    assert rows and all(r.token_hash != token for r in rows)


def test_enabled_but_unconfigured_refuses_to_start(auth, monkeypatch):
    """Fail CLOSED. A misconfigured gate that silently serves an open dashboard is worse than none,
    because the operator believes it is on."""
    monkeypatch.delenv("DASH_ADMIN_PASSWORD")
    with pytest.raises(RuntimeError, match="refusing to start"):
        auth.ensure_admin()
    monkeypatch.setenv("DASH_ADMIN_PASSWORD", "s3cret-pw")
    monkeypatch.setenv("DASH_OTP_EMAILS", "")
    with pytest.raises(RuntimeError, match="nowhere to send"):
        auth.ensure_admin()


def test_only_one_account_ever_exists(auth):
    """No signup, one account — so a stray row is a second way in and must be removed."""
    auth.ensure_admin()
    with auth._db.SessionLocal() as db:
        pw, salt = auth.hash_password("x")
        db.add(auth._models.User(username="ghost", email="g@example.org", password_hash=pw,
                                 password_salt=salt, kdf="scrypt", kdf_params=auth.KDF,
                                 disabled=False, created_at=auth._now()))
        db.commit()
    auth.ensure_admin()
    with auth._db.SessionLocal() as db:
        assert [u.username for u in db.query(auth._models.User).all()] == ["admin"]


def test_rotating_the_password_takes_effect(auth, monkeypatch):
    auth.ensure_admin()
    monkeypatch.setenv("DASH_ADMIN_PASSWORD", "new-pw")
    auth.ensure_admin()
    with auth._db.SessionLocal() as db:
        u = db.get(auth._models.User, "admin")
    assert auth.verify_password(u, "new-pw")
    assert not auth.verify_password(u, "s3cret-pw")


def test_masking_shows_the_mailbox_without_publishing_it(auth):
    from mallory_orchestrator import mailer
    assert mailer.mask("research@mssu.ac.in").endswith("@mssu.ac.in")
    assert "research" not in mailer.mask("research@mssu.ac.in")
    assert mailer.mask("2023000058@mssu.ac.in").startswith("20")

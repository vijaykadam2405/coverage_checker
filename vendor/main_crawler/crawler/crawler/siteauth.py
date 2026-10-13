"""Registered-account crawling: register with a disposable inbox, log in, keep the session.

The four Indian tender aggregators show a teaser — title, agency, due date — and put the tender
value, the buyer contact, the corrigenda and every attachment behind a free account. An anonymous
crawl of them returns a corpus of headlines. So the crawler needs to hold an account.

The flow, once per host, cached on disk:

1. Open the portal's registration page and fill it from a disposable inbox
   (:mod:`crawler.tempmail`). Fields are matched by ``name``/``id``/``placeholder``/``type``, not
   by a hand-written selector per site, because these forms change and a stale selector fails
   silently as "no login". A per-host recipe can pin anything the heuristic gets wrong.
2. Poll the inbox for a confirmation link and visit it.
3. Log in, and verify the result rather than assuming it (a form that re-renders with an error is
   an HTTP 200 with a password box on it).
4. Store the session cookies. Every later fetch — httpx and browser — replays them.

Sessions are stored in ``data/site_auth.json`` and reused until
``CRAWLER_AUTH_TTL_H`` hours old (default 168 = a week), then re-established.

**Known gap: the C3 tier does not carry the session.** Cookies are replayed on the httpx tier
(fetcher._http_fetch) and on the Playwright contexts (AsyncEngine._ensure_auth). A page that
escalates all the way to CamoFox renders in CamoFox's own profile, so it is fetched LOGGED OUT.
That matters exactly where it hurts most — bidassist and tenderdekho both 403 a headless browser,
so their pages are the ones most likely to escalate. Until CamoFox takes injected cookies, run
those crawls with ``CRAWLER_HEADLESS=0`` so the C1 browser clears the WAF itself and the session
holds.

**Scope, deliberately.** This automates a free self-service signup so a crawl can read what a
logged-in visitor reads; it does not defeat access control. Portals that need a phone OTP, a paid
plan, or a GST/company number to finish registration will fail here, and they fail LOUDLY — the
attempt is recorded in the issue log and the crawl proceeds anonymously rather than pretending it
has a session. Check ``GET /v1/auth/sessions`` before assuming a crawl was authenticated.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from . import config, issues, stealth, tempmail
from .tenderdefense import host_of

log = logging.getLogger("siteauth")

STORE_PATH = Path(os.environ.get("CRAWLER_AUTH_STORE", str(config.DATA_DIR / "site_auth.json")))

# Identity used on the forms. Not a disguise — a registration form needs *a* name and *a* phone,
# and leaving them blank fails validation. Override per deployment.
_PROFILE = {
    "name": os.environ.get("CRAWLER_AUTH_NAME", "Mallory Research"),
    "first": os.environ.get("CRAWLER_AUTH_FIRST", "Mallory"),
    "last": os.environ.get("CRAWLER_AUTH_LAST", "Research"),
    "phone": os.environ.get("CRAWLER_AUTH_PHONE", "9000000000"),
    "company": os.environ.get("CRAWLER_AUTH_COMPANY", "Mallory Research"),
    "city": os.environ.get("CRAWLER_AUTH_CITY", "New Delhi"),
    "pincode": os.environ.get("CRAWLER_AUTH_PINCODE", "110001"),
}

# Field heuristics, most specific first — "confirm password" must be tested before "password",
# and "last name" before "name", or the broader rule swallows the narrower one.
_FIELD_RULES: list[tuple[tuple[str, ...], str]] = [
    (("confirm_password", "confirmpassword", "password_confirmation", "cpassword",
      "repeat_password", "retype"), "password"),
    (("email", "e-mail", "mail"), "email"),
    (("password", "passwd", "pwd"), "password"),
    (("firstname", "first_name", "fname", "given"), "first"),
    (("lastname", "last_name", "lname", "surname"), "last"),
    (("mobile", "phone", "contact_no", "contactno", "whatsapp", "cell"), "phone"),
    (("company", "organisation", "organization", "firm", "business"), "company"),
    (("pincode", "pin_code", "zip", "postal"), "pincode"),
    (("city", "district", "location"), "city"),
    (("name", "fullname", "full_name"), "name"),
]

# Anchors/buttons that open the registration form when a site has no direct /register URL.
_REGISTER_LINK = ("register", "sign up", "signup", "create account", "create an account",
                  "new user", "join free", "free trial")
# /sign-in before /login: tender18 serves the real login at /sign-in and 404s /login, and several
# of these portals answer BOTH with a 200, so order decides which one we fill.
_LOGIN_PATHS = ("/sign-in", "/signin", "/login", "/user/login", "/account/login", "/users/login")
_REGISTER_PATHS = ("/register", "/signup", "/sign-up", "/user/register", "/account/register")

# Proof of a session. Checked as visible text; a page that still shows a password box is not
# logged in no matter what else it says.
_LOGGED_IN_MARKS = ("logout", "log out", "sign out", "my account", "my profile", "dashboard",
                    "my dashboard")

#: Per-host overrides for anything the heuristics get wrong. Empty by default ON PURPOSE: a
#: selector written today against a portal we cannot regression-test rots into a silent failure.
#: Add an entry the first time a host actually needs it, with the date you verified it.
#: Keys: register_url, login_url, email_selector, password_selector, submit_selector,
#: success_marker, extra_fields {selector: value}.
RECIPES: dict[str, dict[str, Any]] = {}

#: What we MEASURED about each portal's signup, 2026-07-25. Surfaced to the operator instead of a
#: generic "no form found", and used to skip a doomed attempt (which would otherwise burn a
#: disposable account and 90s of mail-waiting per crawl, every crawl).
#:
#: The headline result: NONE of the four big Indian aggregators offers an email-based
#: self-service signup, so the disposable-inbox path cannot open an account on any of them. They
#: are reachable only with credentials you already hold — see set_credentials. The machinery
#: still applies to portals that DO self-serve; these four are simply not among them.
#: Re-check with: POST /v1/auth/session?url=...&force=true, and update the date when you do.
PORTAL_NOTES: dict[str, str] = {
    "tender18.com":
        "/register is a sales enquiry form (name, company, state, mobile, email, description) "
        "with no password field — accounts are issued by their sales team. Login lives at "
        "/sign-in. Seed credentials with POST /v1/auth/credentials.",
    "bidassist.com":
        "signup is mobile-number + SMS OTP only (the #AUTH modal has no email field at all), so "
        "a disposable inbox cannot open an account. Also 403s a headless browser — run the auth "
        "flow with CRAWLER_HEADLESS=0. Seed credentials with POST /v1/auth/credentials.",
    "tenderdekho.com":
        "no /register, /login or /signup route exists (all 404). Also 403s a headless browser — "
        "run the auth flow with CRAWLER_HEADLESS=0.",
    "tenderdetail.com":
        "answers EVERY unknown path with HTTP 200 by redirecting to /Country-tenders, so no auth "
        "URL can be discovered by guessing. Find the real login by hand and pin it in RECIPES.",
}


# ── session store ────────────────────────────────────────────────────────────────────────────
_CACHE: tuple[float, dict] = (-1.0, {})


def _load() -> dict:
    """The session store, cached on the file's mtime.

    cookie_header() is called on EVERY httpx fetch, so re-reading and re-parsing the JSON each
    time would put a syscall + a parse in the hot loop for a file that changes once a week.
    Keying the cache on mtime means an out-of-process refresh (the /v1/auth/session endpoint runs
    in the API worker, not here) is still picked up on the next fetch.
    """
    global _CACHE
    try:
        mtime = STORE_PATH.stat().st_mtime
    except OSError:
        return {}
    if _CACHE[0] == mtime:
        return _CACHE[1]
    try:
        data = json.loads(STORE_PATH.read_text("utf-8"))
    except Exception:
        return {}
    _CACHE = (mtime, data)
    return data


def _save(data: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), "utf-8")
    tmp.replace(STORE_PATH)                       # atomic: never leave a half-written session file


def get_session(url_or_host: str) -> dict | None:
    """Stored session for a host, or None. Does not check freshness — see ``is_fresh``.

    Takes a full URL or a bare hostname; host_of normalises both to the same store key, so a
    lookup can never miss a session just because the caller had the other form.
    """
    return _load().get(host_of(url_or_host))


def is_fresh(sess: dict | None) -> bool:
    if not sess or not sess.get("cookies"):
        return False
    ttl_h = float(os.environ.get("CRAWLER_AUTH_TTL_H", "168"))
    return (time.time() - float(sess.get("saved_at", 0))) < ttl_h * 3600


def cookie_header(url_or_host: str) -> str:
    """``Cookie:`` header value for httpx, or "" when there is no live session.

    Host-scoped by construction: only the cookies stored under THIS host are returned, so a
    session for one portal can never leak into a request to another.
    """
    sess = get_session(url_or_host)
    if not is_fresh(sess):
        return ""
    return "; ".join(f"{c['name']}={c['value']}" for c in sess["cookies"]
                     if c.get("name") and c.get("value") is not None)


def playwright_cookies(url_or_host: str) -> list[dict]:
    """Stored cookies in Playwright's ``add_cookies`` shape, or []."""
    sess = get_session(url_or_host)
    return list(sess["cookies"]) if is_fresh(sess) else []


def all_sessions() -> list[dict]:
    """Every stored session, redacted — cookie VALUES are session credentials and never leave
    this module. Enough to answer "is the crawl logged in, and to what"."""
    return [{"host": h, "email": s.get("email"), "saved_at": s.get("saved_at"),
             "fresh": is_fresh(s), "cookies": len(s.get("cookies") or []),
             "verified_email": bool(s.get("verified_email")), "error": s.get("error")}
            for h, s in sorted(_load().items())]


def set_credentials(host: str, email: str, password: str) -> dict:
    """Pre-seed an account so ``establish`` logs in instead of registering.

    Needed more often than the automatic path on these portals. Measured 2026-07-25: tender18's
    /register is a sales enquiry form with no password field, and tenderdetail answers every
    unknown path with its listing page — neither offers a self-service signup at all. If you were
    issued (or bought) a login, put it here and the whole register/confirm stage is skipped; the
    session refresh, cookie replay and TTL work exactly the same.

    Stored in plaintext in ``data/site_auth.json`` alongside the session cookies — those cookies
    already grant the same access, so the file is a secret either way. Keep it out of git (it is
    under data/) and off shared volumes.
    """
    h = host_of(host)
    if not h:
        raise ValueError("a host is required")
    data = _load()
    rec = data.get(h) or {}
    rec.update({"email": email, "password": password, "cookies": [], "saved_at": 0,
                "manual": True, "error": None})
    data[h] = rec
    _save(data)
    return {"host": h, "email": email, "manual": True}


def forget(host: str) -> bool:
    data = _load()
    if host in data:
        del data[host]
        _save(data)
        return True
    return False


# ── form filling ─────────────────────────────────────────────────────────────────────────────
def classify_field(name: str, ftype: str = "", email_elsewhere: bool = False) -> str | None:
    """Which profile value belongs in this input. ``name`` is name/id/placeholder/label joined.

    Returns a _PROFILE key, "email", "password", or None for "leave it alone". Type wins over
    name for the two unambiguous HTML types, because a field named "user" of type=password is a
    password box whatever it is called.

    ``email_elsewhere`` resolves the one genuinely ambiguous field. A box called ``username`` is
    the email box on a login form — but on a signup form that ALSO carries a real email field it
    is the person's name. That is not hypothetical: tender18.com's registration form is
    ``<input type=text name=username placeholder="Name">`` next to ``<input type=email
    name=email>``, and treating it as the login identifier types an email address into the Name
    box, which the site then rejects. The caller decides it per FORM, in fill_form.
    """
    t = (ftype or "").lower()
    if t == "password":
        return "password"
    if t == "email":
        return "email"
    low = (name or "").lower()
    if not low:
        return None
    # An OTP box cannot be filled from an inbox — leave it empty so the submit fails visibly
    # rather than filling it with junk and reporting a login we do not have.
    if any(k in low for k in ("otp", "captcha", "verification_code", "verify_code")):
        return None
    # Checked before the generic rules: "username" contains "name", so the trailing name rule
    # would otherwise swallow every login identifier on the web.
    if any(k in low for k in ("username", "user_name", "userid", "user_id", "loginid",
                              "login_id", "user_id")):
        return "name" if email_elsewhere else "email"
    for keys, field in _FIELD_RULES:
        if any(k in low for k in keys):
            return field
    return None


def field_value(field: str, email: str, password: str) -> str:
    if field == "email":
        return email
    if field == "password":
        return password
    return _PROFILE.get(field, "")


async def _describe(inp) -> tuple[str, str]:
    """(joined identifying text, type) for one input element."""
    parts = []
    for attr in ("name", "id", "placeholder", "aria-label", "autocomplete"):
        try:
            v = await inp.get_attribute(attr)
        except Exception:
            v = None
        if v:
            parts.append(v)
    try:
        ftype = (await inp.get_attribute("type")) or "text"
    except Exception:
        ftype = "text"
    return " ".join(parts), ftype


async def fill_form(page, email: str, password: str) -> int:
    """Fill every recognisable input on the page. Returns how many fields were filled.

    Zero means the heuristics found nothing to type — a real signal (wrong page, or a form
    rendered inside an iframe), and the caller treats it as a failure rather than submitting an
    empty form and reading the resulting error page as "registered".
    """
    inputs = await page.query_selector_all("input")
    described = []
    for inp in inputs:
        described.append((inp, *await _describe(inp)))
    # Form-level pass first: does this form have an unmistakable email box? That answers whether a
    # "username" field beside it is the login identifier or the person's name. Per-field
    # classification cannot know it, and getting it wrong silently fails the registration.
    email_elsewhere = any(t.lower() == "email" or
                          any(k in ident.lower() for k in ("email", "e-mail"))
                          for _i, ident, t in described)

    filled = 0
    for inp, ident, ftype in described:
        try:
            if ftype in ("hidden", "submit", "button", "file", "search"):
                continue
            if ftype == "checkbox":
                # Terms/privacy consent gates the submit button on all four portals. Only ever
                # CHECK a box — never uncheck one the site pre-set.
                if any(k in ident.lower() for k in ("term", "agree", "accept", "privacy",
                                                    "condition")):
                    try:
                        await inp.check(timeout=3000)
                        filled += 1
                    except Exception:
                        pass
                continue
            if ftype == "radio":
                continue
            field = classify_field(ident, ftype, email_elsewhere)
            if not field:
                continue
            val = field_value(field, email, password)
            if not val:
                continue
            await inp.fill(val, timeout=5000)
            filled += 1
        except Exception as exc:
            log.debug("field fill skipped: %s", exc)
    # Required <select>s (country, state, business type) block submission when left on their
    # placeholder option. Take the first REAL option — index 1 when index 0 is the "-- select --"
    # placeholder, which is the near-universal shape.
    for sel in await page.query_selector_all("select"):
        try:
            opts = await sel.query_selector_all("option")
            if len(opts) < 2:
                continue
            val = await opts[1].get_attribute("value")
            if val:
                await sel.select_option(val, timeout=3000)
                filled += 1
        except Exception:
            pass
    return filled


async def _submit(page) -> bool:
    """Press the form's submit control. Tries the semantic submit first, then a button whose text
    says what it does — some of these forms are a <div onclick>."""
    for sel in ("button[type=submit]", "input[type=submit]",
                "button:has-text('Register')", "button:has-text('Sign Up')",
                "button:has-text('Sign up')", "button:has-text('Submit')",
                "button:has-text('Login')", "button:has-text('Log In')",
                "button:has-text('Sign In')", "a:has-text('Submit')"):
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click(timeout=8000)
                return True
        except Exception:
            continue
    try:                                     # last resort: Enter in the form
        await page.keyboard.press("Enter")
        return True
    except Exception:
        return False


async def looks_logged_in(page) -> bool:
    """Whether this page is being served to a logged-in visitor.

    A visible password box outvotes every positive marker: the single most common failure is a
    login form that re-renders with "invalid credentials" — HTTP 200, page still says "My
    Account" in the nav, and a naive text check calls it a success.
    """
    try:
        pw = await page.query_selector("input[type=password]")
        if pw and await pw.is_visible():
            return False
    except Exception:
        pass
    try:
        text = (await page.inner_text("body"))[:20000].lower()
    except Exception:
        return False
    return any(m in text for m in _LOGGED_IN_MARKS)


def landed_on_guess(requested: str, landed: str) -> bool:
    """Did the browser stay on the path we asked for?

    HTTP status is not enough to tell "this page exists" from "this site answers everything".
    tenderdetail.com serves HTTP 200 for /register, /signup, /login and every other unknown path
    by redirecting to /Country-tenders — so a status check finds five registration pages that do
    not exist, and the flow then fills the site search box and calls it a signup. Comparing the
    landed path against the requested one costs nothing and catches the whole class.
    """
    from urllib.parse import urlsplit
    want = urlsplit(requested).path.rstrip("/").lower()
    got = urlsplit(landed).path.rstrip("/").lower()
    return want == got


async def _goto_first(page, urls: list[str], need_password: bool = False) -> str | None:
    """First URL that loads, stays on its own path, and (optionally) shows a password box."""
    for u in urls:
        if not u:
            continue
        try:
            r = await page.goto(u, wait_until="domcontentloaded", timeout=45000)
            if r is not None and r.status >= 400:
                continue
            if not landed_on_guess(u, page.url):
                continue
            if need_password:
                # These forms are routinely React-rendered — give the box a moment to exist
                # before concluding the page has none.
                try:
                    await page.wait_for_selector("input[type=password]", timeout=6000)
                except Exception:
                    continue
            return u
        except Exception:
            continue
    return None


async def _find_register_page(page, base: str, recipe: dict) -> tuple[str | None, str]:
    """Locate the registration form. Returns (url, reason-if-unusable).

    The reason matters: "no page" and "a page with no password field" are different problems for
    the operator. tender18.com's /register is the second kind — a sales lead-capture form (name,
    company, state, mobile, email, description) with no password anywhere, because that portal
    issues logins through its sales team. No amount of form-filling produces an account there,
    and reporting it as "form not found" would send someone hunting for a URL that exists.
    """
    if recipe.get("register_url"):
        hit = await _goto_first(page, [recipe["register_url"]])
        return (hit, "" if hit else "recipe register_url did not load")
    guesses = [base.rstrip("/") + p for p in _REGISTER_PATHS]
    hit = await _goto_first(page, guesses, need_password=True)
    if hit:
        return hit, ""
    # A page existed but had no password box — say which, and say what it means.
    plain = await _goto_first(page, guesses)
    if plain:
        return None, (f"{plain} exists but has no password field — this looks like a sales "
                      f"enquiry form, not a self-service signup. Set credentials manually "
                      f"(POST /v1/auth/credentials) if you hold an account.")
    try:
        await page.goto(base, wait_until="domcontentloaded", timeout=45000)
        for a in await page.query_selector_all("a"):
            try:
                txt = ((await a.inner_text()) or "").strip().lower()
            except Exception:
                continue
            if any(k == txt or k in txt for k in _REGISTER_LINK):
                await a.click(timeout=8000)
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_selector("input[type=password]", timeout=6000)
                except Exception:
                    continue          # a modal that carries no password box is not the signup
                return page.url, ""
    except Exception as exc:
        log.debug("register-link discovery failed on %s: %s", base, exc)
    return None, "no self-service registration form found"


# ── the flow ─────────────────────────────────────────────────────────────────────────────────
async def establish(url: str, headless: bool | None = None) -> dict:
    """Register (if needed), confirm, log in, and store the session for ``url``'s host.

    Returns the redacted session record. Never raises for a site-side failure — a portal we
    cannot join is a normal outcome that the caller handles by crawling anonymously — but the
    record then carries ``error`` and no cookies, so nothing downstream can mistake it for a
    live session.
    """
    from playwright.async_api import async_playwright

    host = host_of(url)
    base = f"https://{host}"
    recipe = RECIPES.get(host, {})
    data = _load()
    prior = data.get(host) or {}
    record: dict = {"email": prior.get("email"), "password": prior.get("password"),
                    "cookies": [], "saved_at": 0, "verified_email": False, "error": None}

    if headless is None:
        headless = os.environ.get("CRAWLER_HEADLESS", "1") != "0"

    # Bail before the browser: we have already measured that this host has no self-service signup,
    # and we hold no credentials for it. Launching Chromium to rediscover that on every crawl is
    # pure cost. The issue-log entry still happens via _finish, so "crawled anonymously" is loud.
    if not (record["email"] and record["password"]):
        note = PORTAL_NOTES.get(host)
        if note and not recipe.get("register_url"):
            record["error"] = f"no self-service signup: {note}"
            return _finish(data, host, record)

    pw = await async_playwright().start()
    browser = ctx = None
    try:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", *stealth.stealth_launch_args()],
            ignore_default_args=stealth.stealth_ignore_default_args())
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900}, locale="en-IN",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"})
        if stealth.stealth_enabled():
            await ctx.add_init_script(stealth.STEALTH_INIT_JS)
        page = await ctx.new_page()

        # Reuse the account we already made for this host — re-registering on every crawl would
        # litter the portal with accounts and is exactly what gets an IP blocked.
        if not (record["email"] and record["password"]):
            # Portals with a known-impossible signup already returned above, before the launch.
            inbox = await asyncio.to_thread(tempmail.new_inbox)
            record["email"] = inbox.address
            record["password"] = tempmail.random_password()
            reg_url, why = await _find_register_page(page, base, recipe)
            if not reg_url:
                record["error"] = why or "no registration form found"
                return _finish(data, host, record)
            n = await fill_form(page, record["email"], record["password"])
            if n == 0:
                record["error"] = f"registration form at {reg_url} had no fillable fields"
                return _finish(data, host, record)
            await _submit(page)
            try:
                await page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            link = await asyncio.to_thread(
                inbox.wait_for_link, float(os.environ.get("CRAWLER_AUTH_MAIL_WAIT_S", "150")))
            if link:
                try:
                    await page.goto(link, wait_until="domcontentloaded", timeout=45000)
                    record["verified_email"] = True
                except Exception as exc:
                    log.warning("%s: confirmation link failed: %s", host, exc)
            else:
                # Not fatal. Several portals log you straight in and only mail you later.
                log.info("%s: no confirmation mail within the wait — trying login anyway", host)

        # Log in explicitly even right after registering: a post-registration page is often a
        # "check your email" interstitial with no session behind it.
        if not await looks_logged_in(page):
            login_url = recipe.get("login_url") or None
            hit = await _goto_first(page, [login_url] if login_url
                                    else [base.rstrip("/") + p for p in _LOGIN_PATHS],
                                    need_password=True)
            if not hit:
                record["error"] = "no login form found (tried " + \
                    ", ".join(_LOGIN_PATHS) + ")"
                return _finish(data, host, record)
            if hit:
                await fill_form(page, record["email"], record["password"])
                await _submit(page)
                try:
                    await page.wait_for_load_state("networkidle", timeout=30000)
                except Exception:
                    pass

        if not await looks_logged_in(page):
            record["error"] = "login did not take (still showing a password field or no session)"
            return _finish(data, host, record)

        record["cookies"] = [c for c in await ctx.cookies() if host in (c.get("domain") or "")]
        if not record["cookies"]:
            record["error"] = "logged in but the context held no cookies for this host"
            return _finish(data, host, record)
        record["saved_at"] = time.time()
        log.info("%s: session established as %s (%d cookies)",
                 host, record["email"], len(record["cookies"]))
        return _finish(data, host, record)
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        return _finish(data, host, record)
    finally:
        for closer in (ctx, browser):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        try:
            await pw.stop()
        except Exception:
            pass


def _finish(data: dict, host: str, record: dict) -> dict:
    data[host] = record
    _save(data)
    if record.get("error"):
        # Loudly, in the same log the operator already reads for blocked hosts — a crawl that
        # silently fell back to anonymous is how a portal "has no data".
        issues.log().record(f"https://{host}/", "auth_failed", action="crawling anonymously",
                            evidence=str(record["error"])[:300])
        log.warning("%s: no session — %s", host, record["error"])
    return {k: v for k, v in record.items() if k not in ("cookies", "password")} | \
           {"host": host, "cookies": len(record.get("cookies") or [])}


async def ensure_session(url: str, force: bool = False) -> bool:
    """Make sure there is a live session for ``url``'s host. True if one exists afterwards.

    Cheap and idempotent on the hot path: a fresh stored session returns immediately without
    touching the network, so the engine can call this per job without thinking about it.
    """
    if not force and is_fresh(get_session(url)):
        return True
    if os.environ.get("CRAWLER_AUTH_ENABLED", "1") == "0":
        log.info("auth disabled by CRAWLER_AUTH_ENABLED=0 — %s crawled anonymously", host_of(url))
        return False
    await establish(url)
    return is_fresh(get_session(url))


def demo() -> None:
    """Self-check for the pure logic — no network, no browser."""
    assert classify_field("user_password", "password") == "password"
    assert classify_field("confirm_password", "text") == "password"
    assert classify_field("emailAddress", "text") == "email"
    assert classify_field("txtMobileNo", "text") == "phone"
    assert classify_field("company_name", "text") == "company"      # 'company' beats 'name'
    assert classify_field("lastName", "text") == "last"             # 'lastname' beats 'name'
    assert classify_field("otp", "text") is None                    # never guess an OTP
    assert classify_field("g-recaptcha-response", "text") is None
    assert classify_field("some_slider", "range") is None
    assert field_value("email", "a@b.c", "pw") == "a@b.c"
    assert field_value("phone", "a@b.c", "pw") == _PROFILE["phone"]
    # A stale session must not be replayed as a Cookie header.
    assert is_fresh({"cookies": [{"name": "a", "value": "b"}], "saved_at": time.time()})
    assert not is_fresh({"cookies": [], "saved_at": time.time()})
    assert not is_fresh({"cookies": [{"name": "a", "value": "b"}], "saved_at": 0})
    print("siteauth demo ok")


if __name__ == "__main__":
    demo()

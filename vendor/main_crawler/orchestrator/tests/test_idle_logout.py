"""Idle sign-out, and the reason it cannot be driven by requests alone.

This dashboard polls itself every few seconds. An idle timeout keyed on "last request" would never
fire while the tab is open, so an unattended screen would stay signed in indefinitely — precisely
what the timeout exists to prevent. Polls therefore send `X-Poll: 1` and do not reset the clock.
"""
from __future__ import annotations

import datetime as dt
import importlib
import re
from pathlib import Path

import pytest


@pytest.fixture()
def auth(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'o.db'}")
    monkeypatch.setenv("DASH_AUTH", "1")
    monkeypatch.setenv("DASH_ADMIN_USER", "admin")
    monkeypatch.setenv("DASH_ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("DASH_OTP_EMAILS", "a@example.org")
    monkeypatch.setenv("DASH_IDLE_MINUTES", "30")
    from mallory_orchestrator import config, db as db_mod
    importlib.reload(config)
    importlib.reload(db_mod)
    from mallory_orchestrator import auth as auth_mod, models
    importlib.reload(auth_mod)
    models.Base.metadata.create_all(db_mod.engine)
    auth_mod._db, auth_mod._models = db_mod, models
    return auth_mod


class Req:
    def __init__(self, cookies=None, headers=None):
        self.cookies = cookies or {}
        self.headers = headers or {}
        self.client = type("C", (), {"host": "127.0.0.1"})()


class Resp:
    def __init__(self): self.cookies_set = {}
    def set_cookie(self, name, value, **kw): self.cookies_set[name] = value
    def delete_cookie(self, name, **kw): self.cookies_set.pop(name, None)


def _session(auth) -> str:
    resp = Resp()
    with auth._db.SessionLocal() as db:
        auth._issue_session(db, "admin", resp, "pytest")
    return resp.cookies_set[auth.COOKIE]


def _age(auth, token, minutes):
    with auth._db.SessionLocal() as db:
        row = db.get(auth._models.Session, auth._sha(token))
        row.last_seen_at = auth._now() - dt.timedelta(minutes=minutes)
        db.commit()


def test_idle_past_the_timeout_signs_you_out(auth):
    tok = _session(auth)
    _age(auth, tok, 31)
    with auth._db.SessionLocal() as db:
        assert auth.current_user(Req({auth.COOKIE: tok}), db) is None
    with auth._db.SessionLocal() as db:
        assert db.get(auth._models.Session, auth._sha(tok)) is None, \
            "the row must be deleted, not merely refused — a cookie for a live row is a live session"


def test_just_inside_the_timeout_is_still_signed_in(auth):
    tok = _session(auth)
    _age(auth, tok, 29)
    with auth._db.SessionLocal() as db:
        assert auth.current_user(Req({auth.COOKIE: tok}), db) == "admin"


def test_A_POLL_DOES_NOT_KEEP_YOU_SIGNED_IN(auth):
    """The whole point. Without this the dashboard's own refresh loop defeats the timeout."""
    tok = _session(auth)
    _age(auth, tok, 20)
    for _ in range(5):                       # twenty minutes of polling, ten seconds apart
        with auth._db.SessionLocal() as db:
            assert auth.current_user(Req({auth.COOKIE: tok}, {"x-poll": "1"}), db) == "admin"
    _age(auth, tok, 31)                      # ...and the clock was never reset
    with auth._db.SessionLocal() as db:
        assert auth.current_user(Req({auth.COOKIE: tok}, {"x-poll": "1"}), db) is None


def test_real_activity_DOES_keep_you_signed_in(auth):
    tok = _session(auth)
    _age(auth, tok, 29)
    with auth._db.SessionLocal() as db:      # no X-Poll header = operator-caused
        assert auth.current_user(Req({auth.COOKIE: tok}), db) == "admin"
    _age(auth, tok, 29)                      # the clock restarted, so another 29 min is fine
    with auth._db.SessionLocal() as db:
        assert auth.current_user(Req({auth.COOKIE: tok}), db) == "admin"


def test_a_missing_header_counts_as_activity(auth):
    """Fail towards keeping someone signed in: a client that does not send the header must not log
    the operator out mid-action."""
    tok = _session(auth)
    _age(auth, tok, 29)
    with auth._db.SessionLocal() as db:
        assert auth.current_user(Req({auth.COOKIE: tok}, {}), db) == "admin"


def test_the_absolute_lifetime_still_applies(auth):
    """Activity must not extend a session for ever."""
    tok = _session(auth)
    with auth._db.SessionLocal() as db:
        row = db.get(auth._models.Session, auth._sha(tok))
        row.expires_at = auth._now() - dt.timedelta(seconds=1)
        row.last_seen_at = auth._now()       # active right now, but past its absolute end
        db.commit()
    with auth._db.SessionLocal() as db:
        assert auth.current_user(Req({auth.COOKIE: tok}), db) is None


def test_the_timeout_can_be_disabled(auth, monkeypatch):
    monkeypatch.setenv("DASH_IDLE_MINUTES", "0")
    importlib.reload(auth)
    assert auth.IDLE_TIMEOUT is None
    tok = _session(auth)
    _age(auth, tok, 600)
    with auth._db.SessionLocal() as db:
        assert auth.current_user(Req({auth.COOKIE: tok}), db) == "admin"


def test_the_dashboard_actually_sends_the_header():
    """Guards against the server enforcing it while the client never marks its polls — which would
    sign the operator out every 30 minutes no matter what they were doing."""
    from mallory_orchestrator import api
    html = (Path(api.__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    assert '"X-Poll": "1"' in html, "the dashboard never marks a poll"
    assert re.search(r"ACTIVE\s*=\s*true", html), "no input listener sets ACTIVE"
    assert "ACTIVE = false" in html, "ACTIVE is never consumed, so every request looks like activity"

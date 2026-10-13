"""Daily health-check harness for the Mallory crawler stack.

One runnable suite that exercises the LIVE deployment end to end - the four fetch
tiers, crawl coverage, the push/verify delta, the dashboards, and the control-plane
logic - so a single daily run tells you the crawler is healthy, and a failure points
straight at the subsystem that broke.

This is NOT the unit-test suite (crawler/tests, orchestrator/tests). Those prove the
code is correct in isolation; this proves the *running system* is doing its job. The
`logic` checks do fold the unit suites in, so one command covers both.

Design rules (do not break):
  * READ-ONLY against production. No writes to Postgres/MinIO, no restarts, no crawl
    dispatch. The only outbound traffic is a handful of canary fetches for the tiers.
  * NEVER select main_text/html from documents/documents_current - detoasting the
    TOAST columns over the whole corpus is a known outage. `pg()` REFUSES such SQL.
    Use text_len / html_len / coalesce(text_len, 0). A single-row detoast
    (WHERE document_id = %s LIMIT 1) is allowed via pg(..., allow_body=True).
  * Every check is cheap and bounded (timeouts everywhere) - this runs on a busy box.

A check is a zero-arg function registered with @check(area, name). It returns a short
detail string on success, raises Skip(reason) to skip (e.g. a tier that is disabled),
or raises anything else (AssertionError included) to fail. The runner catches all.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

# -- config (env-driven; defaults target the DC docker network) ----------------
def cfg(key: str, default: str) -> str:
    return os.environ.get(key, default).strip() or default

INGEST_BASE = cfg("HC_INGEST_BASE", cfg("INGEST_BASE", "http://ingest:9090")).rstrip("/")
ORCH_BASE = cfg("HC_ORCH_BASE", cfg("ORCH_BASE", "http://orchestrator:8090")).rstrip("/")
CRAWLER_API_BASE = cfg("HC_CRAWLER_API_BASE", cfg("CRAWLER_API_BASE", "http://crawler-api:8099")).rstrip("/")
CAMOFOX_URL = cfg("HC_CAMOFOX_URL", cfg("CAMOFOX_URL", "http://camofox:9377")).rstrip("/")
DATABASE_URL = cfg("HC_DATABASE_URL", cfg("DATABASE_URL", ""))
HTTP_TIMEOUT = float(cfg("HC_HTTP_TIMEOUT_S", "20"))
# How far back "recent" means for freshness/coverage checks.
RECENT_HOURS = float(cfg("HC_RECENT_HOURS", "48"))

# Canary URLs for the active tier probes (overridable so a dead canary is a one-liner fix).
CANARY_C1 = cfg("HC_CANARY_C1", "https://example.com/")
CANARY_C2 = cfg("HC_CANARY_C2", "https://www.gov.uk/")          # stable + well-archived
CANARY_C3 = cfg("HC_CANARY_C3", "https://www.google.com/")       # renders, harmless


class Skip(Exception):
    """Raise inside a check to mark it SKIP rather than FAIL (disabled feature, etc.)."""


@dataclass
class Result:
    area: str
    name: str
    status: str          # PASS | FAIL | SKIP
    detail: str
    seconds: float
    critical: bool = False


# -- registry ------------------------------------------------------------------
@dataclass
class _Check:
    area: str
    name: str
    fn: Callable[[], str]
    critical: bool


CHECKS: list[_Check] = []


def check(area: str, name: str, critical: bool = False):
    """Register a health check. `critical=True` means a failure fails the whole run
    (used for liveness: if the DB or ingest is down, downstream fails are noise)."""
    def deco(fn: Callable[[], str]) -> Callable[[], str]:
        CHECKS.append(_Check(area, name, fn, critical))
        return fn
    return deco


# -- HTTP helpers (stdlib only, so the harness needs no extra deps) -------------
_INSECURE = ssl.create_default_context()
_INSECURE.check_hostname = False
_INSECURE.verify_mode = ssl.CERT_NONE


def http_get(url: str, timeout: float | None = None, headers: dict | None = None,
             insecure: bool = False) -> tuple[int, str]:
    """GET -> (status, body_text). Raises on transport error (DNS, refused, timeout)."""
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "mallory-healthcheck"})
    ctx = _INSECURE if insecure else None
    t = HTTP_TIMEOUT if timeout is None else timeout
    try:
        with urllib.request.urlopen(req, timeout=t, context=ctx) as r:
            return r.getcode(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def http_json(url: str, timeout: float | None = None, insecure: bool = False):
    """GET a JSON endpoint -> parsed object. Fails loudly on non-200 or non-JSON."""
    status, body = http_get(url, timeout=timeout, insecure=insecure)
    if status != 200:
        raise AssertionError(f"{url} -> HTTP {status}: {body[:200]}")
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        raise AssertionError(f"{url} -> non-JSON ({e}): {body[:200]}")


def http_post(url: str, payload: dict | None = None, timeout: float | None = None):
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "mallory-healthcheck"})
    t = HTTP_TIMEOUT if timeout is None else timeout
    try:
        with urllib.request.urlopen(req, timeout=t) as r:
            body = r.read().decode("utf-8", "replace")
            return r.getcode(), body
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# -- Postgres helper with a HARD TOAST guard -----------------------------------
_BODY_COLS = re.compile(r"\bmain_text\b|\bhtml\b")
# strip the safe metadata columns before scanning, so html_len/html_bytes/text_len pass
_SAFE_TOKENS = re.compile(r"\b(html_len|html_bytes|text_len|main_text_len|has_html)\b")


def _toast_guard(sql: str) -> None:
    scrubbed = _SAFE_TOKENS.sub("", sql.lower())
    if _BODY_COLS.search(scrubbed):
        raise AssertionError(
            "TOAST GUARD: this query references main_text/html - selecting the TOAST "
            "body columns over the corpus is a known outage. Use text_len/html_len, "
            "or pg(..., allow_body=True) for a single-row WHERE document_id LIMIT 1.")


def pg(sql: str, params: tuple | None = None, timeout: float = 20.0,
       allow_body: bool = False):
    """Run a read-only query, return list[dict]. Refuses TOAST-body selects unless
    allow_body=True (reserved for single-row detoast). Requires psycopg + DATABASE_URL."""
    if not allow_body:
        _toast_guard(sql)
    if not DATABASE_URL:
        raise Skip("DATABASE_URL not set")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        raise Skip("psycopg not installed in this environment")
    url = DATABASE_URL
    if url.startswith("postgresql+"):
        url = "postgresql://" + url.split("://", 1)[1]
    ms = int(timeout * 1000)
    with psycopg.connect(url, autocommit=True, connect_timeout=int(timeout),
                         options=f"-c statement_timeout={ms}") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET default_transaction_read_only = on")
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []


def pg_scalar(sql: str, params: tuple | None = None, **kw):
    rows = pg(sql, params, **kw)
    if not rows:
        return None
    return next(iter(rows[0].values()))


def minio_client():
    """A read-only MinIO client from env, or Skip if unavailable."""
    try:
        from minio import Minio
    except ImportError:
        raise Skip("minio client not installed")
    ep = cfg("HC_MINIO_ENDPOINT", cfg("MINIO_ENDPOINT", ""))
    ak = cfg("HC_MINIO_ACCESS_KEY", cfg("MINIO_ACCESS_KEY", cfg("MINIO_ROOT_USER", "")))
    sk = cfg("HC_MINIO_SECRET_KEY", cfg("MINIO_SECRET_KEY", cfg("MINIO_ROOT_PASSWORD", "")))
    if not (ep and ak and sk):
        raise Skip("MinIO env (endpoint/key/secret) not set")
    secure = cfg("HC_MINIO_SECURE", "0") in ("1", "true", "True")
    from minio import Minio
    return Minio(ep.replace("http://", "").replace("https://", ""),
                 access_key=ak, secret_key=sk, secure=secure)


# -- assertions (thin wrappers so failures read cleanly in the report) ----------
def need(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def run_all(area_filter: str | None = None) -> list[Result]:
    results: list[Result] = []
    crit_down = False
    for c in CHECKS:
        if area_filter and c.area != area_filter:
            continue
        # once a critical liveness check fails, skip the rest of its area as noise
        t0 = time.monotonic()
        try:
            detail = c.fn() or "ok"
            status = "PASS"
        except Skip as s:
            detail, status = str(s) or "skipped", "SKIP"
        except Exception as e:  # noqa: BLE001 - a check must never crash the runner
            detail, status = f"{type(e).__name__}: {e}", "FAIL"
            if c.critical:
                crit_down = True
        results.append(Result(c.area, c.name, status, detail,
                              time.monotonic() - t0, c.critical))
    return results

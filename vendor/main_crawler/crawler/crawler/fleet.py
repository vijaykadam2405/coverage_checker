"""Fleet telemetry — every crawler machine reports itself to the data centre.

The crawler runs on N machines that all push their documents into ONE Postgres at the centre. This
module makes the fleet visible: each node upserts a row describing what it is doing right now, into
the same database its documents already go to.

WHY POSTGRES AND NOT AN HTTP ENDPOINT. The centre has no HTTP ingest — the crawler writes its
Postgres directly over an SSH tunnel, and the firewall only lets the crawler hosts reach 5432 at
all. Adding an HTTP service would mean a new port, a new firewall hole and a new auth scheme to get
wrong; a heartbeat row rides the connection that already exists and is already authenticated.

NOTHING HERE MAY EVER BREAK A CRAWL. Telemetry is the least important thing this process does: every
public function swallows its own errors and returns a status rather than raising, and a centre that
is down costs a log line, not a page. That is also why the writer keeps its own cooldown instead of
retrying in-line — a crawl must not spend its time reconnecting to a dashboard.
"""
from __future__ import annotations

import os
import socket
import threading
import time

_LOCK = threading.Lock()
_LAST_ERR: str = ""
_RETRY_AFTER = 0.0          # monotonic ts; skip connect attempts until then
_RETRY_COOLDOWN_S = 30.0
_STARTED_AT = time.time()

# Applied by the first successful heartbeat. IF NOT EXISTS throughout, so N nodes racing to create
# it on a fresh centre is a no-op rather than a crash, and an older centre self-migrates.
SCHEMA = """
CREATE TABLE IF NOT EXISTS fleet_nodes (
    node_id       TEXT PRIMARY KEY,
    hostname      TEXT,
    first_seen    TIMESTAMPTZ DEFAULT now(),
    last_seen     TIMESTAMPTZ,
    status        TEXT,          -- crawling | idle | stopping
    jobs_total    INTEGER,
    jobs_done     INTEGER,
    current_job   TEXT,
    pages         INTEGER,
    pages_per_sec REAL,
    fetched       INTEGER,
    kept          INTEGER,
    sent          INTEGER,
    accepted      INTEGER,
    cpu_percent   REAL,
    ram_percent   REAL,
    ram_used_gb   REAL,
    disk_percent  REAL,
    cpu_count     INTEGER,
    uptime_s      INTEGER,
    push_pending  INTEGER,       -- documents captured here that the centre has NOT accepted yet
    issues_total  INTEGER,
    agent_version TEXT,
    note          TEXT
);
-- A short rolling history, so the dashboard can draw a line rather than a single number. Trimmed by
-- the writer, not by a cron: whoever writes is the only thing guaranteed to be running.
CREATE TABLE IF NOT EXISTS fleet_samples (
    node_id       TEXT NOT NULL,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    status        TEXT,
    pages         INTEGER,
    pages_per_sec REAL,
    cpu_percent   REAL,
    ram_percent   REAL
);
CREATE INDEX IF NOT EXISTS fleet_samples_node_at_idx ON fleet_samples (node_id, at DESC);
"""

_UPSERT = """
INSERT INTO fleet_nodes (
    node_id, hostname, first_seen, last_seen, status, jobs_total, jobs_done, current_job,
    pages, pages_per_sec, fetched, kept, sent, accepted, cpu_percent, ram_percent, ram_used_gb,
    disk_percent, cpu_count, uptime_s, push_pending, issues_total, agent_version, note)
VALUES (%(node_id)s, %(hostname)s, now(), now(), %(status)s, %(jobs_total)s, %(jobs_done)s,
        %(current_job)s, %(pages)s, %(pages_per_sec)s, %(fetched)s, %(kept)s, %(sent)s,
        %(accepted)s, %(cpu_percent)s, %(ram_percent)s, %(ram_used_gb)s, %(disk_percent)s,
        %(cpu_count)s, %(uptime_s)s, %(push_pending)s, %(issues_total)s, %(agent_version)s,
        %(note)s)
ON CONFLICT (node_id) DO UPDATE SET
    hostname = EXCLUDED.hostname, last_seen = now(), status = EXCLUDED.status,
    jobs_total = EXCLUDED.jobs_total, jobs_done = EXCLUDED.jobs_done,
    current_job = EXCLUDED.current_job, pages = EXCLUDED.pages,
    pages_per_sec = EXCLUDED.pages_per_sec, fetched = EXCLUDED.fetched, kept = EXCLUDED.kept,
    sent = EXCLUDED.sent, accepted = EXCLUDED.accepted, cpu_percent = EXCLUDED.cpu_percent,
    ram_percent = EXCLUDED.ram_percent, ram_used_gb = EXCLUDED.ram_used_gb,
    disk_percent = EXCLUDED.disk_percent, cpu_count = EXCLUDED.cpu_count,
    uptime_s = EXCLUDED.uptime_s, push_pending = EXCLUDED.push_pending,
    issues_total = EXCLUDED.issues_total, agent_version = EXCLUDED.agent_version,
    note = EXCLUDED.note
-- first_seen is deliberately NOT updated: it is when this machine joined the fleet, and a restart
-- must not make a long-running node look brand new.
"""


def node_id() -> str:
    """Stable identity for this machine.

    MALLORY_NODE_ID wins so an operator can name a box something meaningful ("gpu-01", "office-nuc")
    instead of whatever the OS calls it — and, importantly, so two machines that happen to share a
    hostname (identical VM images, containers with a generated name) do not collapse into one row
    and overwrite each other's status forever.
    """
    v = (os.environ.get("MALLORY_NODE_ID") or "").strip()
    if v:
        return v[:64]
    try:
        return (socket.gethostname() or "unknown")[:64]
    except Exception:
        return "unknown"


def centre_dsn() -> str:
    """Where heartbeats go: the centre if configured, else the local store.

    Falling back to DATABASE_URL is what makes the dashboard work for a ONE-machine setup too —
    the same page, reading the same table, just with a fleet of one.
    """
    return (os.environ.get("FLEET_DSN")
            or os.environ.get("DATA_CENTRE_DSN")
            or os.environ.get("DATABASE_URL") or "")


def _norm(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg://", "postgresql://").replace("postgres://",
                                                                        "postgresql://")


def uptime_s() -> int:
    return int(time.time() - _STARTED_AT)


def build_payload(metrics: dict, *, push_pending: int | None = None,
                  agent_version: str = "", note: str = "") -> dict:
    """Turn a /v1/metrics dict into a heartbeat row.

    Kept separate from the write so it can be tested without a database, and so the caller decides
    what "status" means rather than this module guessing from a half-populated dict.
    """
    b = metrics.get("batch") or {}
    p = metrics.get("progress") or {}
    t = metrics.get("totals") or {}
    ram = metrics.get("ram") or {}
    disk = metrics.get("disk") or {}
    running = bool(b.get("running"))
    status = "stopping" if (running and b.get("stopping")) else ("crawling" if running else "idle")
    return {
        "node_id": node_id(),
        "hostname": socket.gethostname()[:128] if hasattr(socket, "gethostname") else "",
        "status": status,
        "jobs_total": int(b.get("total") or 0),
        "jobs_done": int(b.get("done") or 0),
        "current_job": str(b.get("current") or "")[:200],
        "pages": int(p.get("pages") or 0),
        "pages_per_sec": float(p.get("pages_per_sec") or 0.0),
        "fetched": int(t.get("fetched") or 0),
        "kept": int(t.get("kept") or 0),
        "sent": int(t.get("sent") or 0),
        "accepted": int(t.get("accepted") or 0),
        "cpu_percent": float(metrics.get("cpu_percent") or 0.0),
        "cpu_count": int(metrics.get("cpu_count") or 0),
        "ram_percent": float(ram.get("percent") or 0.0),
        "ram_used_gb": float(ram.get("used_gb") or 0.0),
        "disk_percent": float(disk.get("percent") or 0.0),
        "uptime_s": uptime_s(),
        "push_pending": int(push_pending) if push_pending is not None else None,
        "issues_total": int((metrics.get("issues") or {}).get("total") or 0),
        "agent_version": agent_version[:40],
        "note": note[:200],
    }


def send(payload: dict, dsn: str = "", *, keep_samples: int = 720) -> dict:
    """Write one heartbeat. NEVER raises.

    Returns {"ok": bool, "error": str} so a caller can surface the state without having to catch
    anything. On failure it sets a cooldown, because a crawl must not spend its time reconnecting
    to a dashboard — the next heartbeat simply skips until the cooldown expires.
    """
    global _RETRY_AFTER, _LAST_ERR
    dsn = _norm(dsn or centre_dsn())
    if not dsn:
        return {"ok": False, "error": "no dsn configured", "skipped": True}
    now = time.monotonic()
    with _LOCK:
        if now < _RETRY_AFTER:
            return {"ok": False, "error": _LAST_ERR, "cooling_down": True}
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
                cur.execute(_UPSERT, payload)
                cur.execute(
                    "INSERT INTO fleet_samples (node_id, status, pages, pages_per_sec, "
                    "cpu_percent, ram_percent) VALUES (%s,%s,%s,%s,%s,%s)",
                    (payload["node_id"], payload["status"], payload["pages"],
                     payload["pages_per_sec"], payload["cpu_percent"], payload["ram_percent"]))
                # Trim THIS node's history only. A global DELETE would make every node fight over
                # the same rows, and a node that is offline would have its history deleted by
                # whoever is still running — losing exactly the history you want after an outage.
                cur.execute(
                    "DELETE FROM fleet_samples WHERE node_id = %s AND at < ("
                    "  SELECT min(at) FROM (SELECT at FROM fleet_samples WHERE node_id = %s "
                    "  ORDER BY at DESC LIMIT %s) keep)",
                    (payload["node_id"], payload["node_id"], int(keep_samples)))
            conn.commit()
        with _LOCK:
            _RETRY_AFTER, _LAST_ERR = 0.0, ""
        return {"ok": True, "error": ""}
    except Exception as exc:                                  # noqa: BLE001
        detail = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else type(exc).__name__
        with _LOCK:
            _RETRY_AFTER = time.monotonic() + _RETRY_COOLDOWN_S
            _LAST_ERR = detail
        return {"ok": False, "error": detail}


def last_error() -> str:
    with _LOCK:
        return _LAST_ERR


if __name__ == "__main__":     # tiny self-check: no DB needed
    m = {"batch": {"running": True, "total": 3, "done": 1, "current": "job_a"},
         "progress": {"pages": 42, "pages_per_sec": 1.5},
         "totals": {"fetched": 40, "kept": 30, "sent": 30, "accepted": 29},
         "cpu_percent": 12.5, "cpu_count": 8,
         "ram": {"percent": 40.0, "used_gb": 12.0}, "disk": {"percent": 55.0},
         "issues": {"total": 2}}
    p = build_payload(m, push_pending=7, agent_version="test")
    assert p["status"] == "crawling" and p["jobs_total"] == 3 and p["pages"] == 42
    assert p["push_pending"] == 7 and p["issues_total"] == 2
    m["batch"]["stopping"] = True
    assert build_payload(m)["status"] == "stopping"
    m["batch"]["running"] = False
    assert build_payload(m)["status"] == "idle"
    assert send(p, dsn="")["skipped"] is True          # unconfigured is not an error path
    assert send(p, dsn="postgresql://nobody@127.0.0.1:1/none")["ok"] is False   # never raises
    print("fleet self-check OK — node_id:", node_id())

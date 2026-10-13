"""Fleet telemetry: node identity, heartbeat payloads, and the promise that none of it can hurt a crawl.

The load-bearing property here is NEGATIVE — a dead data centre, a wrong DSN or a network partition
must cost a log line, never a page. So most of this asserts what does NOT happen.

Runnable directly:  python tests/test_fleet.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import fleet  # noqa: E402

METRICS = {
    "batch": {"running": True, "total": 5, "done": 2, "current": "job_rtx", "stopping": False},
    "progress": {"pages": 120, "pages_per_sec": 3.25},
    "totals": {"fetched": 118, "kept": 90, "sent": 90, "accepted": 88},
    "cpu_percent": 42.0, "cpu_count": 24,
    "ram": {"percent": 61.0, "used_gb": 20.4}, "disk": {"percent": 33.0},
    "issues": {"total": 4},
}


def test_node_id_prefers_the_operators_name(monkeypatch):
    """Two machines built from the same VM image share a hostname. Without an explicit id they
    would collapse onto one row and overwrite each other's status forever."""
    monkeypatch.setenv("MALLORY_NODE_ID", "gpu-01")
    assert fleet.node_id() == "gpu-01"
    monkeypatch.delenv("MALLORY_NODE_ID")
    assert fleet.node_id() and fleet.node_id() != "gpu-01"      # falls back to hostname
    monkeypatch.setenv("MALLORY_NODE_ID", "x" * 200)
    assert len(fleet.node_id()) == 64                            # bounded, never unbounded text


def test_status_is_derived_not_guessed():
    assert fleet.build_payload(METRICS)["status"] == "crawling"
    m = {**METRICS, "batch": {**METRICS["batch"], "stopping": True}}
    assert fleet.build_payload(m)["status"] == "stopping"
    m = {**METRICS, "batch": {**METRICS["batch"], "running": False}}
    assert fleet.build_payload(m)["status"] == "idle"


def test_payload_carries_what_the_dashboard_asks_for():
    p = fleet.build_payload(METRICS, push_pending=13, agent_version="0.1.0")
    assert (p["jobs_total"], p["jobs_done"], p["current_job"]) == (5, 2, "job_rtx")
    assert (p["pages"], round(p["pages_per_sec"], 2)) == (120, 3.25)
    assert (p["fetched"], p["kept"], p["sent"], p["accepted"]) == (118, 90, 90, 88)
    assert (p["cpu_percent"], p["ram_percent"], p["disk_percent"]) == (42.0, 61.0, 33.0)
    assert p["push_pending"] == 13 and p["issues_total"] == 4
    assert p["uptime_s"] >= 0


def test_a_never_reported_field_stays_none_not_zero():
    """The dashboard renders None as "—". Coercing it to 0 would make "this machine has told us
    nothing" indistinguishable from "this machine reports zero", which is the exact lie the
    no-invented-numbers rule exists to prevent."""
    p = fleet.build_payload(METRICS)                    # push_pending not supplied
    assert p["push_pending"] is None
    assert fleet.build_payload({})["pages"] == 0        # ...but a PRESENT-but-empty dict is a real 0


def test_an_empty_payload_never_explodes():
    """Heartbeats run on a timer against whatever /v1/metrics returned, including mid-startup when
    half the keys do not exist yet."""
    p = fleet.build_payload({})
    assert p["status"] == "idle" and p["node_id"]
    for k in ("jobs_total", "jobs_done", "pages", "fetched", "kept", "cpu_percent"):
        assert p[k] is not None, k


def test_send_never_raises_and_never_blocks_forever(monkeypatch):
    """The whole contract. An unreachable centre returns a status; it does not raise, and it does
    not keep retrying inline — the next heartbeat is skipped until the cooldown expires."""
    monkeypatch.setattr(fleet, "_RETRY_AFTER", 0.0)
    p = fleet.build_payload(METRICS)

    assert fleet.send(p, dsn="")["skipped"] is True                    # unconfigured != broken
    # Port 1 on localhost: refused immediately, so this stays fast.
    r = fleet.send(p, dsn="postgresql://nobody:x@127.0.0.1:1/none")
    assert r["ok"] is False and r["error"]
    # ...and the cooldown is now armed, so the next call does not even try to connect.
    r2 = fleet.send(p, dsn="postgresql://nobody:x@127.0.0.1:1/none")
    assert r2["ok"] is False and r2.get("cooling_down") is True
    monkeypatch.setattr(fleet, "_RETRY_AFTER", 0.0)                    # leave no state behind


def test_centre_dsn_falls_back_so_one_machine_still_gets_a_dashboard(monkeypatch):
    for k in ("FLEET_DSN", "DATA_CENTRE_DSN", "DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    assert fleet.centre_dsn() == ""
    monkeypatch.setenv("DATABASE_URL", "postgresql://local/db")
    assert fleet.centre_dsn() == "postgresql://local/db"
    monkeypatch.setenv("DATA_CENTRE_DSN", "postgresql://centre/db")
    assert fleet.centre_dsn() == "postgresql://centre/db"      # the centre wins over local
    monkeypatch.setenv("FLEET_DSN", "postgresql://explicit/db")
    assert fleet.centre_dsn() == "postgresql://explicit/db"    # ...and an explicit override wins


def test_schema_is_idempotent_and_upsert_preserves_first_seen():
    """N machines race to create the table on a fresh centre; every statement must tolerate that.
    And first_seen must survive a restart, or a long-running node looks brand new after a reboot."""
    assert fleet.SCHEMA.count("IF NOT EXISTS") >= 3
    assert "ON CONFLICT (node_id) DO UPDATE" in fleet._UPSERT
    assert "first_seen = EXCLUDED" not in fleet._UPSERT
    assert "last_seen = now()" in fleet._UPSERT


def test_documents_carry_the_capturing_machine():
    """Which machine produced a page has to be answerable from the corpus, not just from a live
    heartbeat — a node can be long gone by the time anyone asks."""
    from ingest_api import pgstore
    assert "node_id" in pgstore._SCHEMA and "documents_node_idx" in pgstore._SCHEMA
    src = (Path(__file__).resolve().parents[1] / "ingest_api" / "pgstore.py").read_text(
        encoding="utf-8")
    assert '"html_len", "node_id"' in src, "node_id is not in the INSERT column list"
    assert "len(_COLS) + 4" in src, "placeholder count was not widened with the new column"
    # ...and it has to survive the trip to the centre, or the centre groups everything as unknown.
    live = (Path(__file__).resolve().parents[1] / "ingest_api" / "live.py").read_text(
        encoding="utf-8")
    assert '"crawl_run_id", "node_id")' in live, "the push does not carry node_id"


if __name__ == "__main__":
    import types

    class _MP:
        def __init__(self): self._undo = []
        def setenv(self, k, v):
            import os
            self._undo.append((k, os.environ.get(k))); os.environ[k] = v
        def delenv(self, k, raising=True):
            import os
            self._undo.append((k, os.environ.get(k))); os.environ.pop(k, None)
        def setattr(self, obj, name, val):
            old = getattr(obj, name); self._undo.append((obj, name, old)); setattr(obj, name, val)
        def undo(self):
            import os
            for item in reversed(self._undo):
                if len(item) == 2:
                    k, v = item
                    if v is None: os.environ.pop(k, None)
                    else: os.environ[k] = v
                else:
                    obj, name, old = item; setattr(obj, name, old)

    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and isinstance(v, types.FunctionType)]
    for fn in fns:
        mp = _MP()
        try:
            fn(mp) if fn.__code__.co_argcount else fn()
            print(f"  ok  {fn.__name__}")
        finally:
            mp.undo()
    print(f"all {len(fns)} fleet tests passed")

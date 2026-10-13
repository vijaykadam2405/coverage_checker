"""Background crawl/harvest control for the SIPRI dashboard's Start button.

One run at a time, process-wide. The button is the kind of thing an operator clicks twice when the
page feels slow, and two concurrent crawls would double the request rate against a server we are
deliberately going easy on — so a second start is refused, not queued.

State lives in memory: this is a control surface for a long-running job, not a job queue. If the
service restarts mid-crawl the run is gone, which is honest — the crawl died with it. Progress that
matters is already durable in Postgres/MinIO as each page lands.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading

log = logging.getLogger("sipri.runner")

_LOCK = threading.Lock()
_STATE: dict = {
    "running": False,
    "task": None,          # what is running: crawl | harvest
    "target": None,
    "started_at": None,
    "finished_at": None,
    "stop_requested": False,
    "stats": {},
    "error": None,
    "log": [],             # last N lines, for the dashboard
}
_STOP = threading.Event()
_MAX_LOG = 200


def _now() -> str:
    return dt.datetime.now(tz=dt.timezone.utc).isoformat(timespec="seconds")


def note(line: str) -> None:
    with _LOCK:
        _STATE["log"].append(f"{_now()} {line}")
        del _STATE["log"][:-_MAX_LOG]        # keep the tail bounded; this runs for hours


def state() -> dict:
    with _LOCK:
        s = dict(_STATE)
        s["log"] = list(s["log"])[-40:]
        return s


def request_stop() -> bool:
    """Cooperative stop. The crawler checks this between items, so it finishes the page in flight
    rather than abandoning a half-written document."""
    if not _STATE["running"]:
        return False
    _STOP.set()
    with _LOCK:
        _STATE["stop_requested"] = True
    note("stop requested — finishing the item in flight")
    return True


def start(task: str = "all", target: str | None = None) -> tuple[bool, str]:
    """Kick off a background run. Returns (started, message)."""
    with _LOCK:
        if _STATE["running"]:
            return False, f"already running ({_STATE['task']}); stop it first"
        _STOP.clear()
        _STATE.update({"running": True, "task": task, "target": target,
                       "started_at": _now(), "finished_at": None,
                       "stop_requested": False, "stats": {}, "error": None, "log": []})
    threading.Thread(target=_run, args=(task, target), daemon=True).start()
    return True, f"started {task}"


def _run(task: str, target: str | None) -> None:
    from . import store
    try:
        note(f"start {task}" + (f" target={target}" if target else ""))
        if not store.ensure_schema():
            note("WARNING: schema/bucket unavailable — results will NOT persist")

        results: dict = {}
        if task in ("all", "harvest"):
            results.update(_harvest(store))
        if task in ("all", "crawl"):
            results.update(_crawl(store, target))

        with _LOCK:
            _STATE["stats"] = results
        note(f"done: {results}")
    except Exception as exc:                      # noqa: BLE001 — a thread that dies silently is worse
        log.exception("sipri run failed")
        with _LOCK:
            _STATE["error"] = str(exc)[:300]
        note(f"FAILED: {exc}")
    finally:
        with _LOCK:
            _STATE["running"] = False
            _STATE["finished_at"] = _now()


def _harvest(store) -> dict:
    """API + spreadsheets + reference data. Fast (~20 requests) — run before the long crawl so the
    dashboard has real numbers within a minute of pressing Start."""
    from . import datasets, reference, transfers
    out = {}
    note("harvest: arms transfers (public JSON API)")
    out["transfers"] = transfers.harvest(store)
    note("harvest: countries + armaments")
    out["countries"] = reference.harvest_countries(store)
    out["armaments"] = reference.harvest_armaments(store)
    note("harvest: spreadsheet datasets")
    out["datasets"] = [datasets.harvest_file_dataset(e, store) for e in _entries()]
    return out


def _entries() -> list:
    from . import config
    try:
        from . import discover
        return discover.refresh_registry()
    except Exception:                             # noqa: BLE001 — pinned URLs are the fallback
        return list(config.FILE_DATASETS)


def _crawl(store, target: str | None) -> dict:
    """Full C3 site crawl into the SIPRI schema. Hours, not minutes."""
    from special_request_crawler import crawl as sr

    from .sink import SipriSink
    sink = SipriSink(store)
    targets = sr.load_targets()
    if target:
        targets = [t for t in targets if t.get("name") == target]
    out = {}
    for t in targets:
        if _STOP.is_set():
            note("stopped before " + t["name"])
            break
        note(f"crawl {t['name']}: max_pages={t.get('max_pages')} depth={t.get('max_depth')}")
        stop_ev = asyncio.Event()

        async def _go(tt=t, ev=stop_ev):
            # Bridge the threading.Event the API sets to the asyncio.Event the crawler polls.
            async def watch():
                while not ev.is_set():
                    if _STOP.is_set():
                        ev.set()
                        return
                    await asyncio.sleep(1)
            w = asyncio.create_task(watch())
            try:
                return await sr.crawl_target(tt, ingest=sink, stop=ev)
            finally:
                w.cancel()

        stats = asyncio.run(_go())
        out[t["name"]] = stats.as_dict() if hasattr(stats, "as_dict") else stats
        note(f"crawl {t['name']} done: {out[t['name']]}")
    out["sink"] = sink.stats()
    return out

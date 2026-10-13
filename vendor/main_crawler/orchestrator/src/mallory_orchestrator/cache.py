"""Last-known-good cache for the panel's slow endpoints.

Measured on the live stack, a cold page load waits on:

    /api/logs/sites        38.4s   (waits on the crawler's batch report across 7 instances)
    /api/data/categories   20.5s   (ingest source rollup joined against the catalogue)
    /api/data/summary      10.6s   (ingest live summary)

The panel already renders each panel as it lands and already guards against stacking requests, so
none of that is a client bug - the numbers simply are not available for half a minute. The fix is
to stop making the reader wait for a number the server computed four minutes ago:

    * a HIT inside the TTL is returned as-is;
    * a STALE hit is returned IMMEDIATELY and a background refresh is started, so the next poll
      (the panel already has 30s and 90s timers) picks the fresh value up on its own;
    * a MISS computes synchronously, because there is nothing better to show than the truth.

Two rules this cache does not break.

**A stale number must never look live.** Every dict value carries `_cache: {as_of, age_s, stale,
refreshing}` and the panel prints the age. Serving four-minute-old counts is fine; serving them as
if they were measured now is the exact class of lie the "only real data" rule exists to stop.

**A failure never overwrites nothing, and never silently becomes a value.** If the refresh raises,
the previous value is kept and `_cache.error` says why - so a dead ingest reads as "last known,
6 minutes ago, refresh failed" instead of either a blank panel or an invented zero. A handler that
returns `{"reachable": false}` is cached verbatim, because that IS the honest current answer.

Values survive a restart in the `app_settings` kv table, so the first page load after a deploy is
also instant. Large payloads stay in memory only (see MAX_PERSIST_BYTES) - the point of the row is
a warm start, not a second copy of the corpus.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading

from .db import SessionLocal
from .models import Setting

log = logging.getLogger("mallory.cache")

PREFIX = "cache:"
# Above this, keep the value in memory only. /api/logs/sites at limit=500 is a few hundred KB and
# rewriting that row every refresh would make the control-plane DB churn for no reader.
MAX_PERSIST_BYTES = 256 * 1024

_LOCK = threading.Lock()
_MEM: dict[str, dict] = {}          # key -> {"value": ..., "at": datetime, "error": str|None}
_INFLIGHT: set[str] = set()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


# -- persistence ---------------------------------------------------------------------------------

def _load(key: str) -> dict | None:
    """Warm the in-memory entry from the kv row. Called once per key per process."""
    try:
        with SessionLocal() as db:
            row = db.get(Setting, PREFIX + key)
            if row is None or not row.value:
                return None
            blob = json.loads(row.value)
            return {"value": blob["value"],
                    "at": dt.datetime.fromisoformat(blob["at"]),
                    "error": None}
    except Exception:  # noqa: BLE001 - a warm start is an optimisation, never a failure mode
        log.debug("cache: could not load %s", key, exc_info=True)
        return None


def _persist(key: str, value, at: dt.datetime) -> None:
    try:
        blob = json.dumps({"value": value, "at": at.isoformat()})
    except (TypeError, ValueError):
        return                      # not JSON-able: memory-only, no complaint needed
    if len(blob) > MAX_PERSIST_BYTES:
        return
    try:
        with SessionLocal() as db:
            row = db.get(Setting, PREFIX + key)
            if row is None:
                db.add(Setting(key=PREFIX + key, value=blob))
            else:
                row.value = blob
            db.commit()
    except Exception:  # noqa: BLE001
        log.debug("cache: could not persist %s", key, exc_info=True)


# -- the cache -----------------------------------------------------------------------------------

def _entry(key: str) -> dict | None:
    with _LOCK:
        e = _MEM.get(key)
    if e is not None:
        return e
    e = _load(key)
    if e is not None:
        with _LOCK:
            _MEM.setdefault(key, e)
        return e
    return None


def _store(key: str, value, error: str | None = None) -> dict:
    e = {"value": value, "at": _now(), "error": error}
    with _LOCK:
        _MEM[key] = e
    _persist(key, value, e["at"])
    return e


def _compute(key: str, fn):
    """Run fn with its OWN session. The background refresher has no request scope, and reusing the
    request's session across threads is how SQLAlchemy sessions corrupt."""
    with SessionLocal() as db:
        return fn(db)


def _refresh_bg(key: str, fn) -> None:
    def run():
        try:
            _store(key, _compute(key, fn))
        except Exception as e:  # noqa: BLE001 - keep the last good value, say why it is old
            with _LOCK:
                old = _MEM.get(key)
            if old is not None:
                old["error"] = str(e)[:200]
            log.warning("cache: refresh of %s failed: %s", key, str(e)[:200])
        finally:
            with _LOCK:
                _INFLIGHT.discard(key)

    with _LOCK:
        if key in _INFLIGHT:
            return                   # single-flight: one refresh per key, never a stampede
        _INFLIGHT.add(key)
    threading.Thread(target=run, name=f"cache-{key}", daemon=True).start()


def _meta(value, e: dict, ttl_s: float, refreshing: bool):
    """Attach cache provenance, without changing the value's shape.

    Only dicts get the stamp: the panel's renderers read lists positionally, and wrapping one would
    break every caller for the sake of a timestamp nobody could see.
    """
    if not isinstance(value, dict):
        return value
    age = (_now() - e["at"]).total_seconds()
    out = dict(value)
    out["_cache"] = {"as_of": e["at"].isoformat() + "Z", "age_s": round(age, 1),
                     "stale": age >= ttl_s, "refreshing": refreshing,
                     "error": e.get("error")}
    return out


def serve(key: str, fn, ttl_s: float, *, force: bool = False):
    """Cached value for `key`, computing it with `fn(db)` when needed.

    `fn` takes a Session and returns a JSON-able value. It is called on a MISS (synchronously, in
    the caller's thread) and on a stale HIT (in a background thread), never concurrently with
    itself for the same key.
    """
    if force:
        return _meta_or_value(_store(key, _compute(key, fn)), ttl_s, False)
    e = _entry(key)
    if e is None:
        return _meta_or_value(_store(key, _compute(key, fn)), ttl_s, False)
    age = (_now() - e["at"]).total_seconds()
    refreshing = False
    if age >= ttl_s:
        _refresh_bg(key, fn)
        refreshing = True
    return _meta(e["value"], e, ttl_s, refreshing)


def _meta_or_value(e: dict, ttl_s: float, refreshing: bool):
    return _meta(e["value"], e, ttl_s, refreshing)


def peek() -> list[dict]:
    """What the cache is holding, for the panel's own diagnostics."""
    with _LOCK:
        items = list(_MEM.items())
        inflight = set(_INFLIGHT)
    now = _now()
    return sorted(({"key": k, "as_of": e["at"].isoformat() + "Z",
                    "age_s": round((now - e["at"]).total_seconds(), 1),
                    "refreshing": k in inflight, "error": e.get("error")}
                   for k, e in items), key=lambda r: r["key"])


def clear(key: str | None = None) -> int:
    """Drop cached values (memory AND the warm-start rows). Returns how many went."""
    with _LOCK:
        keys = [key] if key else list(_MEM.keys())
        for k in keys:
            _MEM.pop(k, None)
    try:
        with SessionLocal() as db:
            rows = ([db.get(Setting, PREFIX + key)] if key else
                    [r for r in db.query(Setting).filter(Setting.key.like(PREFIX + "%")).all()])
            n = 0
            for r in rows:
                if r is not None:
                    db.delete(r)
                    n += 1
            db.commit()
            return n
    except Exception:  # noqa: BLE001
        return len(keys)


def demo() -> None:
    """Self-check: hit, stale-serve-then-refresh, single-flight, and a failed refresh keeping the
    last good value. Run: python -m mallory_orchestrator.cache"""
    import time

    calls = {"n": 0}

    def slow(db):
        calls["n"] += 1
        return {"v": calls["n"]}

    k = "__demo__"
    clear(k)
    first = serve(k, slow, ttl_s=60)
    assert first["v"] == 1 and calls["n"] == 1, first
    assert first["_cache"]["stale"] is False

    # a fresh hit does not recompute
    assert serve(k, slow, ttl_s=60)["v"] == 1 and calls["n"] == 1

    # stale: the OLD value comes back instantly, and a refresh is started
    stale = serve(k, slow, ttl_s=0)
    assert stale["v"] == 1, stale                      # served immediately, not recomputed inline
    assert stale["_cache"]["stale"] is True and stale["_cache"]["refreshing"] is True
    for _ in range(50):
        if calls["n"] == 2:
            break
        time.sleep(0.05)
    assert calls["n"] == 2, "background refresh never ran"
    assert serve(k, slow, ttl_s=60)["v"] == 2, "refreshed value was not stored"

    # Wait for the refresher to actually finish. Not politeness: while the key is still in flight
    # the next stale serve is single-flighted away, so testing the failure path before this point
    # tests the dedupe instead - which is how this check passed a bug on its first run.
    for _ in range(50):
        if k not in _INFLIGHT:
            break
        time.sleep(0.05)
    assert k not in _INFLIGHT

    # a failing refresh keeps the last good value and says why it is old
    def boom(db):
        raise RuntimeError("ingest down")

    out = serve(k, boom, ttl_s=0)
    assert out["v"] == 2, out
    for _ in range(50):
        if _MEM[k].get("error"):
            break
        time.sleep(0.05)
    assert "ingest down" in (_MEM[k]["error"] or ""), _MEM[k]
    assert serve(k, slow, ttl_s=60)["v"] == 2, "a failed refresh must not drop the value"

    # a non-dict value is passed through untouched (no _cache key to break list renderers)
    clear("__demo_list__")
    assert serve("__demo_list__", lambda db: [1, 2, 3], ttl_s=60) == [1, 2, 3]

    clear(k)
    clear("__demo_list__")
    print("cache demo ok")


if __name__ == "__main__":
    demo()

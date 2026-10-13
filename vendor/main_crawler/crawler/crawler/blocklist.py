"""Hosts this crawler must never fetch, however it hears about them.

WHY DELETING A SITE WAS NOT ENOUGH. `nordkurier.de` (a German regional newspaper) and `deki.dk` were
deleted from every store that holds them — Postgres, MinIO, the schedules, and all twelve durable
frontiers — and both came back: 16 and 7 pages, one of them first seen SIX HOURS after the purge.
They are in no catalogue, no shard table, no schedule and no job definition. They return because a
CATALOGUED site links to them or redirects into them, so the engine discovers them as ordinary
children and enqueues them under whatever job was walking at the time.

That makes "delete the data" structurally unable to hold. The decision "this site is not part of the
corpus" has to live somewhere the ENQUEUE path can see, or every purge is temporary and the operator
does it again next week.

Deliberately a FILE in the data dir, not a table or an env var:
  * it is per-instance and survives a container recreate, like the frontier beside it;
  * it can be edited on a live crawler with no rebuild and no restart (re-read when it changes);
  * `crawler_api` has no orchestrator DB access, and an env var cannot be appended to at runtime.

Format: one host per line. `#` comments and blank lines ignored. A `www.` prefix is stripped, and a
listed domain covers its subdomains — `deki.dk` blocks `shop.deki.dk`, because the whole point is
that a site does not come back wearing a different hostname.
"""
from __future__ import annotations

import logging
import os
import threading
from urllib.parse import urlsplit

log = logging.getLogger("blocklist")

FILENAME = "blocked_hosts.txt"

_lock = threading.Lock()
_hosts: frozenset[str] = frozenset()
# (mtime, size), not mtime alone: filesystem mtime granularity is coarse enough that an append can
# land in the same tick as the read that cached it, and the change would then be invisible until the
# next write. Size catches exactly that case.
_stamp: tuple[float, int] | None = None
_path_cache: str | None = None


def path() -> str:
    """Resolved at call time, not import time: tests and per-instance containers both set
    CRAWLER_DATA_DIR, and an import-time snapshot would bind the wrong directory."""
    from crawler import config
    return os.path.join(str(config.DATA_DIR), FILENAME)


def _norm(host: str) -> str:
    return host.strip().lower().rstrip(".").removeprefix("www.")


def _load() -> frozenset[str]:
    """Re-read only when the file changes. This runs on EVERY discovered link, so it must not
    stat-and-parse per call under load."""
    global _hosts, _stamp, _path_cache
    p = path()
    try:
        st = os.stat(p)
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        # No file is the normal case — an empty blocklist, not an error.
        with _lock:
            if _path_cache != p or _stamp is not None:
                _hosts, _stamp, _path_cache = frozenset(), None, p
        return _hosts
    if stamp == _stamp and p == _path_cache:
        return _hosts
    out = set()
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.split("#", 1)[0]
                h = _norm(line)
                if h:
                    out.add(h)
    except OSError as e:
        log.warning("blocklist unreadable at %s: %s", p, e)
        return _hosts
    with _lock:
        _hosts, _stamp, _path_cache = frozenset(out), stamp, p
    log.info("blocklist loaded: %d host(s) from %s", len(out), p)
    return _hosts


def blocked_host(host: str) -> str | None:
    """The blocklist entry that covers `host`, or None. Walks parent-wards so a subdomain cannot
    slip past an entry for its registered domain."""
    h = _norm(host)
    if not h:
        return None
    hosts = _load()
    if not hosts:
        return None
    if h in hosts:
        return h
    parts = h.split(".")
    for i in range(1, len(parts) - 1):
        parent = ".".join(parts[i:])
        if parent in hosts:
            return parent
    return None


def blocked(url: str) -> str | None:
    """The blocklist entry that covers this URL's host, or None."""
    try:
        return blocked_host(urlsplit(url).netloc)
    except Exception:  # noqa: BLE001 — a malformed URL is not the blocklist's problem
        return None


def add(hosts: list[str] | str, reason: str = "") -> int:
    """Append hosts to the file, skipping ones already covered. Returns how many were added.

    Used by the purge so "delete this site" and "do not crawl it again" are one action rather than
    two, the second of which gets forgotten.

    A bare string is accepted as one host. Without this, `add("kia.com")` iterates the CHARACTERS
    and writes one entry per letter — which is silent, because single-character entries can never
    match a real host (blocked_host only ever compares whole labels). The file fills with junk, the
    site is not blocked, and the return value still looks like a success. Measured: 27 junk entries
    written across twelve instances before anything noticed.
    """
    if isinstance(hosts, str):
        hosts = [hosts]
    p = path()
    existing = set(_load())
    new = [_norm(h) for h in hosts if _norm(h) and not blocked_host(h)]
    if not new:
        return 0
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        if not existing:
            fh.write("# Hosts this crawler must never fetch. One per line; # comments ignored.\n"
                     "# A listed domain also covers its subdomains.\n")
        for h in new:
            fh.write(f"{h}{('  # ' + reason) if reason else ''}\n")
    # Invalidate rather than trust the stamp: this process just wrote the file, so it must not be at
    # the mercy of the filesystem's timestamp resolution to observe its own change.
    global _stamp
    with _lock:
        _stamp = None
    _load()
    return len(new)

"""Fixture registry — reproducible offline content for the test batch.

Build decision: *live fetch with fixtures fallback*. Many defence sources
(Janes, MoD portals) are paywalled, JS-gated, or block bots, so the §8 test
batch ships with realistic fixtures keyed by canonical URL. The fetcher serves
a fixture when one exists (or when offline); otherwise it goes to the network.

``tests/fixtures/index.json`` maps canonical_url -> fixture metadata:
    { "file": "lt_k9.html", "content_type": "text/html",
      "etag": "\"abc\"", "last_modified": "...", "published": "2026-06-28" }
"""
from __future__ import annotations

import functools
import json

from . import config
from .canonicalize import canonicalize_url


@functools.lru_cache(maxsize=1)
def _index() -> dict:
    idx = config.FIXTURES_DIR / "index.json"
    if not idx.exists():
        return {}
    with open(idx, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    # Re-key by canonical URL so lookups match the fetcher's canonical form.
    return {canonicalize_url(k): v for k, v in raw.items()}


def has(url: str) -> bool:
    return canonicalize_url(url) in _index()


def get(url: str) -> tuple[bytes, dict] | None:
    """Return (raw_bytes, meta) for a fixture URL, or None if not a fixture."""
    meta = _index().get(canonicalize_url(url))
    if not meta:
        return None
    path = config.FIXTURES_DIR / meta["file"]
    return path.read_bytes(), meta

"""Clean main text + content hash (the heart of the L1 capture output).

``main_text`` is the raw body dump — Layer 2 runs all NLP on it. We extract the
article body with trafilatura (drops nav/ads/comments), fall back to crude visible
text, and hash the *normalized main text* (not raw HTML) so rotating ads/nav/
timestamps never trigger a false "changed" on re-crawl (§7A).
"""
from __future__ import annotations

import hashlib
import re

from . import parse

_WS = re.compile(r"\s+")


def _safe(text: str) -> str:
    """Scrub NULs / surrogate junk that breaks downstream JSON / DB writes."""
    if not text:
        return ""
    return text.replace("\x00", "").encode("utf-8", "ignore").decode("utf-8")


def main_text(html: str) -> str:
    """Boilerplate-stripped article body; falls back to full visible text."""
    try:
        import trafilatura
        out = trafilatura.extract(html, include_comments=False,
                                  include_tables=True, no_fallback=False)
        if out and out.strip():
            return _safe(out.strip())
    except Exception:
        pass
    return _safe(parse.visible_text(html))


def normalize_for_hash(text: str) -> str:
    return _WS.sub(" ", (text or "").strip().lower())


def content_hash(text: str) -> str | None:
    """``sha256:...`` of the normalized main text, or None if empty."""
    norm = normalize_for_hash(text)
    if not norm:
        return None
    return "sha256:" + hashlib.sha256(norm.encode("utf-8")).hexdigest()

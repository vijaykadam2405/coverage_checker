"""Mechanical date parsing — page publish-date metadata only.

Deep field extraction (money/quantity/deadline/spec parsing for typed
records) has moved to Layer 2, which operates on the raw main_text/html this
crawler hands over. This module keeps only date parsing, since
``published_at``/``date_precision`` are page metadata, not record fields.

Never guesses: an unparseable value returns ``None`` (the contract's "Don't
fabricate. Unknown = null" rule).
"""
from __future__ import annotations

import re
from datetime import datetime


def parse_date(raw: str | None) -> tuple[str | None, str]:
    """Return (ISO-8601 string | None, precision). precision ∈ exact|approx|unknown."""
    if not raw:
        return None, "unknown"
    raw = raw.strip()
    try:
        from dateutil import parser as dparser
        dt = dparser.parse(raw, fuzzy=True, default=datetime(2000, 1, 1))
        # If the parser had to borrow day/month from the default, call it approx.
        had_day = bool(re.search(r"\b\d{1,2}\b", raw))
        iso = dt.strftime("%Y-%m-%dT00:00:00Z")
        return iso, ("exact" if had_day else "approx")
    except Exception:
        return None, "unknown"

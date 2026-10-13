"""Page-bundle acceptance rules as a pure, unit-testable function.

L1 sends one raw page bundle (a ``document``) per kept page — no separately
typed records. A bundle is accepted only if:
  1. It has non-empty ``main_text``.
  2. It has a non-empty canonical ``url``.
  3. It has a ``content_hash`` (not the empty-content sentinel).
  4. ``published_at``, if present, parses as ISO.

(The old "resolves to a seed entity" rule is redundant to re-check here now —
the crawl-time gate already enforces exactly this before a page is ever
sent, so L1 physically cannot send a non-resolving, non-tender page.)

Returns ``(accepted, failing_rule | None)``. The API turns a failing rule into
``422 {failing_rule}``.
"""
from __future__ import annotations

from datetime import datetime


def _is_iso(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        # Accept date-only too (YYYY-MM-DD).
        try:
            datetime.strptime(value[:10], "%Y-%m-%d")
            return True
        except ValueError:
            return False


def validate_page(document: dict) -> tuple[bool, str | None]:
    if not document:
        return False, "rule1_missing_document"
    if not (document.get("main_text") or "").strip():
        return False, "rule1_empty_main_text"
    if not (document.get("url") or "").strip():
        return False, "rule1_missing_canonical_url"
    ch = document.get("content_hash") or ""
    if not ch or ch == "sha256:empty":
        return False, "rule1_missing_content_hash"

    # Second line of defence: never STORE a WAF wall as a document. The crawler now rewrites a
    # block body to a 403 before it gets here, but that guard lives in one code path — and 703 of
    # these (CloudFront "403 ERROR / Request blocked" from lockheedmartin, boeing, mbda) reached
    # Postgres and were shipped to L2, which dutifully analysed the error page as if it were
    # Lockheed content. Checking it at the storage boundary too means no future crawler path,
    # replay, or backfill can reintroduce them.
    from crawler import errors
    if errors.is_ip_block_page(document.get("main_text") or ""):
        return False, "rule1_block_page"

    pub = document.get("published_at")
    if pub is not None and not _is_iso(pub):
        return False, "rule4_bad_date:document.published_at"

    return True, None

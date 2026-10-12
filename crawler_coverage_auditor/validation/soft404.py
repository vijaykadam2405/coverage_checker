from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PageValidity:
    status: str
    reason: str


ERROR_PHRASES = ("page not found", "no results found", "nothing found", "does not exist")


def assess_page_validity(status_code: int, text: str, *, item_count: int | None = None,
                         repeats_known_page: bool = False) -> PageValidity:
    """Avoid treating a successful HTTP status as proof of a real page."""
    lowered = text.lower()
    if status_code == 0 or status_code >= 400:
        return PageValidity("HTTP_ERROR", f"HTTP {status_code}")
    if repeats_known_page:
        return PageValidity("REPEATED_LAST_PAGE", "content fingerprint repeats a known page")
    if (len(text) < 800 and any(phrase in lowered for phrase in ERROR_PHRASES)) or lowered.strip() in {"404", "404 not found", "not found"}:
        return PageValidity("SOFT_404", "page contains an error/no-results phrase")
    if item_count == 0:
        return PageValidity("EMPTY_RESULT_PAGE", "listing exposes no result items")
    return PageValidity("VALID_PAGE", "valid response with non-error content")

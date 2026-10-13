"""Harvest the SIPRI Arms Transfers Database through its public JSON API.

The UI flow the operator sees — pick filters, submit, download a CSV — is a Svelte SPA over
POST /api/p/trades/search. Driving the API directly gets the same rows as typed JSON, with no form
state, no CSV re-parsing, and no browser. 29,916 records at pageSize 2000 is ~15 requests.

Pagination is offset-based and the API does NOT return a total in the page, so the loop stops on a
short page. We also read getTotal up front purely as a cross-check: if the two disagree by more than
a page, we say so rather than quietly banking a partial pull as complete.
"""
from __future__ import annotations

import logging

from . import config
from .fetcher import post_json

log = logging.getLogger("sipri.transfers")


def reported_total() -> int | None:
    """The server's own record count, used to verify the pull was complete.

    GET, not POST: /trades/getTotal answers 405 to a POST, and MAX_RETRIES turned that into three
    wasted round trips against a server we are deliberately going easy on. Only the /search family
    takes POST.
    """
    try:
        return int(_get_total_via_get())
    except (TypeError, ValueError):
        return None


def _get_total_via_get() -> object | None:
    from .fetcher import get
    r = get(f"{config.API_BASE}{config.TRADES_TOTAL}", headers=config.API_HEADERS)
    if not r:
        return None
    body, _ct = r
    return body.decode("utf-8", "replace").strip()


def fetch_page(page_no: int, page_size: int | None = None) -> list[dict] | None:
    """One page of transfer records, or None if the request failed."""
    size = page_size or config.PAGE_SIZE
    rows = post_json(config.TRADES_SEARCH, config.search_body(page_no, size))
    if rows is None:
        return None
    if not isinstance(rows, list):
        log.warning("unexpected search payload: %.120s", rows)
        return None
    return rows


def harvest(store=None, *, max_pages: int | None = None,
            page_size: int | None = None) -> dict:
    """Pull every arms-transfer record, writing to `store` page by page.

    Writes incrementally rather than accumulating 30k rows and storing once: a failure at page 12
    should keep the first 11 pages, not throw the whole pull away.
    """
    size = page_size or config.PAGE_SIZE
    expected = reported_total()
    seen_ids: set[int] = set()
    written = failed_pages = fetched_pages = 0
    page = 0

    while True:
        if max_pages is not None and page >= max_pages:
            break
        rows = fetch_page(page, size)
        if rows is None:
            failed_pages += 1
            log.warning("page %d failed", page)
            break                                    # offset pagination: a gap would corrupt the set
        if not rows:
            break
        fetched_pages += 1
        # The API can repeat rows across pages if the underlying order is unstable; dedupe on id so
        # a wobble inflates neither the count nor the table.
        fresh = [r for r in rows if r.get("id") not in seen_ids]
        seen_ids.update(r.get("id") for r in fresh)
        if store is not None and fresh:
            written += store.upsert_transfers(fresh) or 0
        log.info("page %d: %d rows (%d new, %d total)", page, len(rows), len(fresh), len(seen_ids))
        if len(rows) < size:
            break                                    # short page = last page
        page += 1

    got = len(seen_ids)
    complete = expected is not None and got >= expected
    if expected is not None and not complete:
        # Stated plainly: a partial pull reported as success is how stale data becomes invisible.
        log.warning("INCOMPLETE: got %d of %d reported records", got, expected)
    return {"dataset": "arms_transfers", "records": got, "written": written,
            "reported_total": expected, "complete": complete,
            "pages": fetched_pages, "failed_pages": failed_pages}

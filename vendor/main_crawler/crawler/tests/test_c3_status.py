"""C3 must honour the server's HTTP status, and a repeated 404 must eventually be given up on.

The bug this pins: CamoFox's REST API reports only {tabId, url} on navigation, so the C3 path
hardcoded status=200. A 404 renders into perfectly good HTML, so its error page was stored as a
real document — and because no failure was ever recorded, dedup.is_gone() could never fire and the
same dead URL was re-fetched on every run. Measured on a live store: 19.7% of everything C3 stored
was error pages or interstitials, against 0.2% for C1.

Runnable directly:  python tests/test_c3_status.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import errors                       # noqa: E402
from crawler.dedup import CrawlHistory           # noqa: E402


def t_error_status_maps_to_a_recordable_reason() -> None:
    assert errors.http_reason(404) == "http_404"
    assert errors.policy_for_status(404) == errors.GONE
    assert errors.policy_for_status(410) == errors.GONE
    # A 403 is NOT gone — the page exists, we were refused. It must stay retryable/escalatable.
    assert errors.policy_for_status(403) != errors.GONE


def t_a_repeated_404_is_eventually_given_up_on() -> None:
    """The operator's question: 'if a page returns 404 again and again it means it's gone?' — yes,
    but only if someone records the 404. This is that chain end to end."""
    h = CrawlHistory(str(Path(tempfile.mkdtemp()) / "h.sqlite"))
    url = "https://www.sncorp.com/dead-page"

    assert not h.is_gone(url), "an unseen URL must not be pre-emptively skipped"
    h.record_failure(url, status=404, category="http_404", failed_at="2026-07-22T00:00:00Z")
    assert not h.is_gone(url), \
        "one 404 must NOT be fatal — a deploy blip would otherwise kill a live URL forever"
    h.record_failure(url, status=404, category="http_404", failed_at="2026-07-22T01:00:00Z")
    assert h.is_gone(url), "a URL that 404s twice is still being re-crawled"


def t_410_is_permanent_on_the_first_strike() -> None:
    """410 Gone is the server's explicit word, not an inference — one is enough."""
    h = CrawlHistory(str(Path(tempfile.mkdtemp()) / "h2.sqlite"))
    url = "https://example.test/removed"
    h.record_failure(url, status=410, category="http_410", failed_at="2026-07-22T00:00:00Z")
    assert h.is_gone(url), "an explicit 410 was not honoured"


def t_a_200_is_never_marked_gone() -> None:
    """The failure mode on the other side: over-eager quarantine loses real pages."""
    h = CrawlHistory(str(Path(tempfile.mkdtemp()) / "h3.sqlite"))
    url = "https://example.test/alive"
    for _ in range(5):
        h.upsert(url, content_hash="sha256:x", etag=None, last_modified=None,
                 status=200, fetched_at="2026-07-22T00:00:00Z")
    assert not h.is_gone(url), "a healthy page was quarantined"


def t_a_success_resets_the_strike_count() -> None:
    """A URL that 404s once during a deploy and then comes back must not carry the strike."""
    h = CrawlHistory(str(Path(tempfile.mkdtemp()) / "h4.sqlite"))
    url = "https://example.test/flaky"
    h.record_failure(url, status=404, category="http_404", failed_at="2026-07-22T00:00:00Z")
    h.upsert(url, content_hash="sha256:y", etag=None, last_modified=None,
             status=200, fetched_at="2026-07-22T01:00:00Z")   # upsert zeroes fail_count
    h.record_failure(url, status=404, category="http_404", failed_at="2026-07-22T02:00:00Z")
    assert not h.is_gone(url), \
        "strikes survived a successful fetch — an intermittent 404 would eventually kill a live URL"


def t_render_result_carries_a_status_field() -> None:
    """The contract the engine now reads. If render() stops returning `status`, the engine silently
    reverts to trusting every render, so pin the key itself."""
    import inspect

    from crawler import camofox_client
    src = inspect.getsource(camofox_client.render)
    assert '"status": status' in src, "render() no longer returns the real HTTP status"
    assert "responseStatus" in src, "the navigation-timing status probe is gone"
    # An unknown status must stay None rather than defaulting to 200 — see the engine's guard.
    assert "100 <= int(v) <= 599" in src, "the status sanity range is gone"


def main() -> None:
    for fn in (t_error_status_maps_to_a_recordable_reason,
               t_a_repeated_404_is_eventually_given_up_on,
               t_410_is_permanent_on_the_first_strike,
               t_a_200_is_never_marked_gone,
               t_a_success_resets_the_strike_count,
               t_render_result_carries_a_status_field):
        fn()
        print(f"  ok  {fn.__name__}")
    print("ALL C3 STATUS TESTS PASSED")



def test_c3_status_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()

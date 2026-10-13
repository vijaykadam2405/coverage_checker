"""A crashed render must fall back, retry, and eventually stop.

The 2,616 render_crash rows were not 2,616 broken pages. The detail column showed three distinct
things wearing one label:

    net::ERR_ABORTED at .../media/1119/download   the URL serves a PDF; the browser is the wrong tool
    net::ERR_SOCKET_NOT_CONNECTED                 a transport blip, classified as NOTHING at all
    Timeout ...ms exceeded                        a genuinely slow render

Before this, a crash returned a failure without trying any other way to fetch the page, retried once
on a constant 30s timer, and — if it never got a status — was retried on every run forever.

Runnable directly:  python tests/test_render_fallback.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae  # noqa: E402
from crawler import errors  # noqa: E402
from crawler.dedup import CrawlHistory  # noqa: E402


def t_download_urls_never_reach_the_browser() -> None:
    """Chromium aborts navigation on a download, which read as a crashed render."""
    for u in ("https://x.test/en/media/1119/download", "https://x.test/spec.pdf",
              "https://x.test/files/report.xlsx", "https://x.test/downloads/manual.docx"):
        assert ae._looks_like_download(u), f"{u} was not recognised as a file"
    # ...and a real page must not be diverted just because the word appears in a query string.
    for u in ("https://x.test/news/article", "https://x.test/p?download=1",
              "https://x.test/download-centre", "https://x.test/"):
        assert not ae._looks_like_download(u), f"{u} was wrongly treated as a file"


def t_an_aborted_navigation_is_recognised_as_a_download_not_a_crash() -> None:
    assert errors.is_download_abort("nav:Page.goto: net::ERR_ABORTED at https://x.test/dl")
    assert not errors.is_download_abort("render:Timeout 30000ms exceeded")
    assert not errors.is_download_abort(None)


def t_socket_errors_are_classified_instead_of_vanishing() -> None:
    """These fell through classify_failure to an empty category: not retried, not quarantined, not
    recorded. Five zf.com URLs were silently dropped that way."""
    for e in ("nav:net::ERR_SOCKET_NOT_CONNECTED", "net::ERR_NETWORK_CHANGED",
              "net::ERR_ADDRESS_UNREACHABLE", "net::ERR_CONNECTION_ABORTED"):
        got = errors.classify_failure(None, e)
        assert got == errors.CONN_REFUSED, f"{e} -> {got!r}"
        assert got in errors._RETRYABLE_REASONS, f"{e} classified but not retryable"


def t_retry_backoff_is_exponential_and_jittered() -> None:
    """A constant delay reschedules every URL that failed together onto the same second, which
    recreates the pile-up that broke them. Read the formula rather than the clock."""
    import inspect
    src = inspect.getsource(ae.AsyncEngine._on_failure)
    assert "2 ** item.retries" in src, "backoff is no longer exponential"
    assert "random.uniform(0.5, 1.5)" in src, "the jitter is gone"
    assert 'CRAWLER_INRUN_RETRIES", 3' in src, "retry budget dropped back to 1 attempt"


def t_the_fallback_ladder_is_ordered_cheapest_first() -> None:
    import inspect
    src = inspect.getsource(ae.AsyncEngine._render_fallback)
    i_dl = src.index("is_download_abort")
    i_reuse = src.index("An httpx body we already paid for")
    i_fresh = src.index("httpx was never tried")
    assert i_dl < i_reuse < i_fresh, "the fallback rungs are out of cost order"
    # C3 must NOT be CALLED here: _try_camofox_fallback sends the document itself, so returning
    # anything from this function would store the page twice. Match the call, not the word — the
    # comment explaining this deliberately names the function.
    assert "await self._try_camofox_fallback" not in src, \
        "C3 is invoked in _render_fallback — the document would be stored twice"


def t_render_crash_escalates_to_the_other_browser() -> None:
    """The 4xx ladder is keyed on a status and a crash has none, so without an explicit branch
    render_crash never escalated to C3 at all."""
    import inspect
    src = inspect.getsource(ae.AsyncEngine._on_failure)
    assert "reason == errors.RENDER_CRASH" in src and "_try_camofox_fallback" in src
    assert "return None, tab" in src, "a C3 recovery must end the item, not fall through and store"


def t_retry_forever_terminates_on_a_poison_url() -> None:
    """'Retry until it works' has to stop for a URL that can never work — otherwise it costs a
    browser tab, every run, indefinitely."""
    h = CrawlHistory(str(Path(tempfile.mkdtemp()) / "h.sqlite"))
    u = "https://x.test/crashes"
    for i in range(7):
        h.record_failure(u, status=None, category="render_crash", failed_at=f"t{i}")
        assert not h.is_gone(u), f"quarantined after only {i+1} attempts — too eager"
    h.record_failure(u, status=None, category="render_crash", failed_at="t8")
    assert h.is_gone(u), "a URL that failed 8 times without ever answering is still being retried"


def t_a_url_that_answers_is_never_poisoned() -> None:
    """Quarantine requires last_status IS NULL. Anything that reached the server — even a 500 —
    keeps being retried on its own policy, because that is a server problem, not a dead URL."""
    h = CrawlHistory(str(Path(tempfile.mkdtemp()) / "h2.sqlite"))
    u = "https://x.test/flaky-500"
    for i in range(15):
        h.record_failure(u, status=500, category="http_500", failed_at=f"t{i}")
    assert not h.is_gone(u), "a 500 was quarantined as poison"


def t_a_success_clears_the_poison_count() -> None:
    h = CrawlHistory(str(Path(tempfile.mkdtemp()) / "h3.sqlite"))
    u = "https://x.test/recovers"
    for i in range(9):
        h.record_failure(u, status=None, category="render_crash", failed_at=f"t{i}")
    assert h.is_gone(u)
    h.upsert(u, content_hash="sha256:x", etag=None, last_modified=None,
             status=200, fetched_at="t10")            # upsert zeroes fail_count
    assert not h.is_gone(u), "a page that came back is still quarantined"


def t_a_recovered_url_stops_showing_its_old_error() -> None:
    """upsert cleared error_category but not error_detail, so a page that fetched fine at 200 still
    displayed the error from its previous run — the dashboard would show a healthy URL as broken."""
    h = CrawlHistory(str(Path(tempfile.mkdtemp()) / "h4.sqlite"))
    u = "https://x.test/recovered"
    h.record_failure(u, status=None, category="render_crash", failed_at="t1",
                     detail="nav:net::ERR_SOCKET_NOT_CONNECTED")
    h.upsert(u, content_hash="sha256:x", etag=None, last_modified=None, status=200, fetched_at="t2")
    row = h._conn.execute(
        "SELECT error_category, error_detail FROM crawl_pages WHERE canonical_url=?",
        (u,)).fetchone()
    assert row[0] is None, "error_category survived a success"
    assert row[1] is None, f"stale error text survived a success: {row[1]!r}"


def t_a_404_is_never_tagged_as_needing_a_different_network() -> None:
    """"Needs a different network path" means an IP/egress block. A 404 is not that: the page does
    not exist and no VPN, proxy or residential exit will conjure it. Measured before this guard,
    2,024 of 4,111 needs_network_path rows (49%) were plain 404s — so the "sites we cannot reach"
    queue was double its real size, and half of it was unfixable by definition."""
    import inspect
    src = inspect.getsource(ae.AsyncEngine._tag_netpath)
    assert "policy_for_status(status) == errors.GONE" in src, "the GONE guard is gone"
    i_guard, i_tag = src.index("policy_for_status"), src.index("NEEDS_NETWORK_PATH")
    assert i_guard < i_tag, "the netpath tag is applied before the GONE check"
    # ...and a real block must still be tagged.
    assert errors.policy_for_status(403) != errors.GONE
    assert errors.policy_for_status(404) == errors.GONE
    assert errors.policy_for_status(410) == errors.GONE


def main() -> None:
    for fn in (t_download_urls_never_reach_the_browser,
               t_an_aborted_navigation_is_recognised_as_a_download_not_a_crash,
               t_socket_errors_are_classified_instead_of_vanishing,
               t_retry_backoff_is_exponential_and_jittered,
               t_the_fallback_ladder_is_ordered_cheapest_first,
               t_render_crash_escalates_to_the_other_browser,
               t_retry_forever_terminates_on_a_poison_url,
               t_a_url_that_answers_is_never_poisoned,
               t_a_success_clears_the_poison_count,
               t_a_recovered_url_stops_showing_its_old_error,
               t_a_404_is_never_tagged_as_needing_a_different_network):
        fn()
        print(f"  ok  {fn.__name__}")
    print("ALL RENDER-FALLBACK TESTS PASSED")



def test_render_fallback_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()

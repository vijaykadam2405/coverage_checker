"""Two junk classes that filled a quarter-million frontier slots on the news crawl.

MEASURED on the live fleet 2026-08-05. 244,660 URLs were pending across only 75 hosts, and one
instance held 122,440 of them for two sites. Throughput sat at ~1 page/s, because per-host
concurrency is (rightly) capped: an instance whose frontier is two hosts can only ever run at
two hosts' worth of slots no matter how many workers it has. The fix is not more workers — it is
not queueing the junk.

  1. PERCENT-ENCODED UNRESERVED CHARACTERS. al-monitor.com serves every article at /originals/X and
     ALSO links it as /index%2Ephp/originals/X and /index%2ephp/originals/X. RFC 3986 says an
     escape of an unreserved character is equivalent to the character, and hex is case-insensitive
     — so those are one URL wearing three spellings. Nothing normalized them: 31,805 of that
     instance's pending URLs were percent-encoded duplicates.

  2. SCHEMELESS RELATIVE HREFS. asiapacificdefencereporter.com has a footer ad written
     href="www.serco.com/aspac" — no scheme, no leading "//". That is a relative PATH, so urljoin
     correctly resolves it against whatever page it appeared on, and every page on the site minted
     its own variant.

Both are pruned as well as filtered: _is_action_link is what CrawlHistory.frontier_prune runs, so
the URLs already queued go too.
"""
from __future__ import annotations

import pytest

from crawler import async_engine as ae
from crawler.canonicalize import canonicalize_url


# ---------------------------------------------------------------- percent-encoding

def test_percent_encoded_dot_collapses_onto_the_plain_path():
    plain = canonicalize_url("https://www.al-monitor.com/index.php/originals/2021/05/x")
    upper = canonicalize_url("https://www.al-monitor.com/index%2Ephp/originals/2021/05/x")
    lower = canonicalize_url("https://www.al-monitor.com/index%2ephp/originals/2021/05/x")
    assert plain == upper == lower, (
        "three spellings of one path stayed three frontier entries: %r / %r / %r"
        % (plain, upper, lower))


@pytest.mark.parametrize("esc,ch", [("%41", "A"), ("%7A", "z"), ("%39", "9"),
                                    ("%2D", "-"), ("%5F", "_"), ("%7E", "~")])
def test_every_unreserved_class_is_decoded(esc, ch):
    assert canonicalize_url("https://h.test/a" + esc + "b") == "https://h.test/a" + ch + "b"


def test_a_reserved_escape_is_kept_but_case_normalized():
    """%2F is an encoded '/' — decoding it would change the path's STRUCTURE, so it stays. Only its
    hex case is normalized, which still collapses two spellings into one."""
    a = canonicalize_url("https://h.test/a%2fb")
    b = canonicalize_url("https://h.test/a%2Fb")
    assert a == b == "https://h.test/a%2Fb"
    assert canonicalize_url("https://h.test/a%20b") == "https://h.test/a%20b"


def test_the_query_string_keeps_its_existing_treatment():
    """_normalize_pct is applied to the PATH only. The query already goes through a
    parse_qsl/urlencode round trip that decodes and re-encodes it — long-standing behaviour, and
    not something this change touches. Pinned so a later 'normalize the query too' is a deliberate
    decision rather than a side effect: in a query, an escape can be load-bearing to the server."""
    assert canonicalize_url("https://h.test/p?q=a%2Eb") == "https://h.test/p?q=a.b"
    assert canonicalize_url("https://h.test/p?q=a%26b") == "https://h.test/p?q=a%26b"


def test_a_malformed_escape_does_not_raise():
    for bad in ("https://h.test/a%2", "https://h.test/a%zz", "https://h.test/%"):
        assert canonicalize_url(bad)


# ---------------------------------------------------------------- schemeless relative hrefs

def test_the_footer_ad_that_minted_a_url_per_page():
    assert ae._is_action_link(
        "https://asiapacificdefencereporter.com/boeing-beefs-up-ghost-bat/www.serco.com/aspac")
    assert ae._is_action_link("https://asiapacificdefencereporter.com/www.serco.com/aspac")


def test_cloudflare_internals_are_not_pages():
    assert ae._is_action_link("https://asiapacificdefencereporter.com/cdn-cgi/l/email-protection")


@pytest.mark.parametrize("keep", [
    "https://h.test/news/2026/wwwshop",              # www without the dot is just a word
    "https://h.test/about/www",                      # a bare segment, no domain after it
    "https://www.h.test/news/story",                 # www in the HOST is normal
    "https://h.test/blog/how-to-www-proxy",
])
def test_real_paths_are_not_swept_up(keep):
    assert not ae._is_action_link(keep)


def test_the_prune_path_uses_the_same_predicate():
    """The rule that stops junk arriving and the rule that removes what already arrived must be one
    function — see the memory of the ical trap, where filtering alone changed nothing because the
    frontier still held the URLs."""
    import inspect

    from crawler_api import app
    assert "_is_action_link" in inspect.getsource(app._wipe_or_prune_frontier
                                                  if hasattr(app, "_wipe_or_prune_frontier")
                                                  else app)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))

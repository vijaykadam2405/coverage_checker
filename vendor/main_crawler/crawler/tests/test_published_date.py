"""The publication date a news page actually states.

Before this, the crawler read a few meta tags and then fell back to the FIRST
<time> tag anywhere on the page. On a news article that is usually a sidebar
item or an event listing, which is how the corpus acquired publication dates in
the future -- an article fetched in August claiming 30 December.

Run standalone:  python crawler/tests/test_published_date.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from crawler.crawler.parse import extract_meta  # noqa: E402

TODAY = datetime.date.today()
YESTERDAY = (TODAY - datetime.timedelta(days=1)).isoformat()
NEXT_YEAR = (TODAY + datetime.timedelta(days=200)).isoformat()


def pub(html, url=None):
    return extract_meta(html, url).get("published_raw")


def test_jsonld_is_preferred():
    """JSON-LD is where modern news CMSs put the authoritative date."""
    html = """<html><head>
      <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"NewsArticle",
       "datePublished":"%s","headline":"x"}
      </script>
      <meta property="article:published_time" content="2019-01-01">
      </head><body><time datetime="2001-02-03">old</time></body></html>""" % YESTERDAY
    assert pub(html) == YESTERDAY, pub(html)


def test_jsonld_graph_is_walked():
    html = """<html><head><script type="application/ld+json">
      {"@graph":[{"@type":"WebSite"},
                 {"@type":"NewsArticle","datePublished":"%s"}]}
      </script></head><body></body></html>""" % YESTERDAY
    assert pub(html) == YESTERDAY, pub(html)


def test_broken_jsonld_does_not_lose_the_page():
    """Publishers emit invalid JSON-LD often; it must not cost the date."""
    html = """<html><head>
      <script type="application/ld+json">{ this is not json </script>
      <meta property="article:published_time" content="%s">
      </head><body></body></html>""" % YESTERDAY
    assert pub(html) == YESTERDAY, pub(html)


def test_future_date_is_refused_everywhere():
    """A publication date in the future is a misparse, not news."""
    html = """<html><head>
      <script type="application/ld+json">
      {"@type":"NewsArticle","datePublished":"%s"}</script>
      <meta property="article:published_time" content="%s">
      </head><body><time datetime="%s">soon</time></body></html>""" % (
        NEXT_YEAR, NEXT_YEAR, NEXT_YEAR)
    assert pub(html) is None, "a future date was accepted: %r" % pub(html)


def test_sidebar_time_is_not_the_article_date():
    """THE regression: the first <time> on the page belonged to a sidebar."""
    html = """<html><body>
      <aside><ul><li><time datetime="%s">upcoming expo</time></li></ul></aside>
      <article><time itemprop="datePublished" datetime="%s">today</time>
        <p>body</p></article>
      </body></html>""" % (NEXT_YEAR, YESTERDAY)
    assert pub(html) == YESTERDAY, pub(html)


def test_time_inside_article_beats_one_outside():
    html = """<html><body>
      <div class="promo"><time datetime="2001-01-01">promo</time></div>
      <article><time datetime="%s">real</time></article>
      </body></html>""" % YESTERDAY
    assert pub(html) == YESTERDAY, pub(html)


def test_url_path_date_is_the_last_resort():
    html = "<html><body><p>no date anywhere</p></body></html>"
    url = "https://example.com/2026/03/09/some-defence-story"
    got = pub(html, url)
    assert got == "2026-03-09", got


def test_url_date_in_the_future_is_refused():
    y = TODAY.year + 3
    html = "<html><body></body></html>"
    assert pub(html, "https://example.com/%d/05/06/x" % y) is None


def test_no_date_is_none_not_a_guess():
    html = "<html><body><p>nothing here</p></body></html>"
    assert pub(html, "https://example.com/story") is None


def test_non_iso_formats_still_pass_through():
    """The future check reads only leading ISO; other formats are handed on
    unchanged for the real parser rather than dropped."""
    html = """<html><head><meta name="pubdate" content="12 March 2026">
      </head><body></body></html>"""
    assert pub(html) == "12 March 2026", pub(html)


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("  ok    %s" % name)
            except AssertionError as exc:
                fails += 1
                print("  FAIL  %s -- %s" % (name, exc))
    print("")
    print("%d failure(s)" % fails)
    sys.exit(1 if fails else 0)

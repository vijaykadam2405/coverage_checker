"""A JSON listing has no ``<a>``, so every /api/ endpoint the crawler touched paged exactly once.

Measured across the edge-case corpus: 10 of the 15 pagination failures were one shape — the pager
was sitting in the response body as data (``meta.next_page``, ``links.next``, ``nextCursorMark``,
or plain ``?page=``/``?offset=`` arithmetic) and nothing in the crawler could read it.

Two properties are load-bearing here and both are tested in BOTH directions:

  * **Bounded.** An increment is emitted only when the body shows there IS another page. npm's
    search API silently CLAMPS ``?from=`` past the end and re-serves page 1 for ever, and GBIF's
    ``endOfRecords`` never turns true — either one is an endless frontier without the bound test.
  * **Not a self-link.** Crossref re-issues the SAME cursor token every hop, so substituting it
    yields the URL we are already on. To a frontier that dedups on URL that is not a new page, it
    is churn, so it must yield nothing at all.

Runnable directly:  python tests/test_json_pager.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import parse  # noqa: E402
from crawler.async_engine import _looks_paginated  # noqa: E402
from crawler.canonicalize import canonicalize_url  # noqa: E402


def _j(obj) -> str:
    return json.dumps(obj)


def t_an_explicit_next_url_is_followed_wherever_it_sits() -> None:
    """crates.io puts a RELATIVE next_page in meta; DataCite puts an ABSOLUTE one in links."""
    got = parse.links_from_json(
        _j({"crates": [{"id": 1}], "meta": {"next_page": "?page=2&per_page=10"}}),
        "https://crates.io/api/v1/crates?page=1&per_page=10")
    assert got == ["https://crates.io/api/v1/crates?page=2&per_page=10"], got

    got = parse.links_from_json(
        _j({"data": [{"id": "a"}], "links": {"next": "https://api.datacite.org/dois?page%5Bcursor%5D=2"}}),
        "https://api.datacite.org/dois?page%5Bcursor%5D=1")
    assert got == ["https://api.datacite.org/dois?page%5Bcursor%5D=2"], got


def t_a_cursor_token_is_substituted_into_the_param_the_request_already_names() -> None:
    got = parse.links_from_json(
        _j({"results": [{"id": 1}], "meta": {"next_cursor": "IlsxNjk5XSI="}}),
        "https://api.openalex.org/works?cursor=*&per-page=25")
    assert got == ["https://api.openalex.org/works?cursor=IlsxNjk5XSI%3D&per-page=25"], got


def t_a_cursor_that_never_changes_is_not_a_next_page() -> None:
    """Crossref re-issues one token for a whole scroll. Substituting it produces the URL we are
    standing on, and enqueuing that is churn, not progress."""
    same = "AoJ41eSFm-QCPwhodHRwOi8v"
    got = parse.links_from_json(
        _j({"message": {"items": [{"DOI": "x"}], "next-cursor": same}}),
        f"https://api.crossref.org/works?cursor={same}&rows=20")
    assert got == [], got


def t_arithmetic_stops_at_the_bound_the_body_publishes() -> None:
    """The endless-frontier guard, in every form the corpus produced."""
    # Algolia publishes nbPages; page nbPages is the empty one past the end.
    mid = parse.links_from_json(_j({"hits": [{"t": 1}], "page": 3, "nbPages": 10, "nbHits": 97}),
                                "https://hn.algolia.com/api/v1/search?query=python&page=3")
    assert mid == ["https://hn.algolia.com/api/v1/search?query=python&page=4"], mid
    last = parse.links_from_json(_j({"hits": [{"t": 1}], "page": 9, "nbPages": 10}),
                                 "https://hn.algolia.com/api/v1/search?query=python&page=9")
    assert last == [], "walked past the last page a body said existed"

    # npm CLAMPS ?from= past the end and re-serves page 1 for ever.
    clamped = parse.links_from_json(_j({"objects": [{"p": 1}], "total": 40}),
                                    "https://registry.npmjs.org/-/v1/search?text=x&size=20&from=20")
    assert clamped == [], "kept incrementing an offset the body says is already at the end"

    # GBIF's endOfRecords, and the has_more family that means the opposite.
    assert parse.links_from_json(_j({"results": [{"k": 1}], "endOfRecords": True, "offset": 20,
                                     "limit": 20}),
                                 "https://api.gbif.org/v1/occurrence/search?offset=20&limit=20") == []
    assert parse.links_from_json(_j({"quotes": [{"q": 1}], "has_next": False}),
                                 "https://quotes.toscrape.com/api/quotes?page=10") == []

    # An empty result array is the end of the road however the envelope is worded.
    assert parse.links_from_json(_j({"results": [], "page": 2}),
                                 "https://x.example/api?page=2") == []


def t_html_quoted_inside_json_is_never_mined_for_links() -> None:
    """extract_links used to run BeautifulSoup over an API body, which happily lifted <a href> out
    of markup QUOTED INSIDE the JSON — measured on Algolia page 3 of ?query=python, where one HN
    comment produced two invented URLs under the API's own host. Nothing links to those."""
    body = _j({"hits": [{"comment_text": '<a href="https://pynecone.io">pynecone</a>'}],
               "page": 0, "nbPages": 5})
    got = parse.extract_links(body, "https://hn.algolia.com/api/v1/search?query=python&page=0")
    assert got == ["https://hn.algolia.com/api/v1/search?query=python&page=1"], got
    assert not any("pynecone" in u for u in got), "mined a link out of quoted markup"


def t_a_body_that_is_not_a_json_listing_yields_nothing_and_never_raises() -> None:
    for junk in ("", "   ", "<html><a href='/x'>y</a></html>", "{not json", "[1,2,3]",
                 _j({"ok": True}), _j({"detail": "not found"})):
        assert parse.links_from_json(junk, "https://x.example/api") == [], repr(junk[:30])


def t_html_extraction_is_untouched() -> None:
    """The negative control for the JSON branch: an ordinary page must still be parsed as HTML."""
    got = parse.extract_links('<html><body><a href="/news/page/2/">next</a></body></html>',
                              "https://example.com/news/")
    assert got == ["https://example.com/news/page/2/"], got


def t_the_two_pager_shapes_the_shape_test_could_not_see() -> None:
    """JSON:API spells its pager page[number]= and Solr spells its cursor cursorMark=, so neither
    matched — the chains were treated as a facet explosion (depth+1 per page, dead at 20)."""
    for u in ("https://api.datacite.org/dois?page%5Bcursor%5D=2",
              "https://api.datacite.org/dois?page[number]=3",
              "https://www.ebi.ac.uk/europepmc/webservices/rest/search?cursorMark=AoE%2F&format=json",
              "https://x.example/solr?cursor_mark=AoE"):
        assert _looks_paginated(u), f"{u} is not recognised as a pager"
    for u in ("https://x.example/products?colour=red", "https://x.example/a/b/c",
              "https://x.example/s?page_layout=grid"):
        assert not _looks_paginated(u), f"{u} was wrongly front-loaded as a pager"


def t_a_pager_segments_trailing_slash_is_load_bearing() -> None:
    """state.gov answers /press-releases/page/2 with a 301 to /press-releases/ — page ONE — while
    /press-releases/page/2/ is the real page 2. Stripping that slash turned every link in a
    214-page pager into the listing root and the frontier dedupped the whole chain away (measured:
    three byte-identical page-1 bodies, sizes [220162, 220162, 220162])."""
    assert canonicalize_url("https://www.state.gov/press-releases/page/2/") == \
        "https://www.state.gov/press-releases/page/2/"
    assert canonicalize_url("https://x.example/paged/7/") == "https://x.example/paged/7/"
    # Everything else still collapses — the dedup win this carve-out must not undo.
    assert canonicalize_url("https://x.example/about/") == "https://x.example/about"
    assert canonicalize_url("https://x.example/page/two/") == "https://x.example/page/two"


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} JSON-PAGER TESTS PASSED")


def test_json_pager_suite() -> None:
    main()


if __name__ == "__main__":
    main()

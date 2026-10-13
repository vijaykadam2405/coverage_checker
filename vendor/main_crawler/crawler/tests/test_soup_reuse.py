"""One parse per page instead of seven — and the mutation that made it dangerous.

Processing one HTML page called seven extractors in this module, each building its own soup of the
same string: _needs_render → visible_text + extract_links, _capture_assets → extract_pdf_links,
_enqueue_links → extract_links + extract_rel_links, then title_of and extract_meta.

Measured on 11 real defence pages (bhel, bel, drdo, bharatforge, mazagon, adani, chemring, saab,
leonardo, qinetiq; 54-655 KB, mean 221 KB), running that exact seven-call sequence:

    seven parses   163.5 ms/page
    one parse       49.4 ms/page      3.3x, ~15% of the whole per-page CPU budget

The catch is that visible_text used to decompose() nav/header/footer. That was harmless while every
call built a private soup and destructive the moment they share one: _needs_render runs BEFORE
_enqueue_links, so a mutating visible_text would delete the site's navigation from the tree and the
crawl would quietly stop discovering pages. `test_navigation_links_survive_a_visible_text_call` is
that regression, and it fails against the old implementation.

Runnable directly:  python tests/test_soup_reuse.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import parse  # noqa: E402

PAGE = """<!DOCTYPE html>
<html><head><title>Acme Defence</title>
<meta name="author" content="Acme Newsroom">
<meta property="article:published_time" content="2026-07-28T09:00:00Z">
<script>var junk = "SCRIPT_BODY_MUST_NOT_BE_TEXT";</script>
<style>.x{color:red}</style></head>
<body>
<!-- COMMENT_MUST_NOT_BE_TEXT -->
<header><a href="/home">Home</a><a href="/about">About</a></header>
<nav><a href="/products">Products</a><a href="/careers">Careers</a></nav>
<main><p>Real body copy.</p><a href="/story">Story</a>
<a href="/brochure.pdf">Brochure</a></main>
<footer><a href="/legal">Legal</a></footer>
</body></html>"""


def _fresh():
    parse._SOUP_CACHE["key"] = None


def test_the_same_string_is_parsed_once():
    _fresh()
    first = parse._soup(PAGE)
    assert parse._soup(PAGE) is first, "second call rebuilt the soup — the cache is not working"


def test_a_different_document_is_not_served_from_cache():
    """The failure that would matter: page B handed page A's tree. The key carries the length as
    well as the identity precisely because CPython reuses the address of a freed string."""
    _fresh()
    a = parse._soup(PAGE)
    other = PAGE.replace("Acme Defence", "Other Corp Ltd")
    b = parse._soup(other)
    assert b is not a
    assert b.title.get_text() == "Other Corp Ltd"
    assert parse.title_of(other) == "Other Corp Ltd"


def test_navigation_links_survive_a_visible_text_call():
    """THE REGRESSION. visible_text runs first in the real call order; if it mutates the shared
    tree, every header/nav/footer link disappears from the crawl."""
    _fresh()
    before = parse.extract_links(PAGE, "https://acme.test/")
    _fresh()
    parse.visible_text(PAGE)                    # same order as _needs_render → _enqueue_links
    after = parse.extract_links(PAGE, "https://acme.test/")
    assert after == before, f"visible_text destroyed links: {len(before)} -> {len(after)}"
    for want in ("/home", "/about", "/products", "/careers", "/legal", "/story"):
        assert any(u.endswith(want) for u in after), f"lost {want}"


def test_pdf_links_survive_too():
    _fresh()
    parse.visible_text(PAGE)
    assert any(u.endswith("/brochure.pdf")
               for u in parse.extract_pdf_links(PAGE, "https://acme.test/"))


def test_visible_text_still_drops_chrome_and_never_leaks_markup():
    _fresh()
    txt = parse.visible_text(PAGE)
    assert "Real body copy." in txt
    for junk in ("SCRIPT_BODY_MUST_NOT_BE_TEXT", "COMMENT_MUST_NOT_BE_TEXT", "color:red"):
        assert junk not in txt, f"{junk} reached the entity-resolution text"
    for chrome in ("Home", "About", "Products", "Careers", "Legal"):
        assert chrome not in txt, f"nav chrome '{chrome}' is back in the visible text"
    assert not txt.lstrip().startswith("html"), "the doctype leaked into the text"


def test_every_extractor_still_works_off_the_shared_tree():
    _fresh()
    assert parse.title_of(PAGE) == "Acme Defence"
    assert parse.extract_meta(PAGE).get("author") == "Acme Newsroom"
    assert parse.extract_links(PAGE, "https://acme.test/")
    assert parse.visible_text(PAGE)
    assert parse.title_of(PAGE) == "Acme Defence"          # still right after the others ran


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} SOUP-REUSE TESTS PASSED")


if __name__ == "__main__":
    main()

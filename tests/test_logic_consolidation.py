"""URL-set/evidence regressions for shared parsing; no production traffic."""

import asyncio

import pytest

from crawler_coverage_auditor.discovery.advanced import (
    inline_scripts,
    metadata_links,
    source_tasks,
)
from crawler_coverage_auditor.discovery.evidence import embedded_links, header_links
from crawler_coverage_auditor.discovery.filters import (
    _filter_states,
    detect_filters,
    get_filter_urls,
)
from crawler_coverage_auditor.discovery.page import page_analysis, parsed_html
from crawler_coverage_auditor.discovery.pagination import detect_pagination
from crawler_coverage_auditor.discovery.static_links import extract_links
from crawler_coverage_auditor.integrations.main_crawler import load_helper
from crawler_coverage_auditor.validation.access import detect_access
from crawler_coverage_auditor.validation.normalize import normalize_url
from crawler_coverage_auditor.validation.publication import publication_evidence
from crawler_coverage_auditor.validation.text_extract import extract_text

BASE = "https://example.test/news/"


@pytest.mark.parametrize(
    "html",
    [
        "",
        '<a href="/article">Article</a>',
        '<link rel="previous" href="?page=2"><link rel="next nofollow" href="?page=3">',
        '<base href="/root/"><a href="item">Item</a>',
        '<a href="http://[bad/">Bad</a><a href="/good">Good</a>',
        '<a href="/article#details">Section</a><a download href="/report.pdf">PDF</a>',
    ],
)
def test_owned_parser_preserves_imported_url_set(html):
    parser = load_helper("parse")
    original = {normalize_url(u) for u, _ in parser.extract_links_with_text(html, BASE)}
    original |= {normalize_url(u) for u in parser.extract_rel_links(html, BASE)}
    current = {normalize_url(link.url) for link in extract_links(html, BASE)}
    assert original <= current


def test_unique_candidates_keep_spa_resource_hints_labels_and_head_relations(
    monkeypatch,
):
    monkeypatch.setattr(
        "crawler_coverage_auditor.integrations.main_crawler.html_links",
        lambda *_: pytest.fail("Retired second HTML parser must not execute"),
    )
    html = """<link rel="first" href="?page=1"><link rel="last" href="?page=9">
    <a href="/#/article/2">SPA</a><a download href="/report.pdf">PDF</a>
    <a href="/article"></a><a href="/article">Useful label</a>
    <img data-src="/lazy.png"><button onclick="location.assign('/button')">Open</button>"""
    links = extract_links(html, BASE)
    assert len(links) == len({(link.url, link.tag) for link in links})
    assert link_data(links, BASE + "?page=1")
    assert link_data(links, BASE + "?page=9")
    assert link_data(links, "https://example.test/#/article/2")
    assert link_data(links, "https://example.test/report.pdf")[0].tag == "download"
    assert (
        link_data(links, "https://example.test/article")[0].anchor_text
        == "Useful label"
    )


def link_data(links, url):
    return [link for link in links if link.url == url]


def test_header_parser_union_handles_first_last_commas_and_malformed_targets():
    header = '<http://[bad/>; rel="next", </a,b?page=2>; title="x,y"; rel="nofollow next", </last>; rel=last, </first>; rel="first"'
    assert {link.url for link in header_links(header, BASE)} == {
        "https://example.test/a,b?page=2",
        "https://example.test/last",
        "https://example.test/first",
    }
    assert not header_links("", BASE)
    assert not header_links('</not-pagination>; title="an example; rel=next"', BASE)


def test_shared_snapshot_isolation_and_json_reuse():
    html = """<html><head><title>Public</title>
    <script type="application/ld+json">{"url":"/article","image":"/image.png"}</script>
    <script>const route='/other';</script></head><body><nav><a href="/menu">Menu</a></nav>
    <main><h1>Public article heading</h1><p>Meaningful content.</p>
    <select name="year"><option value="2024">2024</option></select></main></body></html>"""
    with page_analysis() as analysis:
        original = str(parsed_html(html))
        assert extract_text(html)[0].startswith("Public article")
        detect_access(200, {}, html, BASE)
        publication_evidence(html, BASE)
        a = {link.url for link in embedded_links(html, BASE)}
        b = {link.url for link in metadata_links(html, BASE)}
        detect_filters(html, BASE)
        source_tasks(html, BASE)
        inline_scripts(html)
        assert a <= b
        assert (
            str(parsed_html(html)) == original
        )  # Consumers never destroy shared links/scripts.
        assert analysis.parses == 1
        assert analysis.json_parses == 1 and analysis.json_hits >= 1
        assert "https://example.test/menu" in {
            link.url for link in extract_links(html, BASE)
        }
    with page_analysis() as other:
        parsed_html(html)
        assert other.parses == 1 and other.hits == 0


def test_source_policy_and_provenance_are_not_collapsed():
    html = '<script type="application/json">{"url":"/admin","font":"/font.eot","page":"/article"}</script>'
    with page_analysis():
        structured = embedded_links(html, BASE)
        metadata = metadata_links(html, BASE)
    assert any(link.tag == "structured" for link in structured)
    assert {link.url for link in metadata} == {"https://example.test/article"}


def test_filter_cache_keeps_defaults_multivalues_and_query_dependencies():
    _filter_states.cache_clear()
    html = '<form action="/search" method="get"><input type="hidden" name="lang" value="en"><select name="year"><option value="2024">2024</option></select></form>'
    control = detect_filters(html, BASE)[0]
    a = get_filter_urls(control, BASE + "?category=a&category=b")
    b = get_filter_urls(control, "https://example.test/another?category=a&category=b")
    assert a == b and _filter_states.cache_info().hits == 1
    assert get_filter_urls(control, BASE + "?category=c") != a
    a.append("caller mutation")
    assert "caller mutation" not in get_filter_urls(
        control, BASE + "?category=a&category=b"
    )


def test_pagination_skips_bad_links_and_retains_multitoken_relations():
    hints = detect_pagination(
        '<a href="http://[bad/">Bad</a><link rel="nofollow next" href="?page=3">', BASE
    )
    assert any(hint.kind == "NEXT" and hint.url == BASE + "?page=3" for hint in hints)


def test_analysis_context_does_not_cross_concurrent_tasks():
    async def work(name):
        with page_analysis() as analysis:
            parsed_html("<h1>" + name + "</h1>")
            await asyncio.sleep(0)
            assert analysis.parses == 1
            return analysis

    async def run():
        return await asyncio.gather(work("one"), work("two"))

    a, b = asyncio.run(run())
    assert a is not b


def test_html_base_does_not_rebase_http_headers():
    html = '<base href="/root/"><meta property="og:url" content="child"><script src="module.js"></script>'
    links = {
        link.url for link in metadata_links(html, BASE, {"content-location": "header"})
    }
    assert links == {"https://example.test/root/child", BASE + "header"}
    assert set(
        source_tasks(html, BASE, {"link": '<wp-json/>; rel="https://api.w.org/"'})
    ) == {
        ("SCRIPT", "https://example.test/root/module.js"),
        ("WORDPRESS_ROOT", BASE + "wp-json/"),
    }


def test_date_based_continuations_are_observed_not_guessed():
    hints = detect_pagination(
        '<link rel="next" href="?before=2024-01-01"><link rel="prev" href="?after=2025-01-01">',
        BASE,
    )
    assert {(item.kind, item.url) for item in hints} == {
        ("NEXT", BASE + "?before=2024-01-01"),
        ("PREVIOUS", BASE + "?after=2025-01-01"),
    }
    assert all(item.value is None for item in hints)


def test_embedded_json_accepts_mime_case_and_parameters():
    html = (
        '<script type="Application/LD+JSON; charset=utf-8">{"url":"/article"}</script>'
    )
    with page_analysis():
        assert {item.url for item in embedded_links(html, BASE)} == {
            "https://example.test/article"
        }
        assert {item.url for item in metadata_links(html, BASE)} == {
            "https://example.test/article"
        }

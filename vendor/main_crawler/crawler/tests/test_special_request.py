"""Special-request crawler: scope, BFS order, budget, and failure isolation.

Runs entirely against a fake C3 render — no CamoFox, no network, no ingest. What matters here is
that a C3-only crawler cannot wander off-site (every page costs a stealth render, so an escape is
expensive as well as wrong) and that one refused page doesn't end the run.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler.ingest_client import IngestOutcome  # noqa: E402
from special_request_crawler import crawl as sr  # noqa: E402

PAGES = {
    "https://www.sipri.org/": """
        <html><head><title>SIPRI</title></head><body>
          <a href="/research">Research</a>
          <a href="/media">Media</a>
          <a href="https://twitter.com/sipriorg">Twitter</a>
          <a href="https://evil.example.com/x">Off-site</a>
        </body></html>""",
    "https://www.sipri.org/research": """
        <html><head><title>Research</title></head><body>
          <a href="/research/armaments">Armaments</a>
        </body></html>""",
    "https://www.sipri.org/media": "<html><head><title>Media</title></head><body>ok</body></html>",
    "https://www.sipri.org/research/armaments":
        "<html><head><title>Armaments</title></head><body>deep</body></html>",
}


class FakeIngest:
    def __init__(self):
        self.docs = []

    def send(self, doc):
        self.docs.append(doc)
        # The REAL dataclass, not a stub: a hand-rolled fake with an invented attribute name is how
        # `outcome.ok` (the field is `accepted`) passed tests while under-reporting every success.
        return IngestOutcome(accepted=True, failing_rule=None, document_id="d1")


def _no_network(*_a, **_kw):
    raise AssertionError("test reached the network — sipri.org must never be hit by the suite")


def install_fake_render(monkey: dict, fail_urls: set[str] | None = None):
    fail_urls = fail_urls or set()
    order: list[str] = []

    async def _render(url, target):
        order.append(url)
        if url in fail_urls:
            return None                                  # refused / challenge page
        html = PAGES.get(url.rstrip("/") if url != "https://www.sipri.org/" else url)
        if html is None:
            html = PAGES.get(url)
        if html is None and "sipri.org" in url:
            html = f"<html><title>seed</title><body>{url}</body></html>"   # any in-scope sitemap seed
        if html is None:
            return None
        return {"html": html, "text": "text", "final_url": url, "snapshot": None, "screenshot": None}

    monkey["_render"] = sr._render
    monkey["_fetch_direct"] = sr._fetch_direct
    sr._render = _render
    sr._fetch_direct = _no_network        # the fallback must not silently become a live fetch
    return order


def restore(monkey: dict):
    sr._render = monkey["_render"]
    if "_fetch_direct" in monkey:
        sr._fetch_direct = monkey["_fetch_direct"]


def target(**over):
    # incremental OFF so these stay hermetic. It defaults ON, and the crawl history is real and
    # shared with production runs — once sipri.org had actually been crawled on this machine,
    # `_already_done` skipped the seed before any fetch and every scope/redirect assertion here
    # started failing with an empty stats dict, which reads exactly like an engine bug. These
    # tests are about scope and redirects, not about incremental skipping.
    # respect_robots off too: the production build fetches robots.txt over the network before the
    # first page, so these offline tests sat waiting on a real HTTP timeout (45s per file) for a
    # host they never intend to contact. Robots compliance has its own suite.
    t = {"name": "sipri", "source_id": "sipri_org", "incremental": False, "respect_robots": False,
         "seed_urls": ["https://www.sipri.org/"], "max_pages": 50, "max_depth": 3,
         "concurrency": 2, "delay_s": 0.0, "timeout_s": 5.0, "solve_captchas": False,
         "path_scope": None, "direct_fallback": False}
    t.update(over)
    return t


def run(t, fail=None):
    monkey: dict = {}
    order = install_fake_render(monkey, fail)
    ing = FakeIngest()
    sr.camofox_client.enabled = lambda: True
    try:
        stats = asyncio.run(sr.crawl_target(t, ingest=ing))
    finally:
        restore(monkey)
    return stats, ing, order


def t_stays_on_site() -> None:
    stats, ing, order = run(target())
    hosts = {sr.urlsplit(d.url).hostname for d in ing.docs}
    assert hosts == {"www.sipri.org"}, f"left the target site: {hosts}"
    assert not any("twitter" in u or "evil" in u for u in order), \
        f"rendered an off-site URL (expensive AND wrong): {order}"


def t_offsite_redirect_is_dropped() -> None:
    """A 30x can carry a fetch off-domain; _document uses final_url, so without a post-fetch scope
    check an off-site page is stored under the target. Observed: meduza.io in sipri.documents."""
    monkey: dict = {}
    monkey["_render"] = sr._render
    monkey["_fetch_direct"] = sr._fetch_direct

    async def redirecting(url, target):
        return {"html": "<html><title>x</title><body>b</body></html>", "text": "t",
                "final_url": "https://meduza.io/feature/hijacked"}

    sr._render = redirecting
    sr._fetch_direct = _no_network
    ing = FakeIngest()
    sr.camofox_client.enabled = lambda: True
    try:
        stats = asyncio.run(sr.crawl_target(target(max_depth=0), ingest=ing))
    finally:
        sr._render = monkey["_render"]; sr._fetch_direct = monkey["_fetch_direct"]
    assert not ing.docs, f"stored an off-site page: {[d.url for d in ing.docs]}"
    assert stats.errors.get("offsite_redirect") == 1, stats.as_dict()


def t_respects_depth() -> None:
    stats, ing, _ = run(target(max_depth=1))
    urls = {d.url for d in ing.docs}
    assert "https://www.sipri.org/research/armaments" not in urls, "crawled past max_depth"
    assert "https://www.sipri.org/research" in urls, "did not crawl depth 1"


def t_respects_page_budget() -> None:
    stats, ing, _ = run(target(max_pages=2))
    assert stats.fetched <= 2, f"fetched {stats.fetched}, budget was 2"
    assert len(ing.docs) <= 2


def t_bfs_before_depth() -> None:
    """Both depth-1 siblings must be rendered before the depth-2 page."""
    _stats, _ing, order = run(target())
    idx = {u: i for i, u in enumerate(order)}
    deep = idx.get("https://www.sipri.org/research/armaments")
    assert deep is not None, f"never reached the deep page: {order}"
    for sibling in ("https://www.sipri.org/research", "https://www.sipri.org/media"):
        assert idx[sibling] < deep, f"depth-2 page ran before {sibling} — not BFS: {order}"


def t_one_refusal_does_not_end_the_run() -> None:
    stats, ing, _ = run(target(), fail={"https://www.sipri.org/media"})
    assert stats.failed == 1 and stats.errors.get("render_empty") == 1, stats.as_dict()
    assert any(d.url == "https://www.sipri.org/research" for d in ing.docs), \
        "a single refused page aborted the crawl"


def t_documents_are_tagged_c3() -> None:
    _stats, ing, _ = run(target())
    d = ing.docs[0]
    assert d.served_by == "c3_camofox",         "must use the established lane vocabulary (c1_live/c2_wayback/c3_camofox)"
    assert d.document_id and d.document_id.startswith("doc_"),         "documents.document_id is a NOT NULL primary key — ingest 200s anyway and the row is lost"
    assert d.content_hash and d.source_id == "sipri_org" and d.title
    assert d.url.startswith("https://www.sipri.org")


def t_targets_file_is_valid() -> None:
    ts = sr.load_targets()
    names = [t["name"] for t in ts]
    assert names == ["sipri", "att_assistance"], f"unexpected targets: {names}"
    for t in ts:
        assert t["seed_urls"], f"{t['name']} has no seeds"
        # C3 is expensive and SIPRI's origin is slow: concurrency above 2 measurably INCREASED the
        # failure rate (11/12 renders empty at conc=3), so this cap is a finding, not a style rule.
        cap = 4 if t.get("transport") == "direct_first" else 2
        assert t["concurrency"] <= cap, (
            f"{t['name']} concurrency {t['concurrency']} > {cap}: C3 renders are expensive and "
            "SIPRI's origin is slow, so only the cheap direct transport earns extra workers")
        assert t.get("retries", 1) <= 1, (
            f"{t['name']}: retries>1 multiplies a 90s timeout on every refused page — at retries=3 "
            "the crawl managed 1 page in 4.5 minutes")
    json.dumps(ts)


def t_direct_fallback_is_tagged_distinctly() -> None:
    """A page rescued by plain HTTPS must NOT claim to be a C3 capture — otherwise C3's real
    coverage is unmeasurable and a silent transport regression looks like success."""
    c3 = sr._document(target(), "https://www.sipri.org/x",
                      {"html": "<html><title>a</title><body>b</body></html>", "text": "t"}, 0, None)
    direct = sr._document(target(), "https://www.sipri.org/x",
                          {"html": "<html><title>a</title><body>b</body></html>", "text": "",
                           "_via": "direct"}, 0, None)
    assert c3.served_by == "c3_camofox"
    assert direct.served_by == "c1_live", "direct fetch mislabelled as a C3 render"

def t_retry_recovers_a_flaky_render() -> None:
    """C3 refusals are intermittent; the first attempt failing must not lose the page."""
    calls: dict[str, int] = {}
    monkey: dict = {}
    monkey["_render"] = sr._render

    async def flaky(url, target):
        calls[url] = calls.get(url, 0) + 1
        if url == "https://www.sipri.org/" and calls[url] == 1:
            return None                                   # first attempt refused
        html = PAGES.get(url)
        return {"html": html, "text": "t", "final_url": url} if html else None

    sr._render = flaky
    ing = FakeIngest()
    sr.camofox_client.enabled = lambda: True
    try:
        stats = asyncio.run(sr.crawl_target(target(max_depth=0, retries=1), ingest=ing))
    finally:
        sr._render = monkey["_render"]
    assert stats.fetched == 1 and stats.failed == 0, stats.as_dict()
    assert calls["https://www.sipri.org/"] == 2, "did not retry the refused render"


def t_retries_are_bounded() -> None:
    calls: dict[str, int] = {}
    monkey: dict = {}
    monkey["_render"] = sr._render

    async def always_fail(url, target):
        calls[url] = calls.get(url, 0) + 1
        return None

    sr._render = always_fail
    sr.camofox_client.enabled = lambda: True
    try:
        stats = asyncio.run(sr.crawl_target(target(max_depth=0, retries=1), ingest=FakeIngest()))
    finally:
        sr._render = monkey["_render"]
    assert calls["https://www.sipri.org/"] == 2, f"unbounded retry: {calls}"
    assert stats.failed == 1


def t_document_survives_missing_html() -> None:
    """Without CAMOFOX_API_KEY the /evaluate route returns no HTML. title/main_text are REQUIRED
    str on Document, so a None there raises ValidationError and loses the page outright."""
    snap = {"html": "", "text": "aria text body", "final_url": "https://www.sipri.org/research"}
    doc = sr._document(target(), "https://www.sipri.org/research", snap, 1, None)
    assert doc.title and isinstance(doc.title, str), "title must never be None"
    assert doc.html == "" and doc.main_text == "aria text body"
    assert doc.content_hash


def t_counts_accepted_via_the_real_outcome_type() -> None:
    """Guards the attribute name: IngestOutcome.accepted, not .ok."""
    ing = FakeIngest()
    monkey: dict = {}
    install_fake_render(monkey)
    sr.camofox_client.enabled = lambda: True
    try:
        stats = asyncio.run(sr.crawl_target(target(max_depth=0), ingest=ing))
    finally:
        restore(monkey)
    assert stats.sent == 1, stats.as_dict()
    assert stats.accepted == stats.sent,         f"accepted={stats.accepted} but sent={stats.sent} — success check reads the wrong field"
    assert not stats.errors, stats.errors


def t_rejection_is_reported_with_its_rule() -> None:
    class Rejecting(FakeIngest):
        def send(self, doc):
            self.docs.append(doc)
            return IngestOutcome(accepted=False, failing_rule="too_short", document_id=None)

    monkey: dict = {}
    install_fake_render(monkey)
    sr.camofox_client.enabled = lambda: True
    try:
        stats = asyncio.run(sr.crawl_target(target(max_depth=0), ingest=Rejecting()))
    finally:
        restore(monkey)
    assert stats.accepted == 0 and stats.errors.get("ingest:too_short") == 1, stats.as_dict()


def t_sitemap_seeds_are_crawled_and_scoped() -> None:
    """Sitemap seeding (phase 1): every published in-scope URL is crawled up front; off-site URLs in
    the sitemap are filtered. Patches the sitemap MODULE, since crawl_target does `from . import
    sitemap` and would otherwise ignore an attribute patched on `sr`."""
    from special_request_crawler import sitemap as sm
    real = sm.fetch_urls
    sm.fetch_urls = lambda root, **k: ["https://www.sipri.org/research/a",
                                       "https://www.sipri.org/databases/b",
                                       "https://off-site.com/x"]
    monkey: dict = {}
    install_fake_render(monkey)
    # direct_first makes _fetch_direct the PRIMARY transport, so it must serve content here rather
    # than raise — otherwise the test proves nothing about seeding, only about the fallback.
    sr._fetch_direct = monkey["_render"] = monkey.get("_render") or sr._fetch_direct
    async def _direct(url, target):
        return {"html": f"<html><title>t</title><body>{url}</body></html>", "text": "",
                "final_url": url, "_via": "direct"}
    sr._fetch_direct = _direct
    ing = FakeIngest()
    sr.camofox_client.enabled = lambda: True
    t = target(sitemap="https://www.sipri.org/sitemap.xml", max_depth=0, transport="direct_first")
    try:
        stats = asyncio.run(sr.crawl_target(t, ingest=ing))
    finally:
        sm.fetch_urls = real
        restore(monkey)
    urls = {d.url for d in ing.docs}
    assert "https://www.sipri.org/research/a" in urls, f"sitemap seed not crawled: {urls}"
    assert "https://www.sipri.org/databases/b" in urls, "second sitemap seed missing"
    assert not any("off-site" in u for u in urls), "off-site sitemap URL was crawled"


def main() -> None:
    for fn in (t_stays_on_site, t_offsite_redirect_is_dropped,
               t_sitemap_seeds_are_crawled_and_scoped, t_respects_depth, t_respects_page_budget, t_bfs_before_depth,
               t_one_refusal_does_not_end_the_run, t_documents_are_tagged_c3,
               t_targets_file_is_valid, t_direct_fallback_is_tagged_distinctly,
               t_retry_recovers_a_flaky_render,
               t_retries_are_bounded, t_document_survives_missing_html, t_counts_accepted_via_the_real_outcome_type,
               t_rejection_is_reported_with_its_rule):
        fn()
        print(f"  ok  {fn.__name__}")
    print("ALL SPECIAL-REQUEST CRAWLER TESTS PASSED")



def test_special_request_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()

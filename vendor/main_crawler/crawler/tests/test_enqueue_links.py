"""_enqueue_links: child-link expansion shared by the live C1 path AND the C3 CamoFox
fallback. Without it a site served entirely by CamoFox dead-ends at the seed page and
never reaches its max_pages budget (only the frontier drives the crawl)."""
import asyncio

from crawler import keywords as kwmod
from crawler.async_engine import AsyncEngine, JobCtx, _looks_paginated, _is_action_link
from crawler.fetcher import FetchResult
from crawler.models import Job
from crawler.seed import load_seed

SEED = load_seed()


def _job(**kw):
    d = dict(job_id="e", job_type="news", seed_urls=["https://ex.com/"],
             max_pages=500, max_depth=4, same_domain_only=True, capture=["html", "text"])
    d.update(kw)
    return Job(**d)


def _eng():
    eng = AsyncEngine.__new__(AsyncEngine)     # skip __init__ (no playwright)
    eng.frontier = asyncio.Queue()
    return eng


def _ctx(job):
    return JobCtx(job, SEED, kwmod.from_list([]), forward=False)


def _fr(html):
    return FetchResult(url="https://ex.com/", final_url="https://ex.com/", status=200,
                       kind="html", text_html=html, fetched_at="2026-01-01T00:00:00Z")


HTML = """<html><body>
  <a href="https://ex.com/a">A</a>
  <a href="https://ex.com/b">B</a>
  <a href="/c">C relative</a>
  <a href="https://other.com/x">offsite</a>
</body></html>"""


def test_camofox_page_enqueues_children():
    """A C3-served page's links land in the frontier (the fix) — this is what lets a
    fully-WAF-blocked site keep crawling past the seed into its max_pages budget."""
    eng = _eng()
    ctx = _ctx(_job())
    eng._enqueue_links(ctx, _fr(HTML), "https://ex.com/", depth=0)
    urls = []
    while not eng.frontier.empty():
        urls.append(eng.frontier.get_nowait().url)
    assert any(u.endswith("/a") for u in urls), urls
    assert any(u.endswith("/b") for u in urls), urls
    assert any(u.endswith("/c") for u in urls), urls          # relative resolved
    assert len(urls) >= 3, urls


def _redirected(seed: str, landed: str, html: str):
    """A seed fetch that ended up on a DIFFERENT host — final_url is where we actually are."""
    return FetchResult(url=seed, final_url=landed, status=200, kind="html",
                       text_html=html, fetched_at="2026-01-01T00:00:00Z")


PARKED_HTML = """<html><body>
  <a href="https://www.hugedomains.com/domain_profile.cfm?d=CeliacMatters.com">CeliacMatters</a>
  <a href="https://www.hugedomains.com/escrow.cfm">Escrow</a>
  <a href="https://www.hugedomains.com/terms.cfm">Terms</a>
</body></html>"""


def test_a_seed_that_expired_onto_a_broker_does_not_drag_in_the_marketplace():
    """An expired domain does not 404 — it 301s to whoever now sells the name.

    acmat.com is in the 802-source catalogue and now redirects to hugedomains.com. Because a
    cross-domain redirect ON A SEED legitimately moves the crawl (the airbots rebrand below), the
    broker was treated as the site's new home and its whole marketplace entered scope: 70 pages and
    164 requests of for-sale listings for CeliacMatters.com and TheZodiacMatch.com landed in a
    defence corpus — 17% of everything that run captured.
    """
    eng = _eng()
    ctx = _ctx(_job(seed_urls=["https://acmat.com/"]))
    fr = _redirected("https://acmat.com/", "https://www.hugedomains.com/domain_profile.cfm?d=Acmat.com",
                     PARKED_HTML)
    eng._enqueue_links(ctx, fr, "https://acmat.com/", depth=0)
    urls = []
    while not eng.frontier.empty():
        urls.append(eng.frontier.get_nowait().url)
    assert not any("hugedomains" in u for u in urls), (
        f"the broker's own pages were enqueued, so the crawl walks the marketplace: {urls}")
    assert not any("hugedomains" in d for d in ctx.seed_domains), (
        f"hugedomains entered seed_domains, which widens scope for every later page: "
        f"{sorted(ctx.seed_domains)}")


def test_a_genuine_rebrand_redirect_still_moves_the_crawl():
    """The case the scope-widening exists for, and which the parking guard must not break:
    airbots.co.in 301s to airbotsaerospace.com and the crawl has to follow, or it stops at one
    page. Only BROKERS are refused, not cross-domain redirects in general."""
    eng = _eng()
    ctx = _ctx(_job(seed_urls=["https://airbots.co.in/"]))
    html = """<html><body>
      <a href="https://airbotsaerospace.com/about">About</a>
      <a href="https://airbotsaerospace.com/products">Products</a>
    </body></html>"""
    fr = _redirected("https://airbots.co.in/", "https://airbotsaerospace.com/", html)
    eng._enqueue_links(ctx, fr, "https://airbots.co.in/", depth=0)
    urls = []
    while not eng.frontier.empty():
        urls.append(eng.frontier.get_nowait().url)
    assert any("airbotsaerospace.com/about" in u for u in urls), (
        f"a legitimate rebrand no longer follows its redirect — the crawl dead-ends: {urls}")


def test_parking_hosts_are_matched_on_the_registered_domain():
    """Brokers serve parking pages from every label they own, and the guard is only useful if it
    is not defeated by a subdomain."""
    from crawler.canonicalize import is_parking_host
    for u in ("https://www.hugedomains.com/x", "https://hugedomains.com/",
              "https://shop.sedo.com/a", "https://dan.com/buy", "https://www.afternic.com/"):
        assert is_parking_host(u), u
    for u in ("https://lockheedmartin.com/", "https://www.sipri.org/", "https://acmat.com/",
              "https://domainsofdefence.example.com/"):
        assert not is_parking_host(u), f"a real site was mistaken for a broker: {u}"


def test_respects_max_depth():
    """At the depth limit, no children are enqueued (bounds the crawl)."""
    eng = _eng()
    ctx = _ctx(_job(max_depth=2))
    eng._enqueue_links(ctx, _fr(HTML), "https://ex.com/", depth=2)   # depth == max_depth
    assert eng.frontier.empty()


def test_respects_budget():
    """No enqueue once the page budget is spent (max_pages bound holds for C3 too)."""
    eng = _eng()
    job = _job(max_pages=1)
    ctx = _ctx(job)
    ctx.budget_used = 1                       # budget exhausted
    eng._enqueue_links(ctx, _fr(HTML), "https://ex.com/", depth=0)
    assert eng.frontier.empty()


# A page whose links include a MALFORMED href (bad port / bad IPv6). Before the fix,
# canonicalize_url(parts.port) raised ValueError and aborted the WHOLE loop → the good
# links after it were never enqueued → shallow crawl. Now one bad link is skipped.
POISON_HTML = """<html><body>
  <a href="https://ex.com/good1">g1</a>
  <a href="http://ex.com:80800/bad">bad port</a>
  <a href="http://[:::1]/bad2">bad ipv6</a>
  <a href="https://ex.com/good2">g2</a>
</body></html>"""


def test_bad_link_does_not_abort_the_page():
    """A poison href is skipped, not fatal — the good links after it still enqueue."""
    eng = _eng()
    ctx = _ctx(_job())
    eng._enqueue_links(ctx, _fr(POISON_HTML), "https://ex.com/", depth=0)
    urls = []
    while not eng.frontier.empty():
        urls.append(eng.frontier.get_nowait().url)
    assert any(u.endswith("/good1") for u in urls), urls
    assert any(u.endswith("/good2") for u in urls), urls   # would be missing under the old bug
    # no parse_error recorded for the page (extraction/enqueue failures are per-link now)
    assert ctx.errors_by_reason.get("parse_error", 0) == 0, ctx.errors_by_reason


def test_capture_assets_isolates_failures():
    """L1 captures text/html/pdf only — _capture_assets extracts PDF links; images/media are dropped."""
    from crawler.async_engine import _capture_assets
    job = _job(capture=["pdf", "images", "media"])
    html = '<html><body><img src="/x.jpg"><a href="/doc.pdf">d</a></body></html>'
    pdf, imgs, media = _capture_assets(job, html, "https://ex.com/")
    assert any("doc.pdf" in p for p in pdf), pdf
    assert imgs == [] and media == []          # images/media no longer captured
    # empty/None html -> three empty lists, no crash
    assert _capture_assets(job, None, "https://ex.com/") == ([], [], [])


# An HN-style listing: nav links at the TOP, the "More" pagination link at the BOTTOM, and a flood
# of non-content action links (vote/hide/login, all carrying ?goto=) in between. This is exactly the
# shape that made a real HN crawl store only the top-nav and never follow news?p=4.
HN_LIKE = """<html><body>
  <a href="/news">news</a><a href="/newest">newest</a><a href="/ask">ask</a>
  <a href="/item?id=1">1 comment</a>
  <a href="/vote?id=1&how=up&goto=news%3Fp%3D3">upvote</a>
  <a href="/hide?id=1&goto=news%3Fp%3D3">hide</a>
  <a href="/login?goto=news%3Fp%3D3">login</a>
  <a href="/user?id=alice">alice</a>
  <a href="/news?p=4" rel="next" class="morelink">More</a>
</body></html>"""


def test_pagination_link_is_enqueued_first():
    """The 'next page' link must jump to the FRONT of the frontier even though it is the LAST link in
    the document — otherwise a rate-limited / max_pages crawl spends its slots on the nav bar above it
    and never follows the chain (the real HN failure)."""
    eng = _eng()
    ctx = _ctx(_job())
    eng._enqueue_links(ctx, _fr(HN_LIKE), "https://ex.com/news?p=3", depth=0)
    urls = []
    while not eng.frontier.empty():
        urls.append(eng.frontier.get_nowait().url)
    assert urls and urls[0].endswith("/news?p=4"), urls


def test_action_links_are_skipped_but_content_is_kept():
    """vote/hide/login (?goto=) links are never fetched — they store nothing and, on a rate-limiting
    host, the flood of them is what triggers the 429 that buries the real links. Content links (item,
    user, pagination) are still followed."""
    eng = _eng()
    ctx = _ctx(_job())
    eng._enqueue_links(ctx, _fr(HN_LIKE), "https://ex.com/news?p=3", depth=0)
    urls = []
    while not eng.frontier.empty():
        urls.append(eng.frontier.get_nowait().url)
    joined = " ".join(urls)
    assert "/vote?" not in joined and "/hide?" not in joined and "/login" not in joined, urls
    assert any("/item?id=1" in u for u in urls), urls          # real content still followed
    assert any("/news?p=4" in u for u in urls), urls


def test_pagination_does_not_consume_crawl_depth():
    """A 'next page' is the SAME listing, not a level deeper. Counting it as depth+1 capped a
    paginated crawl at max_depth PAGES — measured on pixabay (?pagi=), which died at page 3 under
    max_depth=2 while the operator expected the whole 14K-page listing. Content links still go
    deeper; only the pagination chain stays level."""
    eng = _eng()
    ctx = _ctx(_job(max_depth=2))
    html = ('<html><body><a href="/images/search/nature/?pagi=2">next</a>'
            '<a href="/photos/rose-123">a photo</a></body></html>')
    eng._enqueue_links(ctx, _fr(html), "https://ex.com/images/search/nature/", depth=1)
    got = {}
    while not eng.frontier.empty():
        it = eng.frontier.get_nowait()
        got[it.url] = it.depth
    page2 = next(u for u in got if "pagi=2" in u)
    photo = next(u for u in got if "rose-123" in u)
    assert got[page2] == 1, f"pagination must stay at the parent's depth, got {got[page2]}"
    assert got[photo] == 2, f"content links still go deeper, got {got[photo]}"


def test_pagination_is_not_eaten_by_the_facet_variant_cap():
    """Pagination has its own budget. Both key on host+path, so page 2..N used to burn the
    20-variant facet-explosion cap and the chain stopped at page 20 — well short of a long listing."""
    eng = _eng()
    ctx = _ctx(_job(max_depth=-1))
    # 60 pagination links on ONE path — far past CRAWLER_MAX_QUERY_VARIANTS (20)
    links = "".join(f'<a href="/search/?pagi={i}">{i}</a>' for i in range(2, 62))
    eng._enqueue_links(ctx, _fr(f"<html><body>{links}</body></html>"), "https://ex.com/search/", depth=0)
    urls = []
    while not eng.frontier.empty():
        urls.append(eng.frontier.get_nowait().url)
    assert len(urls) == 60, f"pagination chain was truncated by the facet cap: {len(urls)}"


def test_pagination_and_action_helpers():
    """Shape-only detectors: a page param is pagination, an id is not; auth/vote/goto is an action,
    a content path is not."""
    assert _looks_paginated("https://x.com/news?p=4")
    assert _looks_paginated("https://x.com/list?page=2")
    assert _looks_paginated("https://x.com/blog/page/3")
    assert _looks_paginated("https://pixabay.com/images/search/nature/?pagi=2")   # pixabay's form
    assert _looks_paginated("https://x.com/editors_choice?media_type=photo&pagi=7")
    assert not _looks_paginated("https://x.com/item?id=49")     # id is not a page param
    assert not _looks_paginated("https://x.com/newest")         # 'p' in a path is not ?p=
    assert not _looks_paginated("https://x.com/list?pagesize=50")   # a page SIZE is not an index
    assert not _looks_paginated("https://x.com/list?per_page=50")
    assert _is_action_link("https://x.com/vote?id=1&goto=news")
    assert _is_action_link("https://x.com/login?goto=/x")
    assert _is_action_link("https://x.com/logout")
    assert not _is_action_link("https://x.com/news?p=4")
    assert not _is_action_link("https://x.com/item?id=49")
    assert not _is_action_link("https://x.com/from?site=github.com")   # content listing, kept


if __name__ == "__main__":
    test_camofox_page_enqueues_children()
    test_respects_max_depth()
    test_respects_budget()
    test_bad_link_does_not_abort_the_page()
    test_pagination_link_is_enqueued_first()
    test_action_links_are_skipped_but_content_is_kept()
    test_pagination_does_not_consume_crawl_depth()
    test_pagination_is_not_eaten_by_the_facet_variant_cap()
    test_pagination_and_action_helpers()
    print("ALL ENQUEUE TESTS PASSED")
    test_capture_assets_isolates_failures()
    print("OK — enqueue_links self-check passed")


def test_cross_domain_redirect_still_follows_its_links():
    """A seed that 301s to a DIFFERENT domain must keep crawling the domain it landed on.

    The links are on the redirect TARGET, so the parent for the same_domain_only check has to be
    the final URL. Comparing them against the pre-redirect host instead makes every child look
    offsite, and under CRAWLER_STRICT_DOMAIN (the default) there is no one-hop exception to save
    them: the crawl stores the landing page and stops at exactly one page.

    Measured on airbots.co.in -> airbotsaerospace.com, which has 32 followable links.
    """
    import os
    eng = _eng()
    ctx = _ctx(_job(seed_urls=["https://airbots.co.in/"]))
    fr = FetchResult(url="https://airbots.co.in/", final_url="https://airbotsaerospace.com/",
                     status=200, kind="html", fetched_at="2026-01-01T00:00:00Z",
                     text_html='<html><body>'
                               '<a href="/about">About</a><a href="/product">Product</a>'
                               '<a href="https://forms.gle/x">offsite form</a>'
                               '</body></html>')
    old = os.environ.get("CRAWLER_STRICT_DOMAIN")
    os.environ["CRAWLER_STRICT_DOMAIN"] = "1"           # the production default
    try:
        eng._enqueue_links(ctx, fr, "https://airbots.co.in/", depth=0)
    finally:
        if old is None:
            os.environ.pop("CRAWLER_STRICT_DOMAIN", None)
        else:
            os.environ["CRAWLER_STRICT_DOMAIN"] = old
    urls = []
    while not eng.frontier.empty():
        urls.append(eng.frontier.get_nowait().url)
    assert any(u.endswith("/about") for u in urls), urls
    assert any(u.endswith("/product") for u in urls), urls
    assert not any("forms.gle" in u for u in urls), \
        "strict domain still applies -- a genuinely offsite link stays out"


def test_deep_cross_domain_redirect_does_not_rebase_the_crawl():
    """The mirror image of the airbots test, and the bug that test's fix introduced.

    Scope used to be compared child-vs-PARENT. So when a link deep inside rtx.com redirected to a
    Google property, `base` became the Google host and every child of that page was "same site as
    its parent" — the crawl adopted the whole domain. Measured live: 105 hosts / 4,412 pages, with
    about.google (221 pages) and policies.google.com (125) as the biggest strangers.

    A seed may move hosts. A page at depth 3 may not.
    """
    import os
    eng = _eng()
    ctx = _ctx(_job(seed_urls=["https://www.rtx.com/"]))
    fr = FetchResult(url="https://www.rtx.com/deep/page", final_url="https://policies.google.com/privacy",
                     status=200, kind="html", fetched_at="2026-01-01T00:00:00Z",
                     text_html='<html><body>'
                               '<a href="/terms">Terms</a><a href="/technologies">Tech</a>'
                               '</body></html>')
    old = os.environ.get("CRAWLER_STRICT_DOMAIN")
    os.environ["CRAWLER_STRICT_DOMAIN"] = "1"
    try:
        eng._enqueue_links(ctx, fr, "https://www.rtx.com/deep/page", depth=3)
    finally:
        if old is None:
            os.environ.pop("CRAWLER_STRICT_DOMAIN", None)
        else:
            os.environ["CRAWLER_STRICT_DOMAIN"] = old
    urls = []
    while not eng.frontier.empty():
        urls.append(eng.frontier.get_nowait().url)
    assert not any("google.com" in u for u in urls), \
        f"a deep redirect rebased the crawl onto a new domain: {urls}"
    assert "https://policies.google.com/privacy" not in ctx.seed_domains, \
        "a depth-3 redirect must never widen the crawl's scope"


def test_link_relevance_does_not_sever_the_pagination_chain():
    """A relevance-filtered crawl of a LISTING site used to stop dead at page 1.

    link_relevance_keywords matches anchor text, and a pagination control's anchor text is "2",
    "Next" or ">>" — no keyword, so it was dropped along with the irrelevant content links, and
    every relevant item after the first screenful became unreachable. On a tender portal that is
    the whole crawl: the defence listings are spread across hundreds of pages. Pagination is now
    exempt by URL SHAPE (_looks_paginated), so it needs no anchor text and link text cannot spoof
    its way past the filter.
    """
    eng = _eng()
    ctx = _ctx(_job(link_relevance_keywords=["ordnance", "naval"]))
    html = """<html><body>
      <a href="https://ex.com/t/1">Supply Of Ammunition To Ordnance Factory</a>
      <a href="https://ex.com/t/2">Construction Of Overhead Water Tank</a>
      <a href="https://ex.com/list?page=2">2</a>
      <a href="https://ex.com/list?page=3">Next</a>
      <a href="https://ex.com/careers">Careers</a>
    </body></html>"""
    eng._enqueue_links(ctx, _fr(html), "https://ex.com/list", depth=0)
    urls = []
    while not eng.frontier.empty():
        urls.append(eng.frontier.get_nowait().url)
    assert any("/t/1" in u for u in urls), f"relevant listing dropped: {urls}"
    assert any("page=2" in u for u in urls), f"pagination severed by the relevance filter: {urls}"
    assert any("page=3" in u for u in urls), urls
    # ...and the filter still does its job on content links.
    assert not any("/t/2" in u for u in urls), f"irrelevant listing followed: {urls}"
    assert not any("careers" in u for u in urls), urls


def test_link_header_and_rel_next_are_the_only_pager_on_some_apis():
    """`Link: <...>; rel="next"` is the ONLY next-page pointer api.github.com and every wp-json
    endpoint emit — the body is a JSON array with no links in it at all — and `<link rel="next">`
    lives in <head>, which extract_links structurally cannot see because it only reads <a>.
    Without both, those pagers advanced exactly one page and stopped."""
    from crawler import parse

    hdr = ('<https://api.example.com/i?page=1>; rel="prev", '
           '<https://api.example.com/i?page=3>; rel="next", '
           '<https://api.example.com/i?page=9>; rel="last"')
    both = parse.links_from_link_header(hdr, "https://api.example.com/i?page=2")
    assert "https://api.example.com/i?page=3" in both
    assert "https://api.example.com/i?page=1" in both          # prev is a page too
    assert "https://api.example.com/i?page=9" not in both      # rel=last is not a pager step
    # A walker must be able to ask for next ONLY, or it picks prev and marches backwards.
    assert parse.links_from_link_header(hdr, "https://api.example.com/i", {"next"}) == \
        ["https://api.example.com/i?page=3"]
    assert parse.links_from_link_header(None, "https://x.test/") == []
    assert parse.links_from_link_header("<<<garbage;;rel=", "https://x.test/") == []

    html = ('<html><head><link rel="next" href="/search?start_index=51"></head>'
            '<body><a href="/x?count=25&after=t3_abc" rel="nofollow next">next</a>'
            '<a href="/ordinary">ordinary</a></body></html>')
    rel = parse.extract_rel_links(html, "https://s.test/")
    assert "https://s.test/search?start_index=51" in rel, "head <link rel=next> was missed"
    # rel is a TOKEN LIST: rel="nofollow next" is old.reddit's real next button and a naive
    # rel="next" regex does not match it.
    assert "https://s.test/x?count=25&after=t3_abc" in rel
    assert "https://s.test/ordinary" not in rel


def test_cursor_pagers_are_recognised_as_pagination():
    """Cursor/keyset pagers carry an opaque token, so the numeric `=\d+` shape test never matched:
    they were enqueued as ordinary FACET params, which means not front-loaded, counted as depth+1,
    and capped at CRAWLER_MAX_QUERY_VARIANTS=20 — the cap that killed a long chain 20 pages in."""
    from crawler.async_engine import _looks_paginated
    for u in ("https://old.reddit.com/r/p/?count=25&after=t3_abc",
              "https://en.wikipedia.org/w/index.php?title=C&pagefrom=Aar",
              "https://www.gutenberg.org/ebooks/search?start_index=51",
              "https://api.x.test/v1/items?cursor=eyJhIjoxfQ",
              "https://x.test/l?page=2"):
        assert _looks_paginated(u), u
    # Negative controls: a page SIZE is not a page index, and neither is prose or a stray param.
    for u in ("https://x.test/l?per_page=50", "https://x.test/l?pagesize=50",
              "https://x.test/blog/post-after-the-war", "https://x.test/l?sort=after"):
        assert not _looks_paginated(u), u


def test_trailing_slash_is_kept_when_there_is_a_query_string():
    """canonicalize_url stripped it unconditionally. Measured on gutenberg.org, which serves
    /ebooks/search/?query=history (19,652 bytes) and answers /ebooks/search?query=history with a
    404 — so the canonical URL the crawler fetched was a page that does not exist."""
    from crawler.canonicalize import canonicalize_url
    assert canonicalize_url("https://x.test/search/?q=a") == "https://x.test/search/?q=a"
    # ...but a BARE path still collapses, which is the dedup win the stripping exists for.
    assert canonicalize_url("https://x.test/foo/") == "https://x.test/foo"
    assert canonicalize_url("https://x.test/") == "https://x.test/"


def test_follow_gate_matching_nothing_falls_back_instead_of_dead_ending():
    """A filter that matches NO link on a page must not end the crawl at the seed.

    link_relevance_keywords reads ANCHOR TEXT, so a portal whose navigation is in another
    language matches nothing and the crawl stops having fetched one page. Measured 2026-08-18
    against two other crawlers on eeslindia.org, whose nav anchors are Hindi and generic
    English: ours took 1 page where Firecrawl and Scrapling each took 10, on a site that was
    neither blocking nor empty.

    The fallback is deliberately narrow — it applies only when the filter emptied the page.
    A page where SOMETHING matched keeps being filtered, which is what stops the newspaper
    crawl this gate exists to prevent.
    """
    eng = _eng()
    ctx = _ctx(_job(link_relevance_keywords=["ordnance", "naval"]))
    # Nothing here mentions a keyword — the real eeslindia.org shape.
    html = """<html><body>
      <a href="https://ex.com/about">हमारे बारे में</a>
      <a href="https://ex.com/home">Home</a>
      <a href="https://ex.com/contact">Contact Us</a>
    </body></html>"""
    eng._enqueue_links(ctx, _fr(html), "https://ex.com/", depth=0)
    urls = []
    while not eng.frontier.empty():
        urls.append(eng.frontier.get_nowait().url)
    assert len(urls) == 3, f"a zero-match SEED must still be walked, got {urls}"

    # ...but NOT deeper. A zero-match page at depth >= 1 is chrome, and expanding it floods
    # the frontier. Measured on mahatenders.gov.in: the depth-1 chrome (Awards, Debarment
    # List, Cancelled Tenders) matched nothing, the fallback fired there, and its links
    # crowded out FrontEndListTendersbyDate — the crawl grew from 3 pages to 10 and the
    # e-bus tender yield fell from 1 to 0.
    eng3 = _eng()
    ctx3 = _ctx(_job(link_relevance_keywords=["ordnance", "naval"]))
    eng3._enqueue_links(ctx3, _fr(html), "https://ex.com/chrome", depth=1)
    deep = []
    while not eng3.frontier.empty():
        deep.append(eng3.frontier.get_nowait().url)
    assert deep == [], f"chrome at depth 1 was expanded — that is the frontier flood: {deep}"

    # ...and a page where the filter DID match keeps its focus: no fallback, no chrome.
    eng2 = _eng()
    ctx2 = _ctx(_job(link_relevance_keywords=["ordnance", "naval"]))
    html2 = """<html><body>
      <a href="https://ex.com/t/1">Supply Of Ordnance Stores</a>
      <a href="https://ex.com/careers">Careers</a>
    </body></html>"""
    eng2._enqueue_links(ctx2, _fr(html2), "https://ex.com/list", depth=0)
    urls2 = []
    while not eng2.frontier.empty():
        urls2.append(eng2.frontier.get_nowait().url)
    assert any("/t/1" in u for u in urls2), urls2
    assert not any("careers" in u for u in urls2), \
        f"fallback fired on a page that had a match — the gate is now useless: {urls2}"

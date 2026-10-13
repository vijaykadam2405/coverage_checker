"""Unit tests for the mechanical building blocks."""
from crawler.canonicalize import canonicalize_url, same_site
from crawler.dedup import StoredPage, classify
from crawler.fetcher import FetchResult, Fetcher
from crawler.gate import GateResult, evaluate
from crawler.harvest import _link_is_relevant
from crawler.models import Job
from crawler.parse import extract_links_with_text, visible_text
from crawler import keywords as kwmod


# --- canonicalize --------------------------------------------------------
def test_canonicalize_collapses_equivalents():
    # Normalizes scheme/host case, strips tracking params + fragment, trims trailing slash.
    # www is PRESERVED in the fetch url (stripping it breaks apex-path-redirect sites);
    # www is only collapsed for same_site()/domain matching, not here.
    a = canonicalize_url("https://www.Janes.com/a/?utm_source=x#frag")
    assert a == "https://www.janes.com/a"
    assert canonicalize_url("https://janes.com/a/") == "https://janes.com/a"
    assert same_site("https://www.janes.com/a", "https://janes.com/a")   # still same site


def test_same_site():
    assert same_site("https://idrw.org/x", "https://www.idrw.org/y")
    assert not same_site("https://idrw.org/x", "https://janes.com/y")


def test_canonicalize_never_raises_on_malformed():
    # A malformed href (bad port / bad IPv6) must NOT raise — else one such link on a page
    # aborts the whole enqueue loop and the crawl dead-ends shallow. Returns best-effort.
    for bad in ("http://h:80800/x", "http://h:abc/x", "http://[:::1]/x",
                "https://user:p@ss@host/x", "", "notaurl"):
        canonicalize_url(bad)                    # just must not raise
    assert canonicalize_url("http://h:80800/x").startswith("http://h")
    assert same_site("http://h:80800/x", "http://h:99999/y")   # port stripped, same host


def test_visible_text_strips_nav_footer():
    html = ("<html><body>"
            "<nav>Home | About | Acme Defence Systems | Contact</nav>"
            "<article>The main story is about K9 Vajra guns.</article>"
            "<footer>Acme Defence Systems | Privacy</footer>"
            "</body></html>")
    text = visible_text(html)
    assert "K9 Vajra" in text
    assert "Acme Defence Systems" not in text


# --- gate ----------------------------------------------------------------
def _job(**kw):
    base = dict(job_id="j", job_type="news", seed_urls=["https://x"], keywords=["artillery"])
    base.update(kw)
    return Job(**base)


def test_gate_drops_no_corpus_keyword():
    # The gate matches the GLOBAL corpus (kp), not job.keywords.
    kp = kwmod.from_list(["howitzer", "artillery"])
    g = evaluate(_job(), "Cooking recipes", "no defence here", None, kp)
    assert not g.keep and g.reason == "no_keyword_match"


def test_binary_download_never_render_falls_through(monkeypatch):
    # Regression: a render_js job fetching a PDF must NOT fall through to the
    # Playwright renderer (which throws "Download is starting" and yields None).
    # A successful non-HTML download short-circuits before the render fallback.
    f = Fetcher(user_agent="x", delay_s=0, render_js=True)
    f.allow_network = True
    f.prefer_fixtures = False
    f._robots = None
    pdf = FetchResult(url="https://x/f.pdf", final_url="https://x/f.pdf", status=200,
                      kind="pdf", text_html=None, body_bytes=b"%PDF-1.4 ...",
                      fetched_at="2026-01-01T00:00:00Z")
    monkeypatch.setattr(f, "_http_fetch", lambda *a, **k: pdf)
    # if the guard regresses, _render_fetch runs and returns this sentinel error
    monkeypatch.setattr(f, "_render_fetch",
                        lambda *a, **k: FetchResult(url="https://x/f.pdf",
                            final_url="https://x/f.pdf", status=None,
                            fetched_at="", error="RENDER_SHOULD_NOT_RUN"))
    res = f.fetch("https://x/f.pdf")
    assert res.kind == "pdf" and res.body_bytes and res.error is None


def test_fetch_asset_never_renders(monkeypatch):
    # fetch_asset() is httpx-only — even on render_js it must not call the
    # renderer (assets are files, not pages).
    f = Fetcher(user_agent="x", delay_s=0, render_js=True)
    f.allow_network = True
    f.prefer_fixtures = False
    f._robots = None
    img = FetchResult(url="https://x/a.jpg", final_url="https://x/a.jpg", status=200,
                      kind="image", body_bytes=b"\xff\xd8jpeg", fetched_at="t")
    monkeypatch.setattr(f, "_http_fetch", lambda *a, **k: img)

    def _boom(*a, **k):
        raise AssertionError("_render_fetch must not run for an asset")
    monkeypatch.setattr(f, "_render_fetch", _boom)
    res = f.fetch_asset("https://x/a.jpg")
    assert res.kind == "image" and res.body_bytes


def test_asset_delay_is_lighter_than_page_delay():
    # Same-page asset sub-fetches throttle on a smaller delay than page loads.
    f = Fetcher(user_agent="x", delay_s=2.0)
    assert f.asset_delay_s < f.delay_s
    # env override respected
    import os
    os.environ["CRAWLER_ASSET_DELAY"] = "0.1"
    try:
        f2 = Fetcher(user_agent="x", delay_s=2.0)
        assert f2.asset_delay_s == 0.1
    finally:
        del os.environ["CRAWLER_ASSET_DELAY"]


def test_screenshot_wanted_flag_defaults_off():
    assert Fetcher(user_agent="x").screenshot_wanted is False
    assert Fetcher(user_agent="x", screenshot_wanted=True).screenshot_wanted is True


def test_fetch_assets_parallel_preserves_order_and_bytes(monkeypatch):
    # Concurrent asset download must return results in the SAME order as the
    # input URLs and the SAME bytes as a per-URL fetch (just faster).
    f = Fetcher(user_agent="x", delay_s=0)
    f.allow_network = True
    f.prefer_fixtures = False
    f._robots = None
    bodies = {f"https://x/{i}.jpg": bytes([i]) * 4 for i in range(10)}

    def _fake_http(canon, *a, **k):
        return FetchResult(url=canon, final_url=canon, status=200, kind="image",
                           body_bytes=bodies[canon], fetched_at="t")
    monkeypatch.setattr(f, "_http_fetch", _fake_http)
    monkeypatch.setenv("CRAWLER_ASSET_WORKERS", "4")
    urls = list(bodies)
    out = f.fetch_assets(urls)
    assert [r.url for r in out] == urls              # order preserved
    assert [r.body_bytes for r in out] == [bodies[u] for u in urls]  # bytes intact


def test_fetch_assets_respects_robots(monkeypatch):
    # Even in the parallel path, a robots-blocked URL is not downloaded.
    f = Fetcher(user_agent="x", delay_s=0)
    f.allow_network = True
    f.prefer_fixtures = False

    class _Robots:
        def allowed(self, url):
            return "blocked" not in url
    f._robots = _Robots()
    monkeypatch.setattr(f, "_http_fetch",
                        lambda canon, *a, **k: FetchResult(url=canon, final_url=canon,
                            status=200, kind="image", body_bytes=b"ok", fetched_at="t"))
    monkeypatch.setenv("CRAWLER_ASSET_WORKERS", "4")
    out = f.fetch_assets(["https://x/ok.jpg", "https://x/blocked.jpg"])
    assert out[0].body_bytes == b"ok"
    assert out[1].body_bytes is None and out[1].error == "blocked_by_robots"


def test_fetch_assets_empty_and_single():
    f = Fetcher(user_agent="x", delay_s=0)
    f.allow_network = True
    f.prefer_fixtures = False
    f._robots = None
    assert f.fetch_assets([]) == []


def test_gate_keeps_on_corpus_hit():
    kp = kwmod.from_list(["artillery"])
    g = evaluate(_job(), "Generic artillery musings", "artillery in general", None, kp)
    assert g.keep and g.reason == "keyword_match"
    assert "artillery" in g.matched_keywords


def test_gate_empty_corpus_fails_open():
    # A misconfigured/empty corpus must keep every page, not silently drop all.
    kp = kwmod.from_list([])
    g = evaluate(_job(), "anything", "no keywords loaded", None, kp)
    assert g.keep and g.reason == "no_corpus_keep_all"


def test_gate_drops_stale_beyond_freshness():
    kp = kwmod.from_list(["artillery"])
    g = evaluate(_job(freshness_days=7), "artillery", "artillery news",
                 "2000-01-01T00:00:00Z", kp)
    assert not g.keep and g.reason == "stale_beyond_freshness_days"


# --- change detection ----------------------------------------------------
def test_classify_verdicts():
    assert classify(None, status=200, content_hash="sha256:a") == "new"
    stored = StoredPage("u", "sha256:a", '"strong"', None, False, 200)
    assert classify(stored, status=200, content_hash="sha256:a") == "unchanged"
    assert classify(stored, status=200, content_hash="sha256:b") == "changed"
    assert classify(stored, status=404, content_hash=None) == "gone"
    # a 304 is trusted only with a strong etag
    assert classify(stored, status=304, content_hash=None) == "unchanged"
    weak = StoredPage("u", "sha256:a", 'W/"weak"', None, False, 200)
    assert classify(weak, status=304, content_hash=None) == "changed"


# --- crawl provenance: depth + parent_url reach the Document --------------
def test_build_document_stamps_depth_and_parent():
    # depth + parent_url are tracked in the engine but were dropped at build_document;
    # they must now land on the emitted Document so the 9090 tree/backtracking works.
    from crawler.extract import build_document
    from crawler.harvest import HarvestedPage
    from crawler.seed import load_seed
    seed = load_seed()
    job = Job(job_id="d", job_type="news", seed_urls=["https://ex.com/"],
              keywords=["artillery"], capture=["html", "text"])
    body = ("The ministry has awarded a major artillery contract for new howitzers "
            "to be delivered over the next three years. ") * 6
    html = (f"<html><head><title>Artillery deal</title></head><body>"
            f"<h1>Artillery deal</h1><p>{body}</p></body></html>")
    fr = FetchResult(url="https://ex.com/news/1", final_url="https://ex.com/news/1",
                     status=200, kind="html", text_html=html, fetched_at="2026-01-01T00:00:00Z")
    fetcher = Fetcher(user_agent="x", delay_s=0)

    child = build_document(job, HarvestedPage(url="https://ex.com/news/1", depth=2, fetch=fr,
                                              parent_url="https://ex.com/news"),
                           seed, fetcher, enrich=False)
    assert child is not None and child.depth == 2 and child.parent_url == "https://ex.com/news"

    # a seed root carries depth 0 / parent None (the default)
    root = build_document(job, HarvestedPage(url="https://ex.com/", depth=0, fetch=fr),
                          seed, fetcher, enrich=False)
    assert root is not None and root.depth == 0 and root.parent_url is None


def test_ctx_error_skip_recovered_split():
    # The old lumped "errors" is split: real failures (errors), breaker/policy skips (skipped),
    # fallback recoveries (recovered). errors_by_reason still holds every typed reason.
    from crawler.async_engine import JobCtx
    from crawler.seed import load_seed
    from crawler import keywords as kwmod
    job = Job(job_id="c", job_type="news", seed_urls=["https://x"], keywords=["k"])
    ctx = JobCtx(job, load_seed(), kwmod.from_list([]), forward=False)
    ctx.bump_error("dns"); ctx.bump_error("dns")
    ctx.bump_skip("skipped_host_down"); ctx.bump_skip("skipped_host_down"); ctx.bump_skip("skipped_gone")
    ctx.bump_recovered("served_from_archive")
    s = ctx.summary()
    assert s["errors"] == 2 and s["skipped"] == 3 and s["recovered"] == 1
    assert s["errors_by_reason"]["dns"] == 2 and s["errors_by_reason"]["skipped_host_down"] == 2


def test_served_by_flows_to_document():
    # Each crawl tier stamps served_by; build_document defaults C1 to c1_live and carries the
    # archive/source URL for C2/C3 so the 9090 dashboard can badge it.
    from crawler.extract import build_document
    from crawler.harvest import HarvestedPage
    from crawler.seed import load_seed
    seed = load_seed()
    job = Job(job_id="s", job_type="news", seed_urls=["https://ex.com/"], keywords=["a"],
              capture=["html", "text"])
    body = "Artillery contract awarded to the ministry for new howitzers. " * 6
    html = f"<html><head><title>T</title></head><body><h1>T</h1><p>{body}</p></body></html>"
    fr = FetchResult(url="https://ex.com/a", final_url="https://ex.com/a", status=200,
                     kind="html", text_html=html, fetched_at="t")
    fetcher = Fetcher(user_agent="x", delay_s=0)
    d1 = build_document(job, HarvestedPage(url="https://ex.com/a", depth=1, fetch=fr),
                        seed, fetcher, enrich=False)
    assert d1.served_by == "c1_live" and d1.served_from is None
    d2 = build_document(job, HarvestedPage(url="https://ex.com/a", depth=1, fetch=fr,
                                           served_by="c2_wayback",
                                           served_from="http://web.archive.org/web/x"),
                        seed, fetcher, enrich=False)
    assert d2.served_by == "c2_wayback" and d2.served_from == "http://web.archive.org/web/x"


def test_dashboard_site_depth_navigation(monkeypatch):
    # Navigation is site folder → depth levels → pages at that depth. Cards show url + timestamp
    # + data, with NO source/tier/aggregator labels.
    from ingest_api import dashboard
    recs = [
        {"ingested_at": "t", "document": {
            "url": "https://ex.com/", "title": "Home", "main_text": "root", "html": "<p>x</p>",
            "depth": 0, "parent_url": None, "fetched_at": "2026-07-14T00:00:00Z"}},
        {"ingested_at": "t", "document": {
            "url": "https://ex.com/news/1", "title": "Deal", "main_text": "child", "html": "<p>y</p>",
            "depth": 1, "parent_url": "https://ex.com/", "fetched_at": "2026-07-14T00:00:00Z",
            "source_type": "aggregator", "source_tier": 3,
            "served_by": "c2_wayback", "served_from": "http://web.archive.org/web/1/x"}},
        {"ingested_at": "t", "document": {
            "url": "https://ex.com/news/2", "title": "Deal2", "main_text": "child2", "html": "<p>z</p>",
            "depth": 1, "parent_url": "https://ex.com/", "fetched_at": "2026-07-14T00:00:00Z"}},
    ]
    monkeypatch.setattr(dashboard, "load_records", lambda: recs)

    idx = dashboard.render()                              # site index
    assert "Sites" in idx and "ex.com" in idx and "3 pages" in idx

    depths = dashboard.render(folder="ex.com")           # the site's depth levels
    assert "Depth levels" in depths
    assert "depth 0" in depths and "depth 1" in depths and "2 pages" in depths

    pages = dashboard.render(folder="ex.com", depth="1")  # every page at depth 1
    assert pages.count('class="card"') == 2               # only the 2 depth-1 pages
    assert "https://ex.com/news/1" in pages and "https://ex.com/news/2" in pages
    assert "crawled 2026-07-14" in pages                  # timestamp shown
    assert "aggregator" not in pages                      # source labels stripped
    assert "C2 · wayback" in pages                        # tier badge for the wayback-served page
    assert "web.archive.org/web/1/x" in pages             # links to the archive snapshot


def test_dashboard_merges_www_and_apex(monkeypatch):
    # Keeping www in fetch URLs must NOT split a site into two folders: www.rusi.org + rusi.org
    # group under ONE registered-domain folder (display-only; stored URLs keep their www).
    from ingest_api import dashboard
    recs = [
        {"ingested_at": "t", "document": {"url": "https://www.rusi.org/a", "depth": 1, "html": "<p>a</p>"}},
        {"ingested_at": "t", "document": {"url": "https://rusi.org/b", "depth": 2, "html": "<p>b</p>"}},
    ]
    monkeypatch.setattr(dashboard, "load_records", lambda: recs)
    idx = dashboard.render()
    assert "rusi.org" in idx and "2 pages" in idx        # one folder, both pages
    assert "www.rusi.org" not in idx                      # not a separate folder
    folder = dashboard.render(folder="rusi.org")
    assert "depth 1" in folder and "depth 2" in folder    # both www + apex pages land inside


def test_load_records_tail_bounded_and_strips_html(monkeypatch, tmp_path):
    # The whole-file read OOM'd 9090; load_records must keep only the last N lines and drop the
    # heavy inline html (retaining its length for the size stat).
    import json as _json

    from crawler import config
    from ingest_api import dashboard
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    with open(tmp_path / "ingested.ndjson", "w", encoding="utf-8") as fh:
        for i in range(10):
            fh.write(_json.dumps({"ingested_at": "t", "document": {
                "url": f"https://ex.com/{i}", "html": "<html>" + "x" * 2000 + "</html>"}}) + "\n")
    recs = dashboard.load_records(limit=3)
    assert [r["document"]["url"] for r in recs] == \
        ["https://ex.com/7", "https://ex.com/8", "https://ex.com/9"]      # last 3 only
    doc = recs[-1]["document"]
    assert doc["html"] == "" and doc["html_len"] > 2000                    # stripped, length kept


def test_aimd_limit_direction_and_bounds():
    from crawler.async_engine import _aimd_limit
    assert _aimd_limit(cpu=50, limit=10, target=90, step_up=2, lo=4, hi=96) == 12   # headroom → +step
    assert _aimd_limit(cpu=95, limit=40, target=90, step_up=2, lo=4, hi=96) == 34   # over → ×0.85
    assert _aimd_limit(cpu=88, limit=20, target=90, step_up=2, lo=4, hi=96) == 20   # deadband → hold
    assert _aimd_limit(cpu=10, limit=96, target=90, step_up=2, lo=4, hi=96) == 96   # clamp to hi
    assert _aimd_limit(cpu=100, limit=4, target=90, step_up=2, lo=4, hi=96) == 4    # clamp to lo


def test_adaptive_gate_blocks_at_limit_and_wakes_on_grow():
    import asyncio

    from crawler.async_engine import AdaptiveGate

    async def go():
        gate = AdaptiveGate(initial=2, min_limit=1, max_limit=8)
        release = asyncio.Event()
        started = []

        async def worker(i):
            async with gate.slot():
                started.append(i)
                await release.wait()          # hold the permit until released

        tasks = [asyncio.create_task(worker(i)) for i in (1, 2, 3)]
        await asyncio.sleep(0.05)
        assert sorted(started) == [1, 2] and gate.active == 2   # only 2 admitted at limit=2
        await gate.set_limit(3)                                 # grow → the 3rd wakes into headroom
        await asyncio.sleep(0.05)
        assert 3 in started and gate.active == 3
        release.set()
        await asyncio.gather(*tasks)
        assert gate.active == 0
    asyncio.run(go())


def test_shard_jobs_keeps_each_host_in_one_shard():
    # Multi-process sharding must keep every seed host within ONE worker (else two workers crawl
    # the same URLs / fight over dedup), and lose no jobs.
    from crawler.async_engine import _shard_jobs
    from crawler.models import Job

    def mk(host, n):
        return [Job(job_id=f"{host}-{i}", job_type="profile", seed_urls=[f"https://{host}/{i}"])
                for i in range(n)]
    jobs = mk("a.com", 3) + mk("b.com", 2) + mk("c.com", 1)
    shards = _shard_jobs(jobs, 2)
    assert sum(len(s) for s in shards) == 6                      # no jobs lost
    for host in ("a.com", "b.com", "c.com"):
        where = {si for si, s in enumerate(shards) for j in s if host in j.seed_urls[0]}
        assert len(where) == 1, f"{host} was split across shards"


def test_sitemap_norm_and_coverage(monkeypatch):
    from crawler import sitemap
    assert sitemap.norm_url("https://www.Ex.com/A/") == "ex.com/A"         # host lower, path kept
    assert sitemap.norm_url("http://ex.com/p?q=1#f") == "ex.com/p"         # query + fragment dropped
    monkeypatch.setattr(sitemap, "fetch_sitemap_urls",
                        lambda host, max_urls=50000: {"ex.com/a", "ex.com/b", "ex.com/c"})
    cov = sitemap.coverage("ex.com", {"https://www.ex.com/a", "https://ex.com/b"})
    assert cov["sitemap_total"] == 3 and cov["covered"] == 2
    assert cov["coverage_pct"] == 67 and cov["missed"] == ["ex.com/c"]     # the one not crawled


# --- auto render-mode (httpx-first, render only JS-dependent pages) --------
def test_needs_render_heuristic():
    from crawler.async_engine import _needs_render

    def html_fr(html):
        return FetchResult(url="https://x/", final_url="https://x/", status=200, kind="html",
                           text_html=html, fetched_at="t")

    # real static page (plenty of text + links) → httpx is enough, don't render
    real = ("<html><body><p>" + ("Defence procurement news. " * 40) + "</p>"
            '<a href="/a">A</a><a href="/b">B</a><a href="/c">C</a><a href="/d">D</a>'
            "</body></html>")
    assert _needs_render(html_fr(real)) is False
    # SPA shell: empty app root, no server-rendered content → needs a browser
    assert _needs_render(html_fr('<html><body><div id="root"></div></body></html>')) is True
    # thin body + no links → probably JS
    assert _needs_render(html_fr('<html><body><p>hi</p></body></html>')) is True
    # failure / WAF block → render (a browser may clear it)
    assert _needs_render(FetchResult(url="https://x/", final_url="https://x/", status=403,
                                     kind="html", text_html="denied", fetched_at="t")) is True
    assert _needs_render(FetchResult(url="https://x/", final_url="https://x/", status=None,
                                     fetched_at="t", error="boom")) is True
    # a downloaded PDF is already the content → never render
    assert _needs_render(FetchResult(url="https://x/f.pdf", final_url="https://x/f.pdf", status=200,
                                     kind="pdf", body_bytes=b"%PDF", fetched_at="t")) is False


def test_note_render_escalates_host():
    # A host whose early pages mostly need the browser (httpx keeps coming up empty) escalates
    # to always-render; a host httpx serves fine never does.
    from crawler.async_engine import AsyncEngine
    eng = AsyncEngine.__new__(AsyncEngine)
    eng.host_render_always = set()
    eng.host_render_stat = {}
    for r in (True, True, False, True):          # 3 of the first 4 needed JS
        eng._note_render("spa.example", r)
    assert "spa.example" in eng.host_render_always
    for r in (False, False, False, False, False):
        eng._note_render("static.example", r)
    assert "static.example" not in eng.host_render_always


def test_render_mode_selection(monkeypatch):
    from crawler.async_engine import _render_mode
    j_render = Job(job_id="a", job_type="news", seed_urls=["https://x"], keywords=["k"], render_js=True)
    j_plain = Job(job_id="b", job_type="news", seed_urls=["https://x"], keywords=["k"], render_js=False)
    monkeypatch.delenv("CRAWLER_RENDER_MODE", raising=False)
    assert _render_mode(j_render) == "always"        # explicit per-job opt-in wins
    assert _render_mode(j_plain) == "auto"           # new default
    monkeypatch.setenv("CRAWLER_RENDER_MODE", "never")
    assert _render_mode(j_plain) == "never"
    assert _render_mode(j_render) == "always"        # render_js=true still forces always


# --- link-text relevance (opt-in) -----------------------------------------
def test_extract_links_with_text():
    html = ('<a href="/howitzer-news">Howitzer Programme Update</a>'
            '<a href="/careers">Careers</a>')
    pairs = extract_links_with_text(html, "https://x.example/")
    urls = {u: t for u, t in pairs}
    assert urls["https://x.example/howitzer-news"] == "Howitzer Programme Update"
    assert urls["https://x.example/careers"] == "Careers"
    assert _link_is_relevant(urls["https://x.example/howitzer-news"], ["howitzer"])
    assert not _link_is_relevant(urls["https://x.example/careers"], ["howitzer"])


# --- hardware-utilization upgrades ----------------------------------------
def test_depth_ok_sentinel():
    # max_depth < 0 = UNLIMITED; 0 = seed only; N = follow while depth < N.
    from crawler.async_engine import _depth_ok
    assert _depth_ok(0, -1) and _depth_ok(999, -1)          # unlimited
    assert not _depth_ok(0, 0)                              # seed only
    assert _depth_ok(0, 2) and _depth_ok(1, 2) and not _depth_ok(2, 2)


def test_aimd_ram_brake():
    from crawler.async_engine import _aimd_limit
    # RAM over the hard brake → shrink even with CPU headroom (shed load before OOM).
    assert _aimd_limit(cpu=10, limit=40, target=90, step_up=2, lo=4, hi=96,
                       ram=95, ram_target=85, ram_hard=92) == 34    # ×0.85
    # CPU headroom but RAM over target → hold (don't grow into RAM pressure).
    assert _aimd_limit(cpu=10, limit=40, target=90, step_up=2, lo=4, hi=96,
                       ram=88, ram_target=85, ram_hard=92) == 40
    # Both CPU and RAM have headroom → grow.
    assert _aimd_limit(cpu=10, limit=40, target=90, step_up=2, lo=4, hi=96,
                       ram=50, ram_target=85, ram_hard=92) == 42
    # No RAM args → unchanged pure-CPU behavior (defaults make RAM a no-op).
    assert _aimd_limit(cpu=50, limit=10, target=90, step_up=2, lo=4, hi=96) == 12


def test_host_render_persistence(tmp_path, monkeypatch):
    # A host escalated to always-render is remembered across runs (skip the wasted httpx probe).
    from crawler import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.sqlite")
    from crawler.dedup import CrawlHistory
    h = CrawlHistory()
    assert h.get_render_hosts() == set()
    h.set_render_host("spa.example", True)
    h.set_render_host("static.example", False)
    assert h.get_render_hosts() == {"spa.example"}
    h.close()
    h2 = CrawlHistory()                                     # survives reopen (persisted)
    assert "spa.example" in h2.get_render_hosts()
    h2.close()


def test_route_request_blocks_assets():
    # The wire-level gate: abort image/media/font when blocking is on, always abort giant binaries,
    # never block a careful host's assets (fingerprint), and continue everything else.
    import asyncio
    import types

    from crawler import errors
    from crawler.async_engine import AsyncEngine

    class FakeReq:
        def __init__(self, url, rtype):
            self.url, self.resource_type = url, rtype

    class FakeRoute:
        def __init__(self, url, rtype):
            self.request, self.action = FakeReq(url, rtype), None

        async def abort(self):
            self.action = "abort"

        async def continue_(self):
            self.action = "continue"

    async def go():
        eng = AsyncEngine.__new__(AsyncEngine)
        eng._block_assets = True
        eng.host = types.SimpleNamespace(force_careful={"secret.example"})

        async def act(url, rtype):
            r = FakeRoute(url, rtype)
            await eng._route_request(r)
            return r.action

        assert await act("https://x/a.png", "image") == "abort"        # image blocked
        assert await act("https://x/p", "document") == "continue"      # doc allowed
        assert await act("https://x/app.js", "script") == "continue"   # script allowed
        assert await act("https://x/big.zip", "other") == "abort"      # giant binary always
        assert errors.is_careful_host("www.army.mil")                  # precondition
        assert await act("https://www.army.mil/a.png", "image") == "continue"   # .mil exempt
        assert await act("https://secret.example/a.png", "image") == "continue"  # force_careful exempt
        eng._block_assets = False
        assert await act("https://x/a.png", "image") == "continue"     # blocking off → images pass
        assert await act("https://x/big.zip", "other") == "abort"      # giant still blocked
    asyncio.run(go())


def test_finalize_splits_critical_path_and_drains(tmp_path, monkeypatch):
    # _finalize keeps dedup/history on the critical path and hands the enrich+send tail to the
    # finalizer pool; the finalizer drains the queue, sends each kept doc, and updates counters.
    import asyncio
    import types

    from crawler import config, extract
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.sqlite")
    monkeypatch.setattr(extract, "enrich_assets", lambda *a, **k: None)   # no real downloads
    from crawler.async_engine import AsyncEngine
    from crawler.dedup import CrawlHistory

    class FakeIngest:
        def __init__(self):
            self.sent = 0

        def send(self, doc):
            self.sent += 1
            return types.SimpleNamespace(accepted=True)

    async def go():
        eng = AsyncEngine.__new__(AsyncEngine)
        eng._asset_pool = None
        eng.history = CrawlHistory()
        eng._finalize_q = asyncio.Queue(maxsize=16)
        ing = FakeIngest()
        ctx = types.SimpleNamespace(job=types.SimpleNamespace(render_js=False), ingest=ing,
                                    fetcher=None, seen_hashes=set(), kept=0, sent=0, accepted=0,
                                    rejected=0, skipped_unchanged=0, skipped_duplicate=0,
                                    crawl_run_id=None)
        fr = types.SimpleNamespace(status=200, etag=None, last_modified=None, fetched_at="t",
                                   captcha_type=None, proxy_country=None)
        hp = types.SimpleNamespace(fetch=fr)   # HarvestedPage stand-in; _run_finalize reads hp.fetch

        # New content → kept + queued (NOT sent inline — the tab-holding path stays fast).
        await eng._finalize(ctx, types.SimpleNamespace(content_hash="sha256:a"), hp, fr, "https://ex/1")
        assert ctx.kept == 1 and eng._finalize_q.qsize() == 1 and ing.sent == 0
        # Same url+hash again → unchanged → dropped, not re-queued.
        await eng._finalize(ctx, types.SimpleNamespace(content_hash="sha256:a"), hp, fr, "https://ex/1")
        assert ctx.skipped_unchanged == 1 and eng._finalize_q.qsize() == 1

        finalizers = [asyncio.create_task(eng._finalizer_loop()) for _ in range(2)]
        await asyncio.wait_for(eng._finalize_q.join(), timeout=5)     # drain
        for f in finalizers:
            f.cancel()
        await asyncio.gather(*finalizers, return_exceptions=True)
        assert ing.sent == 1 and ctx.sent == 1 and ctx.accepted == 1 and ctx.rejected == 0
        eng.history.close()
    asyncio.run(go())


def test_deadhost_url_requeued_not_dropped(monkeypatch):
    # A cooling host with no C2/C3 recovery REQUEUES its queued URLs (keeps the single-host frontier
    # alive across the 120s cooldown) instead of dropping them as HOST_DOWN — until the wait cap,
    # after which it skips (so a permanently-dead host doesn't loop forever).
    import asyncio

    from crawler import errors
    from crawler.async_engine import AsyncEngine, WorkItem

    # No recovery available: C3 is the ONLY dead-host recovery now (C2 left the ladder — it is an
    # explicit dated mode), and _try_camofox_fallback no-ops unless CAMOFOX_ENABLED=1. This replaces
    # the old CRAWLER_DEADHOST_RECOVER=0 short-circuit, which is gone: one switch, not two that can
    # disagree.
    monkeypatch.delenv("CAMOFOX_ENABLED", raising=False)
    monkeypatch.setenv("CRAWLER_DEADHOST_MAX_WAITS", "4")

    class _Ctx:                                            # ctx is untouched on the requeue path
        def __init__(self):
            self.skips = []
            self.budget_used = 0

        def bump_skip(self, reason):
            self.skips.append(reason)

        # _recover_deadhost reserves before trying C3, and refunds when nothing is recovered.
        def reserve(self):
            self.budget_used += 1
            return True

        def unreserve(self):
            self.budget_used -= 1

    def _eng():
        eng = AsyncEngine.__new__(AsyncEngine)
        eng.frontier = asyncio.Queue()
        eng.dead_hosts = {"ex.com"}
        eng.host_cooldown = {}
        eng.host_fails = {}
        eng._pending_retries = 0
        eng._last_progress = 0.0
        return eng

    async def go():
        eng = _eng()
        eng.host_cooldown["ex.com"] = asyncio.get_running_loop().time() + 100   # still cooling

        # under the cap → requeued (a wait is pending), NOT counted as a skip
        ctx = _Ctx()
        await eng._process(WorkItem("https://ex.com/a", 0, ctx), tab=None)
        assert eng._pending_retries == 1 and ctx.skips == []

        # the scheduled wait actually re-enqueues the URL with deadhost_waits+1
        eng._pending_retries = 0
        eng._schedule_wait(WorkItem("https://ex.com/b", 2, ctx, deadhost_waits=0), delay=0)
        await asyncio.sleep(0.02)
        got = eng.frontier.get_nowait()
        assert got.url == "https://ex.com/b" and got.deadhost_waits == 1 and eng._pending_retries == 0

        # at the cap → dropped as HOST_DOWN (bounded), no further requeue
        ctx2 = _Ctx()
        await eng._process(WorkItem("https://ex.com/c", 0, ctx2, deadhost_waits=4), tab=None)
        assert ctx2.skips == [errors.HOST_DOWN] and eng._pending_retries == 0

    asyncio.run(go())

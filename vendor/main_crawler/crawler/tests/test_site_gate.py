"""Per-page keyword gate: capture ONLY pages that themselves hit >=1 corpus keyword.
Non-hit pages are dropped (not stored/forwarded), but their links are still followed
(enqueue happens before the gate), so the whole site is crawled for discovery.

The gate decision lives in AsyncEngine._process; here we drive _finalize against
in-memory fakes and replicate the per-page branch to assert keep/drop without a browser.
"""
import asyncio

from crawler import keywords as kwmod
from crawler.async_engine import AsyncEngine, JobCtx
from crawler.fetcher import FetchResult
from crawler.harvest import HarvestedPage
from crawler.models import Document, Job
from crawler.seed import load_seed

SEED = load_seed()
CORPUS = kwmod.from_list(["DRDO", "artillery"])   # tiny corpus for the gate


def _job():
    return Job(job_id="sg", job_type="news", seed_urls=["https://ex.com/"],
               keywords=["x"], max_pages=100, max_depth=4,
               capture=["html", "text"])


def _doc(url, text):
    import hashlib
    h = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    return Document(url=url, source_id="ex", title="t", main_text=text,
                    content_hash=h, html="<html></html>",
                    fetched_at="2026-01-01T00:00:00Z")


def _hp(url, depth):
    fr = FetchResult(url=url, final_url=url, status=200, kind="html",
                     text_html="<html></html>", fetched_at="2026-01-01T00:00:00Z")
    return HarvestedPage(url=url, depth=depth, fetch=fr, pdf_links=[],
                         image_candidates=[], media_candidates=[])


class _Ingest:
    def __init__(self):
        self.collected = []

    def send(self, doc):
        self.collected.append(doc)
        class _O:  # noqa
            accepted = True
        return _O()


def _engine(kp):
    eng = AsyncEngine.__new__(AsyncEngine)      # skip __init__ (no playwright)
    from crawler.dedup import CrawlHistory
    eng.history = CrawlHistory(":memory:")
    eng.kp = kp
    return eng


def _ctx(kp):
    ctx = JobCtx(_job(), SEED, kp, forward=False)
    ctx.ingest = _Ingest()
    return ctx


async def _process_gate(eng, ctx, url, depth, text):
    """Replicate _process's per-page gate branch (the part after build_document)."""
    from crawler import gate
    doc = _doc(url, text)
    hp = _hp(url, depth)
    fr = hp.fetch
    g = gate.evaluate(ctx.job, doc.title, doc.main_text, doc.published_at, ctx.kp)
    ctx.bump_reason(g.reason)
    if not g.keep:
        ctx.dropped_by_gate += 1
        return
    await eng._finalize(ctx, doc, hp, fr, url)


def test_only_keyword_pages_are_captured():
    """A page with a keyword is sent; a page without one is dropped — independently."""
    async def run():
        eng = _engine(CORPUS)
        ctx = _ctx(CORPUS)
        await _process_gate(eng, ctx, "https://ex.com/hit", 0, "DRDO tests new gun")
        await _process_gate(eng, ctx, "https://ex.com/miss", 1, "corporate careers page")
        await _process_gate(eng, ctx, "https://ex.com/hit2", 2, "artillery contract signed")
        sent = [d.url for d in ctx.ingest.collected]
        assert sent == ["https://ex.com/hit", "https://ex.com/hit2"], sent
        assert ctx.dropped_by_gate == 1        # the miss page
        assert ctx.kept == 2
    asyncio.run(run())


def test_no_site_wide_unlock():
    """A keyword hit does NOT make later keyword-less pages get captured (per-page, not site)."""
    async def run():
        eng = _engine(CORPUS)
        ctx = _ctx(CORPUS)
        await _process_gate(eng, ctx, "https://ex.com/hit", 0, "DRDO missile")   # hit
        await _process_gate(eng, ctx, "https://ex.com/x", 1, "unrelated cooking") # miss
        assert [d.url for d in ctx.ingest.collected] == ["https://ex.com/hit"]
        assert ctx.dropped_by_gate == 1
    asyncio.run(run())


def test_empty_corpus_keeps_all():
    """No corpus -> gate fails open (keep every page)."""
    async def run():
        eng = _engine(kwmod.from_list([]))
        ctx = _ctx(kwmod.from_list([]))
        await _process_gate(eng, ctx, "https://ex.com/a", 0, "anything at all")
        await _process_gate(eng, ctx, "https://ex.com/b", 1, "no keywords here either")
        assert len(ctx.ingest.collected) == 2
        assert ctx.dropped_by_gate == 0
    asyncio.run(run())


def test_keyword_flag_off_captures_whole_site():
    """keyword=False -> skip matching, keep every page even with zero corpus hits."""
    from crawler import gate
    job = Job(job_id="k", job_type="news", seed_urls=["https://ex.com/"],
              keyword=False, capture=["html", "text"])
    g = gate.evaluate(job, "no keywords in this title", "nothing relevant here", None, CORPUS)
    assert g.keep and g.reason == "keyword_filter_off"


def test_keyword_flag_on_filters_per_page():
    """keyword=True (default) -> a zero-hit page is dropped."""
    from crawler import gate
    job = Job(job_id="k", job_type="news", seed_urls=["https://ex.com/"],
              keyword=True, capture=["html", "text"])
    miss = gate.evaluate(job, "cooking recipes", "no defense words", None, CORPUS)
    hit = gate.evaluate(job, "DRDO news", "artillery test", None, CORPUS)
    assert not miss.keep and miss.reason == "no_keyword_match"
    assert hit.keep and hit.reason == "keyword_match"


if __name__ == "__main__":
    test_only_keyword_pages_are_captured()
    test_no_site_wide_unlock()
    test_empty_corpus_keeps_all()
    print("OK — per-page gate self-check passed")

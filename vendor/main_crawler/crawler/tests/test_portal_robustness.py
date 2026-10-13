"""Phase-2 portal-robustness tests: broadened attachment discovery, PDF table extraction
(graceful), and async form-search + DOM-change pagination. Offline (fake page)."""
import asyncio

from crawler import interaction_async, parse, pdfextract
from crawler.models import InteractionConfig, PaginateConfig, SearchConfig


# ── broadened tender-attachment discovery ────────────────────────────────────
def test_extract_pdf_links_broadened():
    html = """
      <a href="/docs/tender.pdf">pdf</a>
      <a href="/specs.docx">docx</a>
      <a href="/Download.aspx?id=123">gov attach</a>
      <a href="/getfile?doc=9">getfile</a>
      <a href="/page.html">not attach</a>
      <a href="/article">a page</a>
    """
    links = [l.lower() for l in parse.extract_pdf_links(html, "https://x.gov/")]
    assert "https://x.gov/docs/tender.pdf" in links
    assert "https://x.gov/specs.docx" in links
    assert any("download.aspx" in l for l in links)
    assert any("getfile" in l for l in links)
    assert not any(l.endswith("/page.html") for l in links)     # ordinary pages not captured
    assert not any(l.endswith("/article") for l in links)


def test_extract_pdf_links_extensionless_and_download_text():
    # Gov Drupal listings (ddpmod): "Download" links resolve to EXTENSIONLESS
    # /sites/default/files/<hash> PDFs, and some anchors are just labelled "Download"/"RFP" with
    # no extension. Both must be captured (the %PDF magic-check on fetch drops any non-PDF).
    html = """
      <a href="/sites/default/files/abc123/def456">Download</a>
      <a href="/node/99/view">Download</a>
      <a href="/some/report">RFP Volume I</a>
      <a href="/regular-page">Read more</a>
    """
    links = [l.lower() for l in parse.extract_pdf_links(html, "https://x.gov.in/")]
    assert any("/sites/default/files/abc123/def456" in l for l in links)   # Drupal managed file
    assert any("/node/99/view" in l for l in links)                        # anchor labelled "Download"
    assert any("/some/report" in l for l in links)                         # anchor labelled "RFP…"
    assert not any("regular-page" in l for l in links)                     # "Read more" is not an attachment


# ── PDF table extraction degrades gracefully ─────────────────────────────────
def test_extract_tables_graceful():
    assert pdfextract.extract_tables(b"not a pdf at all") == []
    assert pdfextract.extract_tables(b"") == []


# ── has_any gate ─────────────────────────────────────────────────────────────
def test_has_any():
    assert not interaction_async.has_any(None)
    assert not interaction_async.has_any(InteractionConfig())
    cfg = InteractionConfig(search=SearchConfig(
        enabled=True, input_selector="#q", submit_selector="#go", keywords_to_search=["missile"]))
    assert interaction_async.has_any(cfg)


# ── fake async page for interaction logic ────────────────────────────────────
class FakePage:
    def __init__(self, bodies, urls=None):
        self._bodies = bodies
        self._urls = urls or [f"u{i}" for i in range(len(bodies))]
        self._i = 0
        self.url = self._urls[0]

    async def inner_text(self, sel):
        return self._bodies[self._i]

    async def content(self):
        return f"<html>{self._bodies[self._i]}</html>"

    async def click(self, sel, timeout=0):
        self._i = min(self._i + 1, len(self._bodies) - 1)
        self.url = self._urls[self._i]

    async def query_selector(self, sel):
        # _present() gates every interaction on this. Without it the fake raises AttributeError,
        # _present swallows that as "selector absent", and _run_search returns before typing --
        # which silently turned both search tests below into assertions about an empty list.
        return object() if sel else None

    async def fill(self, sel, kw):
        pass

    async def press(self, sel, key):
        pass

    async def wait_for_timeout(self, ms):
        pass

    async def wait_for_load_state(self, s, timeout=0):
        pass


def test_pagination_follows_ajax_then_stops():
    async def go():
        # URL never changes (ASP.NET __doPostBack), content changes then repeats → follow all, stop.
        p = FakePage(bodies=["page one", "page two longer", "page three longest"],
                     urls=["u", "u", "u"])
        cfg = PaginateConfig(enabled=True, next_selector=".next", max_pages=10,
                             wait_network_idle=False)
        texts, _links, _pdfs = await interaction_async._run_pagination(p, cfg)
        assert len(texts) == 3          # followed all 3 despite URL never changing
    asyncio.run(go())


def test_pagination_collects_pdf_links_every_page():
    async def go():
        # Each paginated page carries its own "Download" PDF link. All of them must be collected
        # (not just the last page's) so a paginated tender listing loses no attachments.
        class PdfPage(FakePage):
            async def content(self):
                return f'<html><a href="/files/doc{self._i}.pdf">download</a>{self._bodies[self._i]}</html>'
        p = PdfPage(bodies=["page one", "page two longer"],
                    urls=["https://t.gov/list", "https://t.gov/list"])
        cfg = PaginateConfig(enabled=True, next_selector=".next", max_pages=10, wait_network_idle=False)
        _texts, _links, pdfs = await interaction_async._run_pagination(p, cfg)
        assert any("doc0.pdf" in u for u in pdfs) and any("doc1.pdf" in u for u in pdfs)
    asyncio.run(go())


def test_search_accumulates_per_keyword():
    async def go():
        p = FakePage(bodies=["result A", "result B"])
        cfg = SearchConfig(enabled=True, input_selector="#q", submit_selector="#go",
                           keywords_to_search=["missile", "radar"], pause_ms=0)
        res, _l, _p = await interaction_async._run_search(p, cfg)
        assert len(res) == 2            # one result text per keyword (sync version overwrote)
    asyncio.run(go())


def test_search_press_enter_when_no_submit():
    async def go():
        p = FakePage(bodies=["r1"])
        cfg = SearchConfig(enabled=True, input_selector="#q", submit_selector="",
                           keywords_to_search=["missile"], pause_ms=0)
        res, _l, _p = await interaction_async._run_search(p, cfg)   # no submit_selector → press Enter path
        assert len(res) == 1
    asyncio.run(go())


def test_search_harvests_links_from_every_keyword_not_just_the_last():
    """The last keyword is the one most likely to MISS.

    recipe_for() appends the English "electric bus" to every source's search_terms, so on a
    German or Spanish portal the final DOM is the empty-result page. Harvesting links only from
    that final state -- which is what run_interactions() reads -- threw away every hit the
    earlier, correct-language keywords had already rendered. Measured on service.bund.de:
    Elektrobus and Elektrobusse each return live tenders, and the crawl still ended on
    "Keine Treffer" with zero result links enqueued.
    """
    async def go():
        class SearchPage(FakePage):
            """Serves results for the German term and an empty page for the English one.

            Keyed on the keyword actually typed, not on a click counter: FakePage.click()
            advances its own index, so a counter would conflate 'which keyword' with
            'which pagination step'.
            """
            kw = ""

            async def fill(self, sel, kw):
                type(self).kw = kw

            async def content(self):
                if self.kw == "Elektrobus":
                    return '<html><a href="/IMPORTE/tender-1.html">Elektrobus</a></html>'
                return "<html>Keine Treffer</html>"

        p = SearchPage(bodies=["3 Treffer", "Keine Treffer"],
                       urls=["https://service.bund.de/s", "https://service.bund.de/s"])
        cfg = SearchConfig(enabled=True, input_selector="#q", submit_selector="#go",
                           keywords_to_search=["Elektrobus", "electric bus"], pause_ms=0)
        texts, links, _pdfs = await interaction_async._run_search(p, cfg)
        assert len(texts) == 2
        assert any("tender-1.html" in u for u in links), \
            "the winning keyword's link was dropped in favour of the empty last page"
    asyncio.run(go())

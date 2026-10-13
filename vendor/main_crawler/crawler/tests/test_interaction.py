"""Page-interaction steps (scroll / click / hover / search / paginate).

Playwright isn't driven here — a FakePage stub records the calls interaction.py
makes and serves scripted innerText/scrollHeight, so the real behaviors (scroll
modes, pagination merge, canonical ordering, the no-config no-op) are exercised
offline and fast. Config serialization + the pipeline wiring are tested against
the real models.
"""
from crawler import interaction
from crawler.fetcher import Fetcher
from crawler.models import (
    ClickConfig, HoverConfig, InteractionConfig, Job, PaginateConfig,
    ScrollConfig, SearchConfig,
)


class FakePage:
    """Minimal Playwright-page stand-in: records calls, serves scripted state."""

    def __init__(self, *, body_texts=None, scroll_heights=None, urls=None,
                 html="<html><body>x</body></html>"):
        self.calls: list[tuple] = []
        self._body_texts = list(body_texts or ["page-0"])
        self._body_idx = 0
        self._scroll_heights = list(scroll_heights or [])
        self._urls = list(urls or ["https://x/p0"])
        self._url_idx = 0
        self._html = html

    # --- things interaction.py calls -----------------------------------
    def evaluate(self, script):
        self.calls.append(("evaluate", script))
        # only a bare height *read* returns a height; scrollTo(...) also mentions
        # scrollHeight but must not consume the scripted list.
        if script == "document.body.scrollHeight" and self._scroll_heights:
            return self._scroll_heights.pop(0)
        return None

    def wait_for_timeout(self, ms):
        self.calls.append(("wait", ms))

    def wait_for_load_state(self, state, timeout=None):
        self.calls.append(("load_state", state))

    def viewport_size(self):  # not used (attribute form below), kept for safety
        return {"width": 1920, "height": 1080}

    @property
    def viewport_size(self):
        return {"width": 1920, "height": 1080}

    def click(self, selector, timeout=None):
        self.calls.append(("click", selector))
        # advance URL so pagination sees a change
        if self._url_idx < len(self._urls) - 1:
            self._url_idx += 1

    def hover(self, selector, timeout=None):
        self.calls.append(("hover", selector))

    def fill(self, selector, value):
        self.calls.append(("fill", selector, value))

    def inner_text(self, selector):
        self.calls.append(("inner_text", selector))
        txt = self._body_texts[min(self._body_idx, len(self._body_texts) - 1)]
        self._body_idx += 1
        return txt

    def content(self):
        self.calls.append(("content",))
        return self._html

    @property
    def url(self):
        return self._urls[self._url_idx]


def _names(page):
    return [c[0] for c in page.calls]


# --- 1. scroll reveals content (mode behaviors) --------------------------
def test_scroll_infinite_stops_when_height_stops_growing():
    # heights: 100 -> 200 -> 200 (no growth) -> should stop at the 3rd read
    page = FakePage(scroll_heights=[100, 200, 200, 999, 999])
    cfg = ScrollConfig(enabled=True, mode="infinite", steps=5, pause_ms=0)
    interaction._run_scroll(page, cfg)
    scrolls = [c for c in page.calls if c[0] == "evaluate" and "scrollTo" in c[1]]
    # grew once (100->200), then flat (200==200) -> only 2 scroll actions, not 5
    assert len(scrolls) == 2


def test_scroll_viewport_scrolls_steps_times():
    page = FakePage()
    cfg = ScrollConfig(enabled=True, mode="viewport", steps=3, pause_ms=0,
                       wait_network_idle=False)
    interaction._run_scroll(page, cfg)
    scrolls = [c for c in page.calls if c[0] == "evaluate" and "scrollBy" in c[1]]
    assert len(scrolls) == 3


# --- 2. pagination merges N pages with PAGE_BREAK markers -----------------
def test_pagination_merges_pages_with_break_markers():
    page = FakePage(body_texts=["PAGE-A", "PAGE-B", "PAGE-C"],
                    urls=["https://x/1", "https://x/2", "https://x/3"])
    cfg = InteractionConfig(paginate=PaginateConfig(
        enabled=True, next_selector="a.next", max_pages=3, pause_ms=0,
        wait_network_idle=False))
    html, text = interaction.run_interactions(page, cfg)
    assert text.count("--- PAGE_BREAK ---") == 2      # 3 pages -> 2 breaks
    assert "PAGE-A" in text and "PAGE-B" in text and "PAGE-C" in text


def test_pagination_stops_when_url_unchanged():
    # only one URL -> click never advances it -> stop after page 1
    page = FakePage(body_texts=["ONLY"], urls=["https://x/same"])
    cfg = PaginateConfig(enabled=True, next_selector="a.next", max_pages=5,
                         pause_ms=0, wait_network_idle=False)
    texts = interaction._run_pagination(page, cfg)
    assert texts == ["ONLY"]


# --- 3. no interaction config -> no-op, plain html returned ---------------
def test_no_interaction_returns_html_unchanged():
    page = FakePage(html="<html><body>RAW</body></html>")
    cfg = InteractionConfig()  # all steps None
    html, text = interaction.run_interactions(page, cfg)
    assert html == "<html><body>RAW</body></html>"
    # no interaction ran -> text falls back to html (not inner_text)
    assert text == html
    assert "inner_text" not in _names(page)
    assert "click" not in _names(page) and "hover" not in _names(page)


# --- 4. canonical order Scroll -> Click -> Hover -> Search -> (Paginate) --
def test_steps_run_in_canonical_order():
    page = FakePage(body_texts=["p"], urls=["https://x/1"])
    cfg = InteractionConfig(
        scroll=ScrollConfig(enabled=True, mode="bottom", pause_ms=0,
                            wait_network_idle=False),
        click=ClickConfig(enabled=True, selectors=["#more"], pause_ms=0),
        hover=HoverConfig(enabled=True, selectors=[".menu"], pause_ms=0),
        search=SearchConfig(enabled=True, input_selector="#q",
                            submit_selector="#go", keywords_to_search=["kw"],
                            pause_ms=0),
    )
    interaction.run_interactions(page, cfg)
    order = _names(page)
    i_scroll = order.index("evaluate")         # scroll's scrollTo
    i_click = order.index("click")
    i_hover = order.index("hover")
    i_fill = order.index("fill")               # search fills before its submit-click
    assert i_scroll < i_click < i_hover < i_fill


# --- 5. config serializes through the API Job model + wires to Fetcher ----
def test_interaction_config_roundtrips_and_wires():
    j = Job(job_id="t", job_type="news", seed_urls=["https://x"], keywords=["a"],
            interaction=InteractionConfig(
                scroll=ScrollConfig(enabled=True, mode="infinite", steps=4),
                paginate=PaginateConfig(enabled=True, next_selector="a.next",
                                        max_pages=3)))
    dumped = j.model_dump()
    assert dumped["interaction"]["scroll"]["mode"] == "infinite"
    assert dumped["interaction"]["paginate"]["max_pages"] == 3
    # round-trips back to an equal model
    assert Job(**dumped).interaction.scroll.steps == 4

    f = Fetcher(user_agent="x", interaction_cfg=j.interaction)
    assert f._interaction_cfg is not None
    assert Fetcher._has_any_interaction(j.interaction) is True
    assert Fetcher._has_any_interaction(InteractionConfig()) is False

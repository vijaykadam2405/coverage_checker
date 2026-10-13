"""Check the selector-existence guard in interaction_async._present.

Why this exists: blind click lists are normal (the EV-tender 'spa' recipe carries 7 generic
selectors), and page.click on an absent selector costs the FULL 5s Playwright timeout before
raising. A 175-job sweep spent its wall-clock in those timeouts. _present replaces each of
them with a ~1ms query_selector, so the cost of a miss is a skip instead of a stall.

    python -m pytest test_interaction_present.py      (or: python test_interaction_present.py)
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crawler.interaction_async import _present, _run_search  # noqa: E402


class FakePage:
    """Minimal stand-in. `clicked` records what a real click WOULD have been asked to do, so a
    test can prove the guard skipped rather than clicked."""

    def __init__(self, present_selectors=(), raise_on=()):
        self.present = set(present_selectors)
        self.raise_on = set(raise_on)
        self.queried = []

    async def query_selector(self, sel):
        self.queried.append(sel)
        if sel in self.raise_on:
            raise ValueError(f'Unexpected token "" while parsing selector {sel!r}')
        return object() if sel in self.present else None


def test_present_finds_existing_selector():
    page = FakePage(present_selectors=["a[title='Next']"])
    assert asyncio.run(_present(page, "a[title='Next']")) is True


def test_absent_selector_is_false_and_costs_one_query():
    page = FakePage(present_selectors=[])
    assert asyncio.run(_present(page, "a.next")) is False
    assert page.queried == ["a.next"], "must ask exactly once, never fall through to a click"


def test_empty_selector_never_reaches_the_page():
    """next_selector defaults to "" in crawler/models.py. Before the guard, that produced
    'Page.click: Unexpected token "" while parsing selector ""' on the first paginate
    iteration -- so a search-form job silently returned page 1 only."""
    page = FakePage()
    for empty in ("", "   ", None):
        assert asyncio.run(_present(page, empty)) is False
    assert page.queried == [], "an empty selector must not even be sent to the page"


def test_malformed_selector_is_treated_as_absent_not_raised():
    page = FakePage(raise_on=["::::"])
    assert asyncio.run(_present(page, "::::")) is False


def test_comma_list_is_one_selector():
    """The EV 'search' recipe passes a CSS comma list so one job covers several portals'
    next-page markup. It must be handled as a single selector, not split."""
    sel = "a[title='Next'], a[rel='next'], a.next"
    page = FakePage(present_selectors=[sel])
    assert asyncio.run(_present(page, sel)) is True
    assert page.queried == [sel]


class SearchPage(FakePage):
    """FakePage plus the surface _run_search touches, recording whether it tried to type."""

    def __init__(self, present_selectors=()):
        super().__init__(present_selectors)
        self.filled = []

    async def fill(self, sel, text):
        self.filled.append((sel, text))

    async def wait_for_timeout(self, ms):
        return None

    async def press(self, sel, key):
        return None

    async def click(self, sel, timeout=None):
        return None

    async def wait_for_load_state(self, state, timeout=None):
        return None

    async def inner_text(self, sel):
        return "RESULTS"


class _Cfg:
    def __init__(self, input_selector, keywords):
        self.input_selector = input_selector
        self.submit_selector = "#Go"
        self.keywords_to_search = keywords
        self.pause_ms = 1


def test_search_skips_a_page_with_no_box_instead_of_waiting_30s_per_keyword():
    """THE GePNIC BUG. The search config applies to every page a job walks, but only the
    portal HOME page carries #SearchDescription; its nav pages do not. page.fill takes no
    timeout, so each boxless page cost 30s PER KEYWORD. Measured 2026-08-18: raising the page
    budget from 3 to 10 made the crawl bigger and dropped the EV-tender yield to zero, because
    the run was spent failing to search pages that were never searchable.
    """
    page = SearchPage(present_selectors=[])          # nav page: no search box
    cfg = _Cfg("#SearchDescription", ["electric bus", "e-bus", "electric truck"])
    assert asyncio.run(_run_search(page, cfg)) == ([], [], [])
    assert page.filled == [], "typed into a box that is not there — that is the 30s stall"
    assert page.queried == ["#SearchDescription"], "must cost exactly one ~1ms query"


def test_search_still_runs_where_the_box_exists():
    """...and the guard must not disable the feature on the page that matters."""
    page = SearchPage(present_selectors=["#SearchDescription"])
    cfg = _Cfg("#SearchDescription", ["electric bus"])
    out, _links, _pdfs = asyncio.run(_run_search(page, cfg))
    assert out == ["RESULTS"], out
    assert page.filled == [("#SearchDescription", "electric bus")]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} checks passed")

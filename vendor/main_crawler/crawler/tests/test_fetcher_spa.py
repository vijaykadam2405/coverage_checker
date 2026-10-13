"""Unit tests for the SPA-bootstrap status/final_url fix and the 403 fallback trigger."""
import sys
import types
from unittest.mock import MagicMock

from crawler.fetcher import Fetcher, _BROWSER_UA_FALLBACK


def _make_fake_playwright(pages):
    """Build a fake `playwright.sync_api` module whose page.goto/content/url
    are driven by the `pages` script: a list of dicts consumed in order by
    successive page.goto() calls, each shaped like
    {"status": int, "html": str, "url": str}.
    """
    calls = {"goto": 0}
    page = MagicMock()

    def goto(url, **kwargs):
        step = pages[min(calls["goto"], len(pages) - 1)]
        calls["goto"] += 1
        page.url = step["url"]
        resp = MagicMock()
        resp.status = step["status"]
        return resp

    page.goto.side_effect = goto
    page.content.side_effect = lambda: pages[min(calls["goto"], len(pages) - 1) - 1]["html"]
    page.wait_for_load_state.return_value = None
    page.wait_for_timeout.return_value = None
    page.query_selector.return_value = None  # no matching nav link found by default

    ctx = MagicMock()
    ctx.new_page.return_value = page
    browser = MagicMock()
    browser.new_context.return_value = ctx

    pw = MagicMock()
    pw.chromium.launch.return_value = browser

    fake_module = types.SimpleNamespace(sync_playwright=lambda: _CtxMgr(pw))
    return fake_module, page


class _CtxMgr:
    """Mimics real Playwright's dual API: usable as `with sync_playwright() as pw`
    or as `sync_playwright().start()` (which _render_fetch now uses so it can
    reuse a shared browser without opening a second driver connection)."""
    def __init__(self, pw):
        self._pw = pw

    def __enter__(self):
        return self._pw

    def __exit__(self, *a):
        return False

    def start(self):
        return self._pw

    def stop(self):
        pass


def test_spa_bootstrap_does_not_fake_success_when_navigation_fails(monkeypatch):
    """If the SPA click/pushState never actually changes the page, status must
    stay >=400 instead of being force-set to 200 (the original bug)."""
    fake_module, page = _make_fake_playwright([
        {"status": 404, "html": "<html>tiny 404 shell</html>", "url": "https://spa.example/missing"},
    ])
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    f = Fetcher(user_agent="test-ua", delay_s=0, render_js=True, respect_robots=False)
    result = f._render_fetch("https://spa.example/missing")

    assert result is not None
    assert result.status is not None and result.status >= 400
    assert result.final_url == "https://spa.example/missing"


def test_spa_bootstrap_trusts_verified_navigation(monkeypatch):
    """If content genuinely grows after the bootstrap dance, trust it (status=200)
    and report the real post-navigation final_url."""
    page_script = [
        {"status": 404, "html": "x" * 100, "url": "https://spa.example/product"},
        {"status": 200, "html": "y" * 10000, "url": "https://spa.example/product"},
    ]
    fake_module, page = _make_fake_playwright(page_script)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    # Simulate a nav link being found and clicked successfully.
    link = MagicMock()
    page.query_selector.return_value = link

    f = Fetcher(user_agent="test-ua", delay_s=0, render_js=True, respect_robots=False)
    result = f._render_fetch("https://spa.example/product")

    assert result is not None
    assert result.status == 200
    assert result.final_url == "https://spa.example/product"


def test_403_with_html_body_triggers_render_fallback(monkeypatch):
    """A 403 block page with an HTML body must still trigger the Playwright
    fallback (today's bug: only missing HTML / hard errors triggered it)."""
    import crawler.fetcher as fetcher_mod

    def fake_http_fetch(self, canon, conditional, user_agent=None):
        return fetcher_mod.FetchResult(
            url=canon, final_url=canon, status=403,
            content_type="text/html", kind="html",
            text_html="<html>blocked</html>", fetched_at="now",
        )

    rendered_calls = []

    def fake_render_fetch(self, canon, interaction_cfg=None, user_agent_override=None):
        rendered_calls.append(user_agent_override)
        return fetcher_mod.FetchResult(url=canon, final_url=canon, status=200,
                                       kind="html", text_html="<html>ok</html>",
                                       fetched_at="now")

    monkeypatch.setattr(fetcher_mod.Fetcher, "_http_fetch", fake_http_fetch)
    monkeypatch.setattr(fetcher_mod.Fetcher, "_render_fetch", fake_render_fetch)

    f = Fetcher(user_agent="MalloryBot/1.0", delay_s=0, render_js=True, respect_robots=False,
                prefer_fixtures=False, allow_network=True)
    result = f.fetch("https://blocked.example/page")

    assert result.status == 200
    assert rendered_calls == [_BROWSER_UA_FALLBACK]


def _fake_shared_page(start_url: str):
    """A bare-bones MagicMock page for _click_or_goto tests, with page.url
    mutable via a .click() side effect (simulating in-app navigation)."""
    page = MagicMock()
    page.url = start_url
    page.wait_for_timeout.return_value = None
    page.wait_for_load_state.return_value = None
    page.query_selector.return_value = None
    return page


def test_click_or_goto_succeeds_when_link_present_on_current_page():
    """A live <a href> found on the shared page's current location, clicked
    successfully and landing on the expected path, is trusted (status=200)."""
    page = _fake_shared_page("https://spa.example/")

    link = MagicMock()

    def do_click(**kwargs):
        page.url = "https://spa.example/careers"
    link.click.side_effect = do_click
    page.query_selector.side_effect = lambda sel: link if "careers" in sel else None
    page.content.return_value = "<html>careers page</html>"

    f = Fetcher(user_agent="test-ua", delay_s=0, render_js=True, respect_robots=False)
    f._shared_ctx = (MagicMock(), MagicMock(), MagicMock(), page)

    result = f._click_or_goto("https://spa.example/careers")

    assert result is not None
    assert result.status == 200
    assert result.final_url == "https://spa.example/careers"


def test_click_or_goto_falls_back_when_link_not_on_current_page():
    """BFS visited a different branch first: the shared page is elsewhere and
    no matching link exists there — must return None, not fabricate anything."""
    page = _fake_shared_page("https://spa.example/products")
    page.query_selector.return_value = None  # no link matches on this page

    f = Fetcher(user_agent="test-ua", delay_s=0, render_js=True, respect_robots=False)
    f._shared_ctx = (MagicMock(), MagicMock(), MagicMock(), page)

    result = f._click_or_goto("https://spa.example/careers")

    assert result is None


def test_click_or_goto_falls_back_when_click_does_not_navigate():
    """A link is found and .click() doesn't raise, but the URL never actually
    changed (no-op click / detached handler) — must not report success."""
    page = _fake_shared_page("https://spa.example/")
    link = MagicMock()  # click() succeeds but page.url is left unchanged
    page.query_selector.return_value = link

    f = Fetcher(user_agent="test-ua", delay_s=0, render_js=True, respect_robots=False)
    f._shared_ctx = (MagicMock(), MagicMock(), MagicMock(), page)

    result = f._click_or_goto("https://spa.example/careers")

    assert result is None


def test_fetch_uses_click_mode_then_falls_back_to_render_fetch():
    """fetch(click_mode=True) tries the click path first; when it can't verify
    a navigation, it falls through to the existing render/httpx ladder."""
    import crawler.fetcher as fetcher_mod

    def fake_http_fetch(self, canon, conditional, user_agent=None):
        return fetcher_mod.FetchResult(url=canon, final_url=canon, status=None,
                                       error="no_html", fetched_at="now")

    def fake_render_fetch(self, canon, interaction_cfg=None, user_agent_override=None):
        return fetcher_mod.FetchResult(url=canon, final_url=canon, status=200,
                                       kind="html", text_html="<html>ok</html>",
                                       fetched_at="now")

    monkeypatch_calls = []

    def fake_click_or_goto(self, canon, interaction_cfg=None):
        monkeypatch_calls.append(canon)
        return None  # no matching link — must fall through

    import unittest.mock as um
    with um.patch.object(fetcher_mod.Fetcher, "_http_fetch", fake_http_fetch), \
         um.patch.object(fetcher_mod.Fetcher, "_render_fetch", fake_render_fetch), \
         um.patch.object(fetcher_mod.Fetcher, "_click_or_goto", fake_click_or_goto):
        f = Fetcher(user_agent="test-ua", delay_s=0, render_js=True, respect_robots=False,
                    prefer_fixtures=False, allow_network=True)
        f._shared_ctx = (MagicMock(), MagicMock(), MagicMock(), MagicMock())
        result = f.fetch("https://spa.example/careers", click_mode=True)

    assert monkeypatch_calls == ["https://spa.example/careers"]
    assert result.status == 200
    assert result.text_html == "<html>ok</html>"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))

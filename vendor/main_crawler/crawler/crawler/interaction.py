"""Page interaction orchestration for Playwright render path.

Scroll, click, hover, paginate — execute in fixed canonical order on a Playwright
page before content extraction. Each step is opt-in via InteractionConfig.
"""
from __future__ import annotations

import logging

from .models import InteractionConfig

logger = logging.getLogger(__name__)


def run_interactions(page, config: InteractionConfig) -> tuple[str, str]:
    """Execute interaction steps in canonical order: Scroll -> Click -> Hover -> Paginate.

    Returns (html, text) where:
      - html is the page's raw HTML (for link extraction / BFS crawling)
      - text is the visible innerText (for the text extraction pipeline)

    For pagination, each page's body innerText is concatenated with PAGE_BREAK markers.
    """
    if config.scroll and config.scroll.enabled:
        _run_scroll(page, config.scroll)

    if config.click and config.click.enabled:
        _run_clicks(page, config.click)

    if config.hover and config.hover.enabled:
        _run_hovers(page, config.hover)

    if config.search and config.search.enabled:
        _run_search(page, config.search)

    if config.paginate and config.paginate.enabled:
        texts = _run_pagination(page, config.paginate)
        html = page.content()
        return html, "\n\n--- PAGE_BREAK ---\n\n".join(texts)

    html = page.content()

    # When any interaction ran, use inner_text for content extraction so
    # dynamically-revealed content isn't lost to trafilatura's boilerplate filter.
    had_interaction = any([
        config.scroll and config.scroll.enabled,
        config.click and config.click.enabled,
        config.hover and config.hover.enabled,
        config.search and config.search.enabled,
    ])

    text = page.inner_text("body") if had_interaction else html

    return html, text


def _run_scroll(page, cfg) -> None:
    if cfg.mode == "infinite":
        last_height = page.evaluate("document.body.scrollHeight")
        for _ in range(cfg.steps):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(cfg.pause_ms)
            new_height = page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height
    elif cfg.mode == "bottom":
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(cfg.pause_ms)
    else:  # "viewport"
        viewport = page.viewport_size or {"width": 1920, "height": 1080}
        step_height = viewport["height"]
        for _ in range(cfg.steps):
            page.evaluate(f"window.scrollBy(0, {step_height})")
            page.wait_for_timeout(cfg.pause_ms)

    if cfg.wait_network_idle:
        try:
            page.wait_for_load_state("networkidle", timeout=cfg.network_idle_timeout_ms)
        except Exception:
            logger.warning("Network idle timeout after scroll")


def _run_clicks(page, cfg) -> None:
    for selector in cfg.selectors:
        try:
            page.click(selector, timeout=5000)
            page.wait_for_timeout(cfg.pause_ms)
        except Exception as e:
            logger.warning("Click failed for '%s': %s", selector, e)

    if cfg.wait_network_idle:
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            logger.warning("Network idle timeout after clicks")


def _run_hovers(page, cfg) -> None:
    for selector in cfg.selectors:
        try:
            page.hover(selector, timeout=5000)
            page.wait_for_timeout(cfg.pause_ms)
        except Exception as e:
            logger.warning("Hover failed for '%s': %s", selector, e)

    # After hovering, click any revealed dropdown items
    if hasattr(cfg, 'click_selectors') and cfg.click_selectors:
        for sel in cfg.click_selectors:
            try:
                page.click(sel, timeout=5000)
                page.wait_for_timeout(cfg.pause_ms)
            except Exception as e:
                logger.warning("Hover click failed for '%s': %s", sel, e)


def _run_search(page, cfg) -> None:
    """Fill a search bar, submit, and collect results from the results page."""
    for kw in cfg.keywords_to_search:
        try:
            page.fill(cfg.input_selector, kw)
            page.wait_for_timeout(300)
            page.click(cfg.submit_selector, timeout=5000)
            page.wait_for_timeout(cfg.pause_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
        except Exception as e:
            logger.warning("Search failed for '%s': %s", kw, e)


def _run_pagination(page, cfg) -> list[str]:
    # Use inner_text for ALL pages so they merge cleanly as plain text
    pages_text: list[str] = [page.inner_text("body")]
    current_url = page.url

    for _ in range(1, cfg.max_pages):
        try:
            page.click(cfg.next_selector, timeout=5000)
            page.wait_for_timeout(cfg.pause_ms)

            if cfg.wait_network_idle:
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass

            new_url = page.url
            if new_url == current_url:
                logger.info("Pagination stopped: URL unchanged")
                break

            current_url = new_url
            pages_text.append(page.inner_text("body"))

        except Exception as e:
            logger.info("Pagination ended: %s", e)
            break

    return pages_text

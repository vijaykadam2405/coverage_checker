"""Async twin of interaction.run_interactions for the async_api render pool.

Same canonical order (scroll → click → hover → search → paginate), every call awaited.
Two gov-portal fixes over the sync version:
  - search accumulates per-keyword result text (sync overwrote each keyword);
  - pagination detects DOM change, not just URL change, so ASP.NET __doPostBack /
    AJAX "next" (URL stays constant) is followed instead of stopping at page 1.

This is legitimate front-door interaction (fill a public search box, click next) — no
anti-bot evasion. It's what lets the production pool search a gov tender portal.
"""
from __future__ import annotations

import logging

from . import parse
from .models import InteractionConfig

logger = logging.getLogger(__name__)


def has_any(cfg: InteractionConfig | None) -> bool:
    return bool(cfg and (
        (cfg.scroll and cfg.scroll.enabled)
        or (cfg.click and cfg.click.enabled)
        or (cfg.hover and cfg.hover.enabled)
        or (cfg.search and cfg.search.enabled)
        or (cfg.paginate and cfg.paginate.enabled)
    ))


async def run_interactions(page, config: InteractionConfig) -> tuple[str, str, list[str], list[str]]:
    """Run interaction steps on an async Playwright page. Returns
    (html, text, extra_links, extra_pdf_links): html for link/BFS extraction (final page state),
    text = merged innerText across search/pagination, extra_links = <a href> found on pagination
    pages 2+ (enqueued too), extra_pdf_links = Download/PDF links found on pagination pages 2+
    (downloaded too, so a paginated tender listing's earlier-page PDFs aren't lost)."""
    if config.scroll and config.scroll.enabled:
        await _run_scroll(page, config.scroll)
    if config.click and config.click.enabled:
        await _run_clicks(page, config.click)
    if config.hover and config.hover.enabled:
        await _run_hovers(page, config.hover)

    search_texts: list[str] = []
    search_links: list[str] = []
    search_pdfs: list[str] = []
    if config.search and config.search.enabled:
        search_texts, search_links, search_pdfs = await _run_search(page, config.search)

    def _merge(a: list[str], b: list[str]) -> list[str]:
        """a then b, first occurrence wins — search hits before pagination's."""
        seen = set(a)
        return a + [x for x in b if not (x in seen or seen.add(x))]

    if config.paginate and config.paginate.enabled:
        page_texts, extra_links, extra_pdf_links = await _run_pagination(page, config.paginate)
        html = await page.content()
        return (html, "\n\n--- PAGE_BREAK ---\n\n".join(search_texts + page_texts),
                _merge(search_links, extra_links), _merge(search_pdfs, extra_pdf_links))

    html = await page.content()
    had = any([
        config.scroll and config.scroll.enabled,
        config.click and config.click.enabled,
        config.hover and config.hover.enabled,
        config.search and config.search.enabled,
    ])
    text = await page.inner_text("body") if had else html
    if search_texts:
        text = "\n\n--- PAGE_BREAK ---\n\n".join(search_texts + [text])
    return html, text, search_links, search_pdfs


async def _run_scroll(page, cfg) -> None:
    # Imported, not copied: this must stay the same expression the engine's own auto-scroll uses,
    # or the two paths disagree about what "scroll to the bottom" means on an app-shell layout.
    from .async_engine import _SCROLL_DEEPEST_JS
    if cfg.mode == "infinite":
        last = -1
        for _ in range(cfg.steps):
            new = await page.evaluate(_SCROLL_DEEPEST_JS)
            await page.wait_for_timeout(cfg.pause_ms)
            if not isinstance(new, (int, float)) or new == last:
                break
            last = new
    elif cfg.mode == "bottom":
        await page.evaluate(_SCROLL_DEEPEST_JS)
        await page.wait_for_timeout(cfg.pause_ms)
    else:  # viewport
        vp = page.viewport_size or {"width": 1920, "height": 1080}
        for _ in range(cfg.steps):
            await page.evaluate(f"window.scrollBy(0, {vp['height']})")
            await page.wait_for_timeout(cfg.pause_ms)
    if cfg.wait_network_idle:
        try:
            await page.wait_for_load_state("networkidle", timeout=cfg.network_idle_timeout_ms)
        except Exception:
            pass


async def _present(page, sel) -> bool:
    """Is this selector on the page at all? A query_selector returns in ~1ms; acting on a
    selector that is absent costs the FULL Playwright timeout before raising.

    Blind selector lists are the normal case, not the exception -- the EV-tender 'spa' recipe
    carries 7 generic selectors (a[href*='tender'], [role='tab'], .pagination a, ...), so a
    perfectly good page can match none of them. An empty or malformed selector is caught here
    too, rather than raising 'Unexpected token "" while parsing selector ""' from inside the
    click, where the surrounding except swallows it as an ordinary failure.
    """
    if not sel or not sel.strip():
        return False
    try:
        return await page.query_selector(sel) is not None
    except Exception:
        return False    # malformed selector: treat as absent, never as a full timeout


async def _run_clicks(page, cfg) -> None:
    for sel in cfg.selectors:
        if not await _present(page, sel):
            continue
        try:
            await page.click(sel, timeout=5000)
            await page.wait_for_timeout(cfg.pause_ms)
        except Exception as e:
            logger.warning("click failed '%s': %s", sel, e)
    if cfg.wait_network_idle:
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass


async def _run_hovers(page, cfg) -> None:
    for sel in cfg.selectors:
        if not await _present(page, sel):
            continue
        try:
            await page.hover(sel, timeout=5000)
            await page.wait_for_timeout(cfg.pause_ms)
        except Exception as e:
            logger.warning("hover failed '%s': %s", sel, e)
    for sel in getattr(cfg, "click_selectors", None) or []:
        if not await _present(page, sel):
            continue
        try:
            await page.click(sel, timeout=5000)
            await page.wait_for_timeout(cfg.pause_ms)
        except Exception as e:
            logger.warning("hover-click failed '%s': %s", sel, e)


async def _run_search(page, cfg) -> tuple[list[str], list[str], list[str]]:
    """Fill a public search box, submit (button or Enter), collect each result page's text."""
    results: list[str] = []
    links: list[str] = []
    pdf_links: list[str] = []
    seen_l: set[str] = set()
    seen_p: set[str] = set()
    # NO BOX ON THIS PAGE -> NO SEARCH, AND NO WAITING TO FIND OUT.
    #
    # page.fill takes no timeout argument, so it uses Playwright's 30s default. Without this
    # guard a page WITHOUT the box costs 30s PER KEYWORD, and the search config is applied to
    # every page the job walks — not just the seed. Measured 2026-08-18 on the GePNIC portals:
    # the home page has #SearchDescription and yields the e-bus tenders, while the nine nav
    # pages behind it (Tenders by Location, by Organisation, Downloads...) have no box at all,
    # so a 10-page crawl spent ~9 x 3 x 30s failing to search pages that were never searchable.
    # The visible symptom was the crawl getting BIGGER and the tender yield going to zero.
    #
    # An empty input_selector — the DEFAULT in models.py — is the other half: it raises
    # 'Unexpected token "" while parsing selector ""', which the except below logs as
    # "search failed", so a misconfigured job silently walks the home page and stores nothing.
    if not await _present(page, cfg.input_selector):
        logger.info("search skipped: no input for %r", (cfg.input_selector or "")[:60])
        return results, links, pdf_links
    for kw in cfg.keywords_to_search:
        try:
            await page.fill(cfg.input_selector, kw)
            await page.wait_for_timeout(300)
            if cfg.submit_selector:
                await page.click(cfg.submit_selector, timeout=5000)
            else:
                await page.press(cfg.input_selector, "Enter")   # no button → press Enter
            await page.wait_for_timeout(cfg.pause_ms)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            results.append(await page.inner_text("body"))
            # HARVEST HERE, PER KEYWORD -- not from the final DOM the caller reads.
            #
            # run_interactions() took its links from `await page.content()` AFTER this loop, so
            # only the LAST keyword's result page ever contributed links; every earlier keyword's
            # hits were typed, rendered, counted in the text, and then overwritten. recipe_for()
            # appends the English "electric bus" to every source's search_terms, which makes the
            # last keyword the one guaranteed to miss on a non-English portal -- measured on
            # service.bund.de, where Elektrobus and Elektrobusse each return live tenders and the
            # crawl still ended on "Keine Treffer" with zero result links.
            try:
                content = await page.content()
                for u in parse.extract_links(content, page.url):
                    if u not in seen_l:
                        seen_l.add(u)
                        links.append(u)
                for u in parse.extract_pdf_links(content, page.url):
                    if u not in seen_p:
                        seen_p.add(u)
                        pdf_links.append(u)
            except Exception:
                pass                   # harvesting is best-effort; the search itself worked
        except Exception as e:
            logger.warning("search failed '%s': %s", kw, e)
    return results, links, pdf_links


async def _run_pagination(page, cfg) -> tuple[list[str], list[str], list[str]]:
    """Click `next_selector` up to max_pages. Returns (per-page innerText, all <a href> page links,
    all PDF/attachment "Download" links) harvested across EVERY visited page — so both later-page
    navigation links AND their Download PDFs survive (extract_links drops PDFs; we collect them
    separately here, else pages 2..N-1 lose their attachments)."""
    body0 = await page.inner_text("body")
    texts: list[str] = [body0]
    links: list[str] = []
    pdf_links: list[str] = []
    seen_links: set[str] = set()
    seen_pdfs: set[str] = set()

    async def _harvest():
        try:
            content = await page.content()
        except Exception:
            return
        try:
            for u in parse.extract_links(content, page.url):
                if u not in seen_links:
                    seen_links.add(u)
                    links.append(u)
        except Exception:
            pass
        try:
            for u in parse.extract_pdf_links(content, page.url):   # Download/RFP links per page
                if u not in seen_pdfs:
                    seen_pdfs.add(u)
                    pdf_links.append(u)
        except Exception:
            pass

    await _harvest()
    prev_url, prev_len = page.url, len(body0)
    for _ in range(1, cfg.max_pages):
        try:
            await page.click(cfg.next_selector, timeout=5000)
            await page.wait_for_timeout(cfg.pause_ms)
            if cfg.wait_network_idle:
                try:
                    await page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
            new_url = page.url
            body = await page.inner_text("body")
            # Stop only when BOTH url and content are unchanged — catches __doPostBack/AJAX
            # pagination where the URL never changes but the results table does.
            if new_url == prev_url and len(body) == prev_len:
                logger.info("pagination stopped: url+content unchanged")
                break
            prev_url, prev_len = new_url, len(body)
            texts.append(body)
            await _harvest()
        except Exception as e:
            logger.info("pagination ended: %s", e)
            break
    return texts, links, pdf_links

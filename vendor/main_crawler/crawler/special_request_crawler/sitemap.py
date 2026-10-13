"""Sitemap expansion: start from the site's own published URL list, then let BFS find the rest.

Why this beats crawling from '/': BFS only reaches a page if something links to it, so orphaned and
deep pages are found last or never, and on a 15.5s-per-page origin "last" means days. The sitemap is
the site telling us every URL it considers real — seeding from it makes coverage complete by
construction, and link discovery from those pages still catches anything the sitemap omits.

Fetched over plain HTTPS, never C3: these are XML documents with no JS, so a browser render would
cost ~30s each for nothing. SIPRI's index alone is 57 sub-sitemaps.

The result is cached on disk because re-fetching 57 pages from a slow origin costs ~15 minutes, and
a crawl that restarts should not pay that again.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

log = logging.getLogger("special_request.sitemap")

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _get(url: str, timeout: float, retries: int = 3) -> str | None:
    """Fetch one sitemap XML. Retries because SIPRI's origin resets connections intermittently even
    on tiny XML — the very flakiness that makes sitemap-seeding worth it also breaks the seeding."""
    import httpx
    for attempt in range(retries):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as c:
                r = c.get(url, headers={"User-Agent": UA,
                                        "Accept": "application/xml,text/xml,*/*"})
            if r.status_code == 200 and r.text:
                return r.text
            log.info("sitemap %s -> %s (attempt %d)", url, r.status_code, attempt + 1)
        except Exception as exc:                      # noqa: BLE001 — a missing sitemap is not fatal
            log.info("sitemap fetch failed %s: %s (attempt %d)", url, exc, attempt + 1)
        time.sleep(2.0 * (attempt + 1))               # back off; the origin is already struggling

    # Direct HTTPS exhausted. SIPRI resets connections on plain fetches but serves the same URL
    # through a real browser, so fall back to C3 — the transport this whole crawler exists for.
    try:
        from crawler import camofox_client
        if camofox_client.enabled():
            snap = camofox_client.render(url, timeout_s=timeout)
            body = (snap or {}).get("html") or (snap or {}).get("text") or ""
            # A rendered XML page comes back wrapped in HTML; the <loc> regex still finds the URLs.
            if "<loc>" in body:
                log.info("sitemap %s recovered via C3", url)
                return body
    except Exception as exc:                           # noqa: BLE001
        log.info("sitemap C3 fallback failed %s: %s", url, exc)
    return None


def _is_sitemap(url: str) -> bool:
    """A <loc> that points at another sitemap rather than a page."""
    return "sitemap" in url.lower() and (url.lower().endswith(".xml") or "sitemap.xml?" in url.lower())


def fetch_urls(root: str, *, timeout: float = 60.0, delay: float = 2.0,
               max_children: int = 200, cache: str | Path | None = None,
               max_age_s: float = 86400.0) -> list[str]:
    """Every page URL the site publishes. Follows one level of sitemap-index nesting."""
    cache_path = Path(cache) if cache else None
    if cache_path and cache_path.exists():
        try:
            blob = json.loads(cache_path.read_text(encoding="utf-8"))
            if time.time() - blob.get("at", 0) < max_age_s and blob.get("root") == root:
                log.info("sitemap: %d urls from cache", len(blob["urls"]))
                return list(blob["urls"])
        except Exception:                             # noqa: BLE001 — a bad cache is just a cold start
            pass

    # The index is the single point of failure — if it doesn't load, nothing does — so it gets more
    # patience than a sub-sitemap. SIPRI resets connections on it intermittently even when the same
    # URL loaded minutes earlier.
    text = _get(root, timeout, retries=6)
    if not text:
        log.warning("sitemap index unreachable after retries: %s", root)
        return []
    first = _LOC.findall(text)
    children = [u for u in first if _is_sitemap(u)][:max_children]
    urls = {u for u in first if not _is_sitemap(u)}

    for i, child in enumerate(children, 1):
        time.sleep(delay)                             # their origin is slow; do not stampede it
        body = _get(child, timeout)
        if not body:
            continue
        found = [u for u in _LOC.findall(body) if not _is_sitemap(u)]
        urls.update(found)
        if i % 10 == 0 or i == len(children):
            log.info("sitemap: %d/%d sub-sitemaps, %d urls", i, len(children), len(urls))

    out = sorted(urls)
    if cache_path and out:                            # never cache an empty result — a bad-luck run
                                                      # would then serve "0 urls" from cache for a day
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"at": time.time(), "root": root, "urls": out}),
                                  encoding="utf-8")
        except Exception as exc:                      # noqa: BLE001
            log.info("sitemap cache write failed: %s", exc)
    log.info("sitemap: %d published urls (%d sub-sitemaps)", len(out), len(children))
    return out


def demo() -> None:
    """Offline self-check of the parsing and nesting rules."""
    idx = """<sitemapindex><sitemap><loc>https://x.org/sitemap.xml?page=1</loc></sitemap>
             <sitemap><loc>https://x.org/sitemap.xml?page=2</loc></sitemap></sitemapindex>"""
    pages = {"https://x.org/sitemap.xml?page=1":
             "<urlset><url><loc>https://x.org/a</loc></url><url><loc>https://x.org/b</loc></url></urlset>",
             "https://x.org/sitemap.xml?page=2":
             "<urlset><url><loc>https://x.org/c</loc></url></urlset>"}

    import special_request_crawler.sitemap as m
    real_get, real_sleep = m._get, m.time.sleep
    m._get = lambda u, t, retries=3: idx if u.endswith("/sitemap.xml") else pages.get(u)
    m.time.sleep = lambda _s: None
    try:
        got = m.fetch_urls("https://x.org/sitemap.xml", delay=0)  # call via module so the patch binds
        assert got == ["https://x.org/a", "https://x.org/b", "https://x.org/c"], got
        assert _is_sitemap("https://x.org/sitemap.xml?page=3")
        assert not _is_sitemap("https://x.org/news/article")
    finally:
        m._get, m.time.sleep = real_get, real_sleep
    print("all sitemap self-checks passed")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    demo()

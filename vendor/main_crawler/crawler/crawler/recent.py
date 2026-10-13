"""Cheap discovery of a news site's LAST-N-DAYS articles, for seeding a dated crawl.

WHY THIS EXISTS. `freshness_days` is a KEEP-GATE, not a finder: gate.py applies it after a page has
been fetched, parsed and dated, so a 90-day news crawl driven by it pays a full fetch (often a
render) for every article back to 2015 and then throws them away. Worse, `passes_freshness` returns
True when a page carries no detectable date -- deliberately, so it never fabricates a drop -- so as
a filter it is not even tight. Discovery has to happen BEFORE the crawl, and cheaply.

Sitemaps already carry the answer. A news site publishes <lastmod> per URL, and a <sitemapindex>
carries <lastmod> PER CHILD SITEMAP -- so a site with ten years of archives split by month can be
reduced to the three child maps worth opening, and the other 117 are never fetched. That is the
whole trick: a few HTTP requests per site instead of crawling the link graph and dating pages after
the fact.

This is deliberately a SEPARATE module from sitemap.py. That one's docstring states the operator
rule -- "use the sitemap to cross-check, don't depend on it for links" -- and it enforces that by
returning scheme-less normalized strings that cannot be used as seeds. That rule is about COVERAGE
(an exhausted frontier is not a covered site, and a sitemap must not be trusted to enumerate a
site). Seeding a DATED news sweep is a different job with a different failure mode: here a missing
URL costs one article, not a false claim of completeness. Both remain true; they are not the same
function, so they are not the same module.

    from crawler.recent import recent_urls
    urls = recent_urls("defensenews.com", days=90)      # -> full https:// URLs, newest first

Runnable:  python -m crawler.recent defensenews.com 90
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

_UA = "Mozilla/5.0 (compatible; MalloryRecent/1.0)"
_TIMEOUT = 20.0

# <url><loc>..</loc><lastmod>..</lastmod></url> — captured as PAIRS, because a bare findall of each
# tag separately would mis-align the moment one entry omits its lastmod, and silently date every
# later URL with its neighbour's timestamp.
_ENTRY = re.compile(
    r"<(?:url|sitemap)>(.*?)</(?:url|sitemap)>", re.I | re.S)
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
# news:publication_date first: a Google-News sitemap carries the true publish date, while <lastmod>
# on the same entry is often the CMS touch time and drifts.
_DATE = re.compile(
    r"<(?:news:publication_date|lastmod|published|updated)>\s*([^<\s]+)", re.I)
_SITEMAP_DECL = re.compile(r"(?im)^\s*sitemap:\s*(\S+)")

_CANDIDATES = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
               "/news-sitemap.xml", "/sitemap-news.xml", "/post-sitemap.xml",
               "/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml")


def _parse_date(s: str):
    """Sitemaps are XML-schema dates in theory and anything in practice. Return None rather than
    guessing: an unparseable date must not become 'now' and pull a 2011 archive into the window."""
    s = (s or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    for cut in (None, 19, 10):                      # full, to seconds, to the day
        try:
            d = datetime.fromisoformat(s if cut is None else s[:cut])
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y/%m/%d"):
        try:
            d = datetime.strptime(s, fmt)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except Exception:
            continue
    return None


def _get(client: httpx.Client, url: str) -> str:
    try:
        r = client.get(url, timeout=_TIMEOUT, headers={"User-Agent": _UA},
                       follow_redirects=True)
        # Some CDNs answer 200 with an HTML error page; a map without <loc> is not a map.
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


def _entries(xml: str) -> list[tuple[str, datetime | None]]:
    """[(url, date|None)] from a sitemap, sitemapindex, RSS or Atom body."""
    out: list[tuple[str, datetime | None]] = []
    for block in _ENTRY.findall(xml or ""):
        loc = _LOC.search(block)
        if not loc:
            continue
        d = _DATE.search(block)
        out.append((loc.group(1).strip(), _parse_date(d.group(1)) if d else None))
    if out:
        return out
    # RSS / Atom fallback: <item><link>..</link><pubDate>..</pubDate></item>
    for block in re.findall(r"<(?:item|entry)>(.*?)</(?:item|entry)>", xml or "", re.I | re.S):
        m = (re.search(r"<link[^>]*>\s*([^<\s]+)", block, re.I)
             or re.search(r"<link[^>]*href=[\"']([^\"']+)", block, re.I))
        if not m:
            continue
        d = re.search(r"<(?:pubDate|published|updated|dc:date)>\s*([^<]+)", block, re.I)
        out.append((m.group(1).strip(), _parse_date(d.group(1)) if d else None))
    return out


def _host_variants(host: str) -> list[str]:
    h = host[4:] if host.startswith("www.") else host
    return [h, "www." + h]


def recent_urls(host: str, days: int = 90, max_urls: int = 4000,
                max_children: int = 40) -> list[str]:
    """Full article URLs published/updated within *days*, newest first. [] if undiscoverable.

    Absent-safe by design: a site with no usable sitemap returns [], and the caller falls back to an
    ordinary crawl. Returning [] must never be read as "this site published nothing" -- it means
    "discovery did not work here", which is a different fact.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    reg = (host[4:] if host.startswith("www.") else host).lower()
    found: dict[str, datetime | None] = {}

    def same_site(u: str) -> bool:
        h = (urlsplit(u).hostname or "").lower()
        h = h[4:] if h.startswith("www.") else h
        return h == reg or h.endswith("." + reg)

    with httpx.Client() as client:
        seeds: list[str] = []
        for h in _host_variants(host):
            seeds += _SITEMAP_DECL.findall(_get(client, f"https://{h}/robots.txt"))
            seeds += [f"https://{h}{p}" for p in _CANDIDATES]

        seen_maps: set[str] = set()
        queue = list(dict.fromkeys(seeds))
        children_opened = 0

        while queue and len(found) < max_urls:
            sm = queue.pop(0)
            if sm in seen_maps:
                continue
            seen_maps.add(sm)
            xml = _get(client, sm)
            if not xml:
                continue
            is_index = "<sitemapindex" in xml.lower()
            for url, when in _entries(xml):
                if is_index:
                    # THE SAVING. A child sitemap's own lastmod dates its NEWEST entry, so a child
                    # older than the cutoff cannot contain anything in the window and is never
                    # fetched. This is what turns 120 monthly archive maps into three requests.
                    if when is not None and when < cutoff:
                        continue
                    if children_opened < max_children:
                        children_opened += 1
                        queue.append(url)
                    continue
                if not same_site(url):
                    continue
                if when is None or when >= cutoff:
                    # Undated entries are KEPT here and left for gate.py to judge after fetching --
                    # the same rule as passes_freshness, and for the same reason: never invent a
                    # drop. Dated ones are filtered now, which is where the saving is.
                    prev = found.get(url)
                    if prev is None or (when and prev and when > prev):
                        found[url] = when
                    else:
                        found.setdefault(url, when)

    dated = [(u, d) for u, d in found.items() if d is not None]
    undated = [u for u, d in found.items() if d is None]
    dated.sort(key=lambda t: t[1], reverse=True)
    return [u for u, _ in dated][:max_urls] + undated[:max(0, max_urls - len(dated))]


def _demo() -> None:
    """Self-check on the parsing, which is where this can silently go wrong. No network."""
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    new = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S+00:00")

    xml = f"""<urlset>
      <url><loc>https://x.example/new</loc><lastmod>{new}</lastmod></url>
      <url><loc>https://x.example/old</loc><lastmod>{old}</lastmod></url>
      <url><loc>https://x.example/undated</loc></url>
    </urlset>"""
    ents = _entries(xml)
    assert len(ents) == 3, ents
    # THE ALIGNMENT BUG this guards: the undated entry must carry None, not its neighbour's date.
    assert dict((u, d is None) for u, d in ents)["https://x.example/undated"] is True

    assert _parse_date("2026-08-21") is not None
    assert _parse_date("2026-08-21T10:00:00Z") is not None
    assert _parse_date("Mon, 21 Aug 2026 10:00:00 +0000") is not None
    assert _parse_date("garbage") is None, "an unparseable date must be None, never now()"
    assert _parse_date("") is None

    rss = """<rss><channel>
      <item><link>https://x.example/a</link><pubDate>Mon, 21 Aug 2026 10:00:00 +0000</pubDate></item>
    </channel></rss>"""
    assert _entries(rss)[0][0] == "https://x.example/a"

    idx = f"<sitemapindex><sitemap><loc>https://x.example/s1.xml</loc><lastmod>{old}</lastmod></sitemap></sitemapindex>"
    assert "<sitemapindex" in idx.lower() and _entries(idx)[0][1] < now - timedelta(days=100)
    print("ok")


if __name__ == "__main__":
    import sys
    if "--demo" in sys.argv or len(sys.argv) == 1:
        _demo()
    else:
        host = sys.argv[1]
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
        us = recent_urls(host, days=days)
        print(f"{host}: {len(us)} URL(s) in the last {days} days")
        for u in us[:15]:
            print("  ", u)

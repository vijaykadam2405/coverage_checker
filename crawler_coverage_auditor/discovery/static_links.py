from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


@dataclass(frozen=True, slots=True)
class Link:
    url: str
    anchor_text: str
    tag: str


def published_url(raw: str, base: str) -> str | None:
    """One malformed link must not abort extraction of every other published URL."""
    try:
        target = urljoin(base, raw.strip())
        parts = urlsplit(target)
        _ = parts.port  # Validate malformed ports as well as IPv6 literals.
        return target if parts.scheme.lower() in {'http', 'https'} and parts.hostname else None
    except (ValueError, UnicodeError, AttributeError):
        return None


def extract_links(html: str, page_url: str) -> list[Link]:
    """Extract navigational links from a static response without site-specific rules."""
    soup = BeautifulSoup(html, "lxml")
    base = soup.select_one('base[href]')
    if base:
        page_url = published_url(base['href'], page_url) or page_url
    links: list[Link] = []
    def add(raw, text, kind):
        target = published_url(raw, page_url)
        if target:
            links.append(Link(target, text, kind))
    attrs = {"a": "href", "iframe": "src", "img": "src", "source": "src", "video": "src", 'audio': 'src', 'object': 'data', 'embed': 'src'}
    for tag_name, attr in attrs.items():
        for element in soup.find_all(tag_name):
            raw = (element.get(attr) or '').strip()
            if not raw or (raw.startswith("#") and not raw.startswith(("#/", "#!/"))) or raw.startswith(("mailto:", "tel:", "javascript:", "data:")):
                continue
            add(raw, element.get_text(" ", strip=True), 'download' if element.has_attr('download') else tag_name)
    for element in soup.select("link[rel=canonical], link[rel=alternate], link[rel=next], link[rel=prev], a[download], [data-href], [data-url], [onclick]"):
        raw = element.get("data-href") or element.get("data-url") or element.get("href")
        if not raw and element.get("onclick"):
            import re
            match = re.search(r"(?:location(?:\.href)?\s*=|(?:window\.)?open\s*\()\s*['\"]([^'\"]+)", element["onclick"])
            raw = match.group(1) if match else None
        raw = raw.strip() if raw else raw
        if raw and not raw.startswith(("javascript:", "data:", "mailto:", "tel:")):
            add(raw, element.get_text(" ", strip=True), 'download' if element.has_attr('download') else element.name)
    for element in soup.select("img[srcset], source[srcset]"):
        for candidate in element["srcset"].split(","):
            if candidate.strip():
                add(candidate.strip().split()[0], "", "img")
    # Common lazy attributes are published URL evidence, not files to download.
    for element in soup.select('img[data-src], img[data-original], img[data-lazy-src], source[data-src], img[data-srcset], source[data-srcset], video[poster]'):
        for attr in ('data-src', 'data-original', 'data-lazy-src', 'poster'):
            raw = (element.get(attr) or '').strip()
            if raw and not raw.lower().startswith(('data:', 'javascript:', 'blob:')):
                add(raw, '', 'img' if element.name == 'img' or attr == 'poster' or element.find_parent('picture') else 'source')
        for candidate in (element.get('data-srcset') or '').split(','):
            raw = candidate.strip().split()[0] if candidate.strip() else ''
            if raw and not raw.lower().startswith(('data:', 'javascript:', 'blob:')):
                add(raw, '', 'img')
    for form in soup.select("form[action]"):
        if form.get("method", "get").lower() == "get":
            add(form['action'], "Search/filter form", "form")
    from crawler_coverage_auditor.integrations.main_crawler import html_links
    known = {link.url for link in links}
    for target, label in html_links(html, page_url):
        if target not in known:
            add(target, label, 'a')
            known.add(target)
    return links


def count_listing_items(html: str) -> int | None:
    """Conservative signal for page validity; None means no clear listing structure."""
    soup = BeautifulSoup(html, "lxml")
    candidates = soup.select("article, .result, .search-result, .card, li[class*='item']")
    return len(candidates) if candidates else None

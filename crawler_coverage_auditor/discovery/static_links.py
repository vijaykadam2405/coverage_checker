from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup


@dataclass(frozen=True, slots=True)
class Link:
    url: str
    anchor_text: str
    tag: str


def extract_links(html: str, page_url: str) -> list[Link]:
    """Extract navigational links from a static response without site-specific rules."""
    soup = BeautifulSoup(html, "lxml")
    base = soup.select_one('base[href]')
    if base:
        page_url = urljoin(page_url, base['href'].strip())
    links: list[Link] = []
    attrs = {"a": "href", "iframe": "src", "img": "src", "source": "src", "video": "src", 'audio': 'src', 'object': 'data', 'embed': 'src'}
    for tag_name, attr in attrs.items():
        for element in soup.find_all(tag_name):
            raw = (element.get(attr) or '').strip()
            if not raw or (raw.startswith("#") and not raw.startswith(("#/", "#!/"))) or raw.startswith(("mailto:", "tel:", "javascript:", "data:")):
                continue
            links.append(Link(urljoin(page_url, raw), element.get_text(" ", strip=True), 'download' if element.has_attr('download') else tag_name))
    for element in soup.select("link[rel=canonical], link[rel=alternate], link[rel=next], link[rel=prev], a[download], [data-href], [data-url], [onclick]"):
        raw = element.get("data-href") or element.get("data-url") or element.get("href")
        if not raw and element.get("onclick"):
            import re
            match = re.search(r"(?:location(?:\.href)?\s*=|(?:window\.)?open\s*\()\s*['\"]([^'\"]+)", element["onclick"])
            raw = match.group(1) if match else None
        raw = raw.strip() if raw else raw
        if raw and not raw.startswith(("javascript:", "data:", "mailto:", "tel:")):
            links.append(Link(urljoin(page_url, raw), element.get_text(" ", strip=True), 'download' if element.has_attr('download') else element.name))
    for element in soup.select("img[srcset], source[srcset]"):
        for candidate in element["srcset"].split(","):
            if candidate.strip():
                links.append(Link(urljoin(page_url, candidate.strip().split()[0]), "", "img"))
    for form in soup.select("form[action]"):
        if form.get("method", "get").lower() == "get":
            links.append(Link(urljoin(page_url, form["action"].strip()), "Search/filter form", "form"))
    return links


def count_listing_items(html: str) -> int | None:
    """Conservative signal for page validity; None means no clear listing structure."""
    soup = BeautifulSoup(html, "lxml")
    candidates = soup.select("article, .result, .search-result, .card, li[class*='item']")
    return len(candidates) if candidates else None

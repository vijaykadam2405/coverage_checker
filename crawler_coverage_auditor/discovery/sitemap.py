from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

from crawler_coverage_auditor.validation.http import PoliteHttpClient


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    location: str
    lastmod: str | None = None


def sitemap_candidates(seed_url: str) -> list[str]:
    return [urljoin(seed_url, "/" + suffix) for suffix in ("robots.txt", "sitemap.xml", "sitemap_index.xml", "sitemap-index.xml")]


def robots_sitemaps(text: str, base_url: str) -> list[str]:
    return [urljoin(base_url, item.strip()) for item in re.findall(r"^\s*Sitemap\s*:\s*(\S+)", text, flags=re.I | re.M)]


def parse_sitemap(content: bytes) -> tuple[str, list[SitemapEntry]]:
    root = ET.fromstring(content)
    name = root.tag.rsplit("}", 1)[-1].lower()
    entries: list[SitemapEntry] = []
    for item in root:
        values = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in item}
        if values.get("loc"):
            entries.append(SitemapEntry(values["loc"], values.get("lastmod")))
    return name, entries


async def discover_sitemaps(seed_url: str, client: PoliteHttpClient, *, on_sitemap, on_url, on_error=None, stopped=None) -> None:
    """Follow sitemap indexes recursively; no one-level or first-sitemap shortcut."""
    checked: set[str] = set()
    queue: list[tuple[str, str | None]] = []
    robots = await client.get(urljoin(seed_url, "/robots.txt"), discovery_metadata=True)
    if robots.status_code == 200:
        queue.extend((url, None) for url in robots_sitemaps(robots.text, seed_url))
    queue.extend((urljoin(seed_url, "/" + path), None) for path in ("sitemap.xml", "sitemap_index.xml", "sitemap-index.xml"))
    while queue:
        if stopped and stopped():
            return
        if len(checked) >= 1000:
            if on_error:
                on_error(seed_url, "Sitemap safeguard (1000 files) reached; remaining indexes may contain more content")
            return
        sitemap_url, parent = queue.pop(0)
        if sitemap_url in checked:
            continue
        checked.add(sitemap_url)
        response = await client.get(sitemap_url, discovery_metadata=True)
        if response.status_code != 200 or not response.content:
            if on_error and (parent or response.error or response.status_code in {401, 403, 429} or response.status_code >= 500):
                on_error(sitemap_url, f"Sitemap unavailable: HTTP {response.status_code}; {response.error or ''}")
            continue
        try:
            import gzip
            content = gzip.decompress(response.content) if response.content.startswith(b"\x1f\x8b") else response.content
            kind, entries = parse_sitemap(content)
        except (ET.ParseError, OSError, EOFError):
            if on_error and parent:
                on_error(sitemap_url, "Declared child sitemap could not be parsed")
            continue
        on_sitemap(sitemap_url, parent, kind, len(entries))
        if kind == "sitemapindex":
            queue.extend((entry.location, sitemap_url) for entry in entries)
        elif kind == "urlset":
            for entry in entries:
                on_url(entry.location, sitemap_url, entry.lastmod)

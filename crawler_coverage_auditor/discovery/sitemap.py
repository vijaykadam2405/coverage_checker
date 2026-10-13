from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from urllib.parse import urljoin
from xml.etree import ElementTree as ET
from hashlib import sha256

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


async def discover_sitemaps(seed_url: str, client: PoliteHttpClient, *, on_sitemap, on_url, on_error=None, stopped=None, progress=None) -> None:
    """Follow sitemap indexes recursively; no one-level or first-sitemap shortcut."""
    checked: set[str] = set()
    queue: deque[tuple[str, str | None]] = deque()
    if progress and progress.state()['sitemap_completed']:
        return
    if not progress or not progress.state()['sitemap_initialized']:
        robots = await client.get(urljoin(seed_url, "/robots.txt"), discovery_metadata=True)
        if stopped and stopped():
            return
        if robots.status_code == 200:
            queue.extend((url, None) for url in robots_sitemaps(robots.text, seed_url))
        queue.extend((urljoin(seed_url, "/" + path), None) for path in ("sitemap.xml", "sitemap_index.xml", "sitemap-index.xml"))
        if progress:
            progress.initialize_sitemaps(url for url, parent in queue)
            queue.clear()
    while queue or progress and progress.pending_sitemap():
        if stopped and stopped():
            return
        if progress:
            task = progress.pending_sitemap()
            sitemap_url, parent = task['url'], task['parent_url']
            progress.enter(sitemap_url, 'SITEMAP')
        else:
            sitemap_url, parent = queue.popleft()
        if sitemap_url in checked:
            continue
        checked.add(sitemap_url)
        response = await client.get(sitemap_url, discovery_metadata=True)
        if stopped and stopped():
            return
        if response.status_code != 200 or not response.content:
            if on_error and (parent or response.error or response.status_code in {401, 403, 429} or response.status_code >= 500):
                on_error(sitemap_url, f"Sitemap unavailable: HTTP {response.status_code}; {response.error or ''}")
            if progress:
                progress.finish_sitemap(sitemap_url)
            continue
        try:
            import gzip
            cap = getattr(client, 'max_response_bytes', 8 * 1024 * 1024)
            if response.content.startswith(b"\x1f\x8b"):
                from io import BytesIO
                with gzip.GzipFile(fileobj=BytesIO(response.content)) as compressed:
                    content = compressed.read(cap + 1)
            else:
                content = response.content
            if len(content) > cap:
                if on_error:
                    on_error(sitemap_url, f'Sitemap exceeds decoded response limit ({cap} bytes); inventory may be incomplete')
                if progress:
                    progress.finish_sitemap(sitemap_url)
                continue
            kind, entries = parse_sitemap(content)
        except (ET.ParseError, OSError, EOFError):
            if on_error and parent:
                on_error(sitemap_url, "Declared child sitemap could not be parsed")
            if progress:
                progress.finish_sitemap(sitemap_url)
            continue
        on_sitemap(sitemap_url, parent, kind, len(entries))
        if progress:
            body_hash = sha256(content).hexdigest()
            offset = progress.sitemap_position(sitemap_url, body_hash)
            with progress.db.discovery_batch():
                for index, entry in enumerate(entries[offset:], offset):
                    if stopped and stopped():
                        return
                    if kind == 'sitemapindex':
                        progress.enqueue_sitemap(entry.location, sitemap_url)
                    elif kind == 'urlset':
                        on_url(entry.location, sitemap_url, entry.lastmod)
                    progress.sitemap_entry(sitemap_url, body_hash, index + 1)
            if stopped and stopped():
                return
            progress.finish_sitemap(sitemap_url)
        elif kind == "sitemapindex":
            queue.extend((entry.location, sitemap_url) for entry in entries)
        elif kind == "urlset":
            for entry in entries:
                if stopped and stopped():
                    return
                on_url(entry.location, sitemap_url, entry.lastmod)
    if progress:
        progress.finish_sitemaps()

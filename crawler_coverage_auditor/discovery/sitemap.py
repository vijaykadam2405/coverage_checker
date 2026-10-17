from __future__ import annotations

import gzip
import re
from collections import deque
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from tempfile import SpooledTemporaryFile
from urllib.parse import urljoin

from lxml import etree as ET

from crawler_coverage_auditor.validation.http import PoliteHttpClient

MAX_SITEMAP_BYTES = 50 * 1024 * 1024  # Protocol file size, not a total URL ceiling.


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    location: str
    lastmod: str | None = None


def sitemap_candidates(seed_url):
    return [
        urljoin(seed_url, "/" + suffix)
        for suffix in (
            "robots.txt",
            "sitemap.xml",
            "sitemap_index.xml",
            "sitemap-index.xml",
        )
    ]


def robots_sitemaps(text, base_url):
    return [
        urljoin(base_url, item.strip())
        for item in re.findall(r"^\s*Sitemap\s*:\s*(\S+)", text, re.I | re.M)
    ]


def _xml_entries(stream):
    root = None
    for event, element in ET.iterparse(
        stream,
        events=("start", "end"),
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
    ):
        if root is None:
            root = element
        if event != "end":
            continue
        name = ET.QName(element).localname
        if name not in {"url", "sitemap", "item", "entry"}:
            continue
        loc = next(
            (
                child.text
                for child in element
                if isinstance(child.tag, str) and ET.QName(child).localname == "loc"
            ),
            None,
        )
        lastmod = next(
            (
                child.text
                for child in element
                if isinstance(child.tag, str)
                and ET.QName(child).localname in {"lastmod", "updated", "pubDate"}
            ),
            None,
        )
        if name in {"item", "entry"}:
            link = next(
                (child for child in element if ET.QName(child).localname == "link"),
                None,
            )
            loc = (link.get("href") or link.text) if link is not None else None
        if loc and loc.strip():
            yield SitemapEntry(loc.strip(), lastmod)
        for child in element.iterdescendants():
            if not isinstance(child.tag, str):
                continue
            local, namespace = (
                ET.QName(child).localname,
                ET.QName(child).namespace or "",
            )
            if (
                ("image" in namespace or "video" in namespace)
                and local in {"loc", "content_loc", "thumbnail_loc"}
                and child.text
            ):
                yield SitemapEntry(child.text.strip())
        element.clear()
        while element.getprevious() is not None:
            del element.getparent()[0]


def _kind(stream):
    stream.seek(0)
    for _, root in ET.iterparse(
        stream,
        events=("start",),
        resolve_entities=False,
        load_dtd=False,
        no_network=True,
    ):
        if root.getroottree().docinfo.doctype:
            raise ValueError("Sitemap DTD/entities are not permitted")
        name = ET.QName(root).localname.lower()
        stream.seek(0)
        if name not in {"sitemapindex", "urlset", "rss", "feed"}:
            raise ValueError("Unrecognized sitemap format")
        return name
    raise ValueError("Empty sitemap")


def _entries(stream, kind):
    stream.seek(0)
    if kind == "text":
        for line in stream:
            raw = line.decode("utf-8-sig").strip()
            if raw.startswith(("https://", "http://")):
                yield SitemapEntry(raw)
    else:
        yield from _xml_entries(stream)


def parse_sitemap(content):
    stream = BytesIO(content)
    return _kind(stream), list(_xml_entries(stream))


@asynccontextmanager
async def _download(client, url):
    if hasattr(client, "metadata_stream"):
        async with client.metadata_stream(url) as value:
            yield value
    else:
        response = await client.get(url, discovery_metadata=True)
        with BytesIO(response.content) as stream:
            yield response, stream


async def discover_sitemaps(
    seed_url: str,
    client: PoliteHttpClient,
    *,
    on_sitemap,
    on_url,
    on_error=None,
    stopped=None,
    progress=None,
):
    checked, queue = set(), deque()
    if progress and progress.state()["sitemap_completed"]:
        return
    if not progress or not progress.state()["sitemap_initialized"]:
        robots = await client.get(
            urljoin(seed_url, "/robots.txt"), discovery_metadata=True
        )
        if stopped and stopped():
            return
        if robots.status_code == 200:
            queue.extend((url, None) for url in robots_sitemaps(robots.text, seed_url))
        queue.extend(
            (urljoin(seed_url, "/" + path), None)
            for path in ("sitemap.xml", "sitemap_index.xml", "sitemap-index.xml")
        )
        if progress:
            progress.initialize_sitemaps(url for url, _ in queue)
            queue.clear()
    while queue or progress and progress.pending_sitemap():
        if stopped and stopped():
            return
        task = progress.pending_sitemap() if progress else None
        sitemap_url, parent = (
            (task["url"], task["parent_url"]) if task else queue.popleft()
        )
        if sitemap_url in checked:
            continue
        checked.add(sitemap_url)
        if progress:
            progress.enter(sitemap_url, "SITEMAP")
        try:
            async with _download(client, sitemap_url) as (response, source):
                if stopped and stopped():
                    return
                if response.status_code != 200 or response.error or response.url_only:
                    if (
                        parent
                        or response.error
                        or response.status_code in {401, 403, 429}
                        or response.status_code >= 500
                    ):
                        raise ValueError(
                            f"Sitemap unavailable: HTTP {response.status_code}; {response.error or ''}"
                        )
                    if progress:
                        progress.finish_sitemap(sitemap_url)
                    continue
                compressed = source.read(2) == b"\x1f\x8b"
                source.seek(0)
                decoded = gzip.GzipFile(fileobj=source) if compressed else source
                with SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as content:
                    digest, length, scan_tail = sha256(), 0, b""
                    while chunk := decoded.read(64 * 1024):
                        if stopped and stopped():
                            return
                        length += len(chunk)
                        if length > MAX_SITEMAP_BYTES:
                            raise ValueError(
                                f"Sitemap exceeds decoded response limit ({MAX_SITEMAP_BYTES} bytes); enumeration incomplete"
                            )
                        scan = scan_tail + chunk.upper()
                        if b"<!DOCTYPE" in scan or b"<!ENTITY" in scan:
                            raise ValueError("Sitemap DTD/entities are not permitted")
                        scan_tail = scan[-16:]
                        digest.update(chunk)
                        content.write(chunk)
                    if compressed:
                        decoded.close()
                    content.seek(0)
                    first = content.read(256).lstrip()
                    kind = (
                        "text"
                        if first.startswith((b"https://", b"http://"))
                        else _kind(content)
                    )
                    count = 0
                    for _ in _entries(content, kind):
                        if stopped and stopped():
                            return
                        count += 1
                    on_sitemap(sitemap_url, parent, kind, count)
                    body_hash = digest.hexdigest()
                    offset = (
                        progress.sitemap_position(sitemap_url, body_hash)
                        if progress
                        else 0
                    )
                    batch = progress.db.discovery_batch() if progress else nullcontext()
                    with batch:
                        for index, entry in enumerate(_entries(content, kind)):
                            if index < offset:
                                continue
                            if stopped and stopped():
                                return
                            if kind == "sitemapindex":
                                if progress:
                                    progress.enqueue_sitemap(
                                        entry.location, sitemap_url
                                    )
                                else:
                                    queue.append((entry.location, sitemap_url))
                            else:
                                on_url(entry.location, sitemap_url, entry.lastmod)
                            if progress:
                                progress.sitemap_entry(
                                    sitemap_url, body_hash, index + 1
                                )
                    if kind == "feed":
                        content.seek(0)
                        for _, element in ET.iterparse(
                            content,
                            events=("end",),
                            resolve_entities=False,
                            no_network=True,
                        ):
                            if (
                                ET.QName(element).localname == "link"
                                and element.get("rel") == "next"
                                and element.get("href")
                            ):
                                next_url = urljoin(sitemap_url, element.get("href"))
                                if progress:
                                    progress.enqueue_sitemap(next_url, sitemap_url)
                                else:
                                    queue.append((next_url, sitemap_url))
                            element.clear()
        except (
            ET.XMLSyntaxError,
            OSError,
            EOFError,
            ValueError,
            UnicodeError,
        ) as error:
            if on_error:
                on_error(sitemap_url, f"Sitemap enumeration incomplete: {error}")
        if progress:
            progress.finish_sitemap(sitemap_url)
    if progress:
        progress.finish_sitemaps()

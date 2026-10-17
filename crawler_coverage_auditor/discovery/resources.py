"""URL-only resource typing and published API link discovery."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from crawler_coverage_auditor.discovery.api_pagination import continuations
from crawler_coverage_auditor.discovery.page import parsed_json, snapshot_value
from crawler_coverage_auditor.discovery.static_links import Link, published_url

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".avif", ".bmp", ".tif", ".tiff"}
FILE_EXTENSIONS = {".pdf", ".doc", ".docx", '.docm', ".xls", ".xlsx", '.xlsm', '.xlsb', ".ppt", ".pptx", '.pptm', ".csv", ".txt", ".rtf", ".odt", ".ods", ".odp", ".zip", ".xml", ".epub", ".7z", ".rar", ".gz", ".tar"}
RESOURCE_EXTENSIONS = {".mp4", ".webm", ".mp3", ".wav", ".ogg", ".mov", ".avi", ".exe", ".iso", ".css", ".js", ".woff", ".woff2", ".ttf"}
URL_ONLY_TYPES = {"DOCUMENT", "IMAGE", "RESOURCE"}


def resource_type(url: str, content_type: str = "") -> str:
    from pathlib import PurePosixPath
    content_type = content_type.lower()
    extension = PurePosixPath(urlsplit(url).path).suffix.lower()
    mime = content_type.split(';', 1)[0].strip()
    # Reliable response headers take priority over a misleading filename.
    if mime in {'text/html', 'application/xhtml+xml'}:
        return 'HTML'
    if mime.startswith('image/'):
        return 'IMAGE'
    if mime.startswith(('audio/', 'video/', 'font/')) or mime in {'text/css', 'text/javascript', 'application/javascript'}:
        return 'RESOURCE'
    if 'json' in mime:
        return 'API'
    if any(marker in mime for marker in ('rss+xml', 'atom+xml')):
        return 'FEED'
    if mime in {'text/plain', 'text/csv', 'text/rtf', 'application/rtf', 'application/zip', 'application/x-7z-compressed', 'application/octet-stream'} or any(marker in mime for marker in ('pdf', 'officedocument', 'msword', 'opendocument', 'epub', 'ms-excel', 'ms-powerpoint', 'spreadsheet', 'vnd.ms-word')):
        return 'DOCUMENT'
    if extension == '.xml' and PurePosixPath(urlsplit(url).path).stem.lower() in {'feed', 'rss', 'atom'}:
        return 'FEED'
    if extension in RESOURCE_EXTENSIONS:
        return 'RESOURCE'
    if extension in IMAGE_EXTENSIONS:
        return "IMAGE"
    if extension in FILE_EXTENSIONS:
        return "DOCUMENT"
    if extension == '.json':
        return "API"
    if extension in {'.rss', '.atom'}:
        return 'FEED'
    return "HTML"


def link_resource_hint(link: Link) -> str | None:
    return {'img': 'IMAGE', 'download': 'DOCUMENT', 'object': 'DOCUMENT', 'embed': 'DOCUMENT',
            'video': 'RESOURCE', 'audio': 'RESOURCE', 'source': 'RESOURCE'}.get(link.tag)


def json_links(content: str, base_url: str) -> list[Link]:
    """Follow URLs actually supplied by APIs, including next/cursor links."""
    return list(snapshot_value(('json_links', content, base_url), lambda: tuple(_json_links(content, base_url))))


def _json_links(content: str, base_url: str) -> list[Link]:
    try:
        payload = parsed_json(content)
    except ValueError:
        return []
    result: list[Link] = []
    def visit(value, key=""):
        if isinstance(value, dict):
            for name, item in value.items():
                visit(item, str(name).lower())
        elif isinstance(value, list):
            for item in value:
                visit(item, key)
        elif isinstance(value, str) and (value.startswith(("http://", "https://", "/")) or key in {"url", "href", "link", "next", "next_url", "nextpageurl", "next_page_url", "next_page", "canonical", "download"}):
            if not value.startswith(("javascript:", "data:")) and ("/" in value or "?" in value):
                target = published_url(value, base_url)
                if target and target != base_url:
                    result.append(Link(target, key, "api"))
    visit(payload)
    if isinstance(payload, dict):
        for url in continuations(payload, base_url):
            result.append(Link(url, 'API response-backed pagination', 'api'))
    return result


def trap_reason(url: str) -> str | None:
    """Reject structural repetition; never reject merely because a path is deep."""
    segments = [item for item in urlsplit(url).path.split("/") if item]
    for size in range(1, min(6, len(segments) // 3 + 1)):
        if any(segments[index:index + size] * 3 == segments[index:index + size * 3] for index in range(len(segments) - size * 3 + 1)):
            return "Repeated path segments indicate a potential crawler trap"
    if re.search(r"[?&](?:sessionid|phpsessid|jsessionid|sid)=", url, re.I):
        return "Session-specific URL excluded to avoid infinite duplicate sessions"
    return None

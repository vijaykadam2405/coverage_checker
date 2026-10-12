from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup


PAGE_PARAMETERS = {"page", "paged", "p", "pg", "pagina", "pagenumber", "page_number", "offset", "start"}
NUMERIC_PAGE_PARAMETERS = PAGE_PARAMETERS - {"offset", "start"}


@dataclass(frozen=True, slots=True)
class PaginationHint:
    kind: str
    value: int | None
    url: str | None


def detect_pagination(html: str, page_url: str) -> list[PaginationHint]:
    soup = BeautifulSoup(html, "lxml")
    hints: list[PaginationHint] = []
    for link in soup.select("a[href], link[rel=next][href], link[rel=last][href]"):
        rel = " ".join(link.get("rel", [])).lower()
        text = link.get_text(" ", strip=True).lower()
        href = urljoin(page_url, link["href"])
        if rel == "next" or text in {"next", "older", "more", ">", "»", ">>"}:
            hints.append(PaginationHint("NEXT", None, href))
        for name, value in parse_qsl(urlsplit(href).query):
            if name.lower() in PAGE_PARAMETERS and value.isdigit():
                hints.append(PaginationHint("QUERY", int(value), href))
        match = re.search(r"/page/(\d+)/?$", urlsplit(href).path)
        if match:
            hints.append(PaginationHint("PATH", int(match.group(1)), href))
    return hints


def numeric_page_parameter(url: str) -> tuple[str, int] | None:
    """Return a page-number parameter, excluding offset/start batch pagination."""
    for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if name.lower() in NUMERIC_PAGE_PARAMETERS and value.isdigit():
            return name, int(value)
    match = re.search(r"/page/(\d+)/?$", urlsplit(url).path)
    if match:
        return "__path__", int(match.group(1))
    return None


def pagination_series_key(url: str, parameter: str) -> str:
    """Stable key for one paginator while preserving its meaningful filters."""
    return replace_page_parameter(url, parameter, 0)


def replace_page_parameter(url: str, parameter: str, page: int) -> str:
    parts = urlsplit(url)
    if parameter == "__path__":
        return urlunsplit((parts.scheme, parts.netloc, re.sub(r"(/page/)\d+(/?)$", lambda match: f"{match.group(1)}{page}{match.group(2)}", parts.path), parts.query, ""))
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    replaced = [(key, str(page) if key.lower() == parameter.lower() else value) for key, value in pairs]
    if not any(key.lower() == parameter.lower() for key, _ in pairs):
        replaced.append((parameter, str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(replaced), ""))

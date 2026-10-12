from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

TRACKING_PARAMETERS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "_ga", "_gl", "ref", "referrer",
}
SEMANTIC_PARAMETER_HINTS = {
    "page", "paged", "p", "pg", "pagina", "pagenumber", "offset", "start",
    "limit", "cursor", "cursormark", "year", "month", "day", "category",
    "country", "type", "id", "q", "query", "search", "from", "to",
}


def is_tracking_parameter(name: str) -> bool:
    lower = name.lower()
    return lower in TRACKING_PARAMETERS or lower.startswith("utm_")


def normalize_url(url: str, base_url: str | None = None) -> str:
    """Canonicalise transport-level URL differences without dropping semantic queries."""
    # HTML hrefs often contain indentation/trailing whitespace. Browser URL
    # parsing trims it; requesting a literal trailing space instead creates %20
    # duplicates/404s. Preserve encoded %20 and meaningful inner characters.
    url = url.strip()
    absolute = urljoin(base_url.strip(), url) if base_url else url
    parts = urlsplit(absolute)
    scheme = parts.scheme.lower()
    hostname = (parts.hostname or "").lower()
    port = parts.port
    netloc = f'[{hostname}]' if ':' in hostname else hostname
    if port and not ((scheme == "https" and port == 443) or (scheme == "http" and port == 80)):
        netloc = f"{netloc}:{port}"
    path = parts.path or "/"
    while "//" in path:
        path = path.replace("//", "/")
    pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not is_tracking_parameter(key)]
    query = urlencode(sorted(pairs), doseq=True)
    fragment = parts.fragment if parts.fragment.startswith(("/", "!/")) else ""
    return urlunsplit((scheme, netloc, path, query, fragment))


def same_scope(url: str, allowed_hosts: Iterable[str], include_subdomains: bool = False) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    allowed = {item.lower() for item in allowed_hosts}
    if host in allowed:
        return True
    return include_subdomains and any(host.endswith("." + candidate) for candidate in allowed)


def classify_parameter(name: str) -> str:
    lower = name.lower()
    if is_tracking_parameter(lower):
        return "TRACKING"
    if lower in {"sid", "session", "sessionid", "phpsessid", "jsessionid"}:
        return "SESSION"
    if lower in {"page", "paged", "p", "pg", "pagina", "pagenumber", "offset", "start", "limit", "cursor", "cursormark"}:
        return "PAGINATION"
    if lower in {"year", "month", "day", "category", "country", "type", "from", "to"}:
        return "FILTER"
    if lower in {"id", "articleid", "productid"}:
        return "CONTENT_ID"
    if lower in {"sort", "order", "orderby"}:
        return "SORT"
    return "UNKNOWN"

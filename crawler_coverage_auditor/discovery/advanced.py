"""Published discovery evidence; patterns and speculation are not content URLs."""

import re
from urllib.parse import parse_qsl, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment

from crawler_coverage_auditor.validation.resource_policy import excluded_resource_url

from .evidence import header_relations
from .page import parsed_html, text_without
from .resources import json_links
from .static_links import Link, document_base, published_url

SENSITIVE = re.compile(
    r"^(?:token|access_token|api_?key|authorization|password|secret|signature|sessionid)$",
    re.I,
)
ACTION = re.compile(
    r"/(?:admin|private|internal|logout|signout|delete|checkout|register)(?:/|$)", re.I
)
FRAMEWORK_IDS = {
    "__NEXT_DATA__",
    "__NUXT_DATA__",
    "__INITIAL_STATE__",
    "__PRELOADED_STATE__",
}


def public_candidate(raw, base):
    target = published_url(raw, base)
    if not target or excluded_resource_url(target):
        return None
    parts = urlsplit(target)
    if (
        parts.username
        or parts.password
        or ACTION.search(parts.path)
        or any(SENSITIVE.match(key) for key, _ in parse_qsl(parts.query))
    ):
        return None
    if re.search(r"\.(?:bak|sql|env)$|/\.git(?:/|$)", parts.path, re.I):
        return None
    return target


def metadata_links(html, base, headers=None):
    soup = parsed_html(html)
    response_base = base
    base = document_base(html, base)
    result = {}

    def add(raw, label, origin=None):
        url = public_candidate(raw, origin or base)
        if url:
            result[url] = Link(url, label, "metadata")

    selectors = {
        "[data-link]": "data-link",
        "[data-route]": "data-route",
        "[data-path]": "data-path",
        "[data-endpoint]": "data-endpoint",
        "area[href]": "href",
        "[itemid]": "itemid",
        "[resource]": "resource",
        "link[rel=amphtml][href]": "href",
        'meta[property="og:url"]': "content",
        'meta[property="og:see_also"]': "content",
        'meta[property="og:image"]': "content",
        'meta[name="twitter:image"]': "content",
        'meta[name="twitter:url"]': "content",
        'meta[name="DC.relation"]': "content",
    }
    for selector, attr in selectors.items():
        for node in soup.select(selector):
            raw = node.get(attr, "")
            if raw and not raw.startswith("#"):
                add(raw, "Published metadata")
    for node in soup.select("meta[http-equiv]"):
        if node.get("http-equiv", "").lower() == "refresh":
            match = re.search(r"url\s*=\s*(.+)$", node.get("content", ""), re.I)
            if match:
                add(match[1].strip(" \"'"), "Meta refresh destination")
    for node in soup.select("script"):
        if node.get("id") in FRAMEWORK_IDS or node.get("type", '').split(';')[0].strip().lower() in {
            "application/json",
            "application/ld+json",
        }:
            for link in json_links(node.string or node.get_text(), base):
                add(link.url, "Published framework data")
        elif node.get("type", "").lower() in {
            "text/template",
            "text/x-handlebars-template",
        }:
            template = BeautifulSoup(node.string or node.get_text(), "lxml")
            for link in template.select("[href], [src]"):
                add(link.get("href") or link.get("src"), "Published template link")
    for comment in soup.find_all(string=lambda item: isinstance(item, Comment)):
        for value in re.findall(
            r"(?:href|src)\s*=\s*[\"\']([^\"\']+)[\"\']|\b(https?://[^\s<>\"\']+)",
            str(comment),
        ):
            add(value[0] or value[1], "HTML comment URL candidate")
    for value in re.findall(r"\bhttps?://[^\s<>\"\']+", text_without(soup)):
        add(value.rstrip(".,);"), "URL published in page text")
    for name, value in (headers or {}).items():
        if name.lower() == "content-location":
            add(value, "Content-Location candidate", response_base)
        elif name.lower() == "refresh":
            match = re.search(r"url\s*=\s*(.+)$", value, re.I)
            if match:
                add(match[1].strip(" \"'"), "HTTP refresh destination", response_base)
    return list(result.values())


def source_tasks(html, base, headers=None):
    soup = parsed_html(html)
    response_base = base
    base = document_base(html, base)
    tasks = set()
    for node in soup.select("script[src], link[rel=modulepreload][href]"):
        raw = node.get("src") or node.get("href")
        url = public_candidate(raw, base)
        if url:
            tasks.add(("SCRIPT", url))
    for node in soup.select("link[href]"):
        rel = set(node.get("rel", []))
        url = public_candidate(node["href"], base)
        if not url:
            continue
        if "manifest" in rel:
            tasks.add(("MANIFEST", url))
        if "resourcesync" in rel:
            tasks.add(("RESOURCESYNC", url))
        if "https://api.w.org/" in rel:
            tasks.add(("WORDPRESS_ROOT", url))
    for target, relations in header_relations((headers or {}).get("link", ""), response_base):
        url = public_candidate(target, base)
        if url and 'https://api.w.org/' in relations:
            tasks.add(("WORDPRESS_ROOT", url))
        elif url and 'resourcesync' in relations:
            tasks.add(("RESOURCESYNC", url))
    return sorted(tasks)


def inline_scripts(html):
    soup = parsed_html(html)
    return [
        node.string or node.get_text()
        for node in soup.select("script:not([src])")
        if node.get("type", "").lower()
        in {"", "module", "text/javascript", "application/javascript"}
    ]


def manifest_links(payload, base):
    if not isinstance(payload, dict):
        return []
    values = [payload.get("start_url")]
    values.extend(
        item.get("url")
        for item in payload.get("shortcuts", [])
        if isinstance(item, dict)
    )
    return [
        Link(url, "Published app entry/shortcut", "metadata")
        for raw in values
        if isinstance(raw, str) and (url := public_candidate(raw, base))
    ]


def wordpress_collections(payload, root):
    """Only advertised public post/page collections, never users or arbitrary routes."""
    routes = payload.get("routes", {}) if isinstance(payload, dict) else {}
    result = []
    for path, specification in routes.items():
        if path not in {"/wp/v2/posts", "/wp/v2/pages"} or not isinstance(
            specification, dict
        ):
            continue
        if "GET" in specification.get("methods", []):
            # Page size is a request size, not a total-content cutoff.
            result.append(
                urljoin(root.rstrip("/") + "/", path.lstrip("/"))
                + "?per_page=100&_fields=id,link,date,modified&page=1&orderby=id&order=asc"
            )
    return result

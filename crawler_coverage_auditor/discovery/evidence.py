"""Discover URLs from published metadata, feeds and HTTP pagination pointers."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from crawler_coverage_auditor.discovery.page import parsed_html
from crawler_coverage_auditor.discovery.resources import json_links
from crawler_coverage_auditor.discovery.static_links import (
    Link,
    document_base,
    published_url,
)


def embedded_links(html: str, base_url: str) -> list[Link]:
    soup = parsed_html(html)
    base_url = document_base(html, base_url)
    links = []
    for script in soup.select('script'):
        if script.get('type', '').split(';')[0].strip().lower() not in {'application/ld+json', 'application/json'}:
            continue
        for link in json_links(script.string or script.get_text(), base_url):
            links.append(Link(link.url, 'Published structured/hydration data', 'structured'))
    return links


def header_relations(header: str, base_url: str):
    """Parse relation targets once; malformed targets never abort sibling links.

    Commas inside <URLs> are not separators. Quoted multi-token relations and
    additional parameters are accepted without treating their URLs as links.
    """
    for target, parameters in re.findall(r'<([^<>]+)>\s*([^<]*)', header or ''):
        url = published_url(target, base_url)
        if not url:
            continue
        for parameter in _header_parameters(parameters):
            name, separator, value = parameter.partition('=')
            if name.strip().lower() != 'rel' or not separator:
                continue
            value = value.strip().rstrip(',').strip()
            if value.startswith(('"', "'")):
                if len(value) < 2 or value[-1] != value[0]:
                    continue
                value = value[1:-1]
            yield url, frozenset(value.lower().split())


def _header_parameters(value):
    """Do not interpret rel= inside a quoted title as a relation parameter."""
    start, quote, escaped = 0, None, False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
        elif quote and character == '\\':
            escaped = True
        elif quote and character == quote:
            quote = None
        elif not quote and character in {'"', "'"}:
            quote = character
        elif not quote and character == ';':
            yield value[start:index]
            start = index + 1
    yield value[start:]


def header_links(header: str, base_url: str) -> list[Link]:
    links = {}
    for url, relations in header_relations(header, base_url):
        if relations & {'first', 'next', 'last', 'prev', 'previous'}:
            links[url] = Link(url, 'HTTP Link ' + ' '.join(sorted(relations)), 'http-link')
    return list(links.values())


def feed_links(xml: str, base_url: str) -> list[Link]:
    soup = parsed_html(xml, 'xml')
    if not (soup.find('rss') or soup.find('feed')):
        return []
    links = []
    for element in soup.find_all(['link', 'enclosure']):
        target = element.get('href') or element.get('url') or element.get_text(strip=True)
        if target:
            links.append(Link(urljoin(base_url, target), 'Published feed ' + element.get('rel', 'item'), 'feed'))
    return links

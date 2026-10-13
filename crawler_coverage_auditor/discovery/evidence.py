"""Discover URLs from published metadata, feeds and HTTP pagination pointers."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler_coverage_auditor.discovery.resources import json_links
from crawler_coverage_auditor.discovery.static_links import Link


def embedded_links(html: str, base_url: str) -> list[Link]:
    soup = BeautifulSoup(html, 'lxml')
    links = []
    for script in soup.select('script[type="application/ld+json"], script[type="application/json"]'):
        for link in json_links(script.string or script.get_text(), base_url):
            links.append(Link(link.url, 'Published structured/hydration data', 'structured'))
    return links


def header_links(header: str, base_url: str) -> list[Link]:
    # Match angle-bracket URL targets, not comma-separated guesses (targets may contain commas).
    links = []
    for target, parameters in re.findall(r'<([^>]+)>\s*([^<]*)', header or ''):
        relation = re.search(r'\brel\s*=\s*"?([^";,]+)', parameters, re.I)
        if relation and set(relation.group(1).lower().split()) & {'next', 'last', 'prev', 'previous'}:
            links.append(Link(urljoin(base_url, target), 'HTTP Link ' + relation.group(1), 'http-link'))
    from crawler_coverage_auditor.integrations.main_crawler import http_links
    links.extend(Link(url, 'Main crawler HTTP Link', 'http-link') for url in http_links(header, base_url))
    links = list({link.url: link for link in links}.values())
    return links


def feed_links(xml: str, base_url: str) -> list[Link]:
    soup = BeautifulSoup(xml, 'xml')
    if not (soup.find('rss') or soup.find('feed')):
        return []
    links = []
    for element in soup.find_all(['link', 'enclosure']):
        target = element.get('href') or element.get('url') or element.get_text(strip=True)
        if target:
            links.append(Link(urljoin(base_url, target), 'Published feed ' + element.get('rel', 'item'), 'feed'))
    return links

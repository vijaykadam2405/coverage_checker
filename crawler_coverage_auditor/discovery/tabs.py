"""Distinguish already-present page sections from lazy tabs and actual filters."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup

from crawler_coverage_auditor.discovery.page import parsed_html

TAB_SELECTOR = '[role=tab], [data-toggle=tab], [data-bs-toggle=tab]'


@dataclass(frozen=True, slots=True)
class ContentTab:
    label: str
    control_id: str | None
    target_id: str | None
    loaded: bool
    kind: str = 'CONTENT'
    link_url: str | None = None


def same_document_url(first: str, second: str) -> bool:
    """Ordinary section anchors belong to one page; SPA hash routes do not."""
    a, b = urlsplit(first), urlsplit(second)
    if (a.scheme, a.netloc, a.path, a.query) != (b.scheme, b.netloc, b.path, b.query):
        return False
    if a.fragment.startswith(('/', '!/')) or b.fragment.startswith(('/', '!/')):
        return a.fragment == b.fragment
    return True


def panel_has_content(panel) -> bool:
    if panel is None:
        return False
    clone = BeautifulSoup(str(panel), 'lxml')
    for node in clone.select('script, style, template, [role=tab], [aria-busy=true], .spinner'):
        node.decompose()
    text = clone.get_text(' ', strip=True)
    if text and not re.fullmatch(r'(?:loading(?:\s+content)?|please wait)[.\s…]*', text, re.I):
        return True
    return bool(clone.select('img[src], a[href]:not([href^="#"]), video[src], object[data], iframe[src]'))


def detect_tabs(html: str, page_url: str) -> list[ContentTab]:
    soup = parsed_html(html)
    page = urlsplit(page_url)
    result, seen = [], set()
    for node in soup.select(TAB_SELECTOR):
        control = node if node.name in {'a', 'button'} or node.has_attr('tabindex') else node.find(['a', 'button']) or node
        label = control.get_text(' ', strip=True) or control.get('aria-label', '')
        if not label:
            continue
        target = control.get('aria-controls') or control.get('data-bs-target') or control.get('data-target') or control.get('href', '')
        link_url, target_id = None, None
        if target.startswith('#'):
            target_id = unquote(target[1:])
        elif control.get('aria-controls'):
            target_id = target.split()[0]
        elif target and not target.lower().startswith(('javascript:', 'data:')):
            resolved = urlsplit(urljoin(page_url, target))
            if (resolved.scheme, resolved.netloc, resolved.path, resolved.query) == (page.scheme, page.netloc, page.path, page.query):
                target_id = unquote(resolved.fragment) or None
            else:
                link_url = urljoin(page_url, target)
        group = node.find_parent(attrs={'role': 'tablist'})
        group_label = ' '.join(str(group.get(key, '')) for key in ('aria-label', 'id', 'class')) if group else ''
        kind = 'NAVIGATION' if link_url else 'FILTER' if re.search(r'\b(filters?|years?|categor(?:y|ies)|countries|regions|status)\b', group_label, re.I) else 'CONTENT'
        panel = soup.find(id=target_id) if target_id else None
        if target.startswith(('#/', '#!/')) and panel is None:
            kind, link_url = 'NAVIGATION', urljoin(page_url, target)
        lazy = any(control.has_attr(attr) or (panel is not None and panel.has_attr(attr)) for attr in ('data-url', 'data-src', 'data-remote', 'hx-get'))
        loaded = panel_has_content(panel) and not lazy
        # Responsive accordions can repeat the desktop tab labels/panels.
        identity = (kind, label, 'loaded') if kind == 'CONTENT' and loaded else (kind, target_id, label, link_url)
        if identity not in seen:
            seen.add(identity)
            result.append(ContentTab(label, control.get('id') or node.get('id'), target_id, loaded, kind, link_url))
    return result


def merge_tab_content(html: str, fragments: list[str]) -> str:
    """Retain lazy tab content as evidence belonging to ONE HTML page."""
    if not fragments:
        return html
    soup = parsed_html(html, mutable=True)
    owner = soup.find('main') or soup.body or soup
    seen = set()
    for fragment in fragments:
        text = BeautifulSoup(fragment, 'lxml').get_text(' ', strip=True)
        if not text or text in seen or text in owner.get_text(' ', strip=True):
            continue
        seen.add(text)
        section = soup.new_tag('section', attrs={'data-crawlscope-evidence': 'same-page-tab'})
        fragment_soup = BeautifulSoup(fragment, 'lxml')
        section.extend(list((fragment_soup.body or fragment_soup).contents))
        owner.append(section)
    return str(soup)

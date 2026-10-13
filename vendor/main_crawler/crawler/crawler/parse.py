"""Low-level HTML helpers: links, images, title, meta, tables.

Main-content (article body) extraction lives in ``textextract.py`` (trafilatura);
this module is the BeautifulSoup layer the fetcher/harvester use for crawling
(links) and the extractor uses for images/metadata/tables.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, NavigableString

_WS = re.compile(r"\s+")
# Asset extensions we never enqueue as crawlable pages. Documents (pdf/doc/...)
# are captured as ATTACHMENTS (see extract_pdf_links), not crawled as pages, so
# a tender RFP isn't emitted twice (once as attachment, once as its own page).
_NON_PAGE_EXT = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js",
    ".mp4", ".mp3", ".avi", ".mov", ".zip", ".gz", ".woff", ".woff2", ".ttf",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)


# ONE PARSE PER PAGE. Every extractor in this module takes raw html and builds its own soup, and
# processing a single page calls seven of them (_needs_render → visible_text + extract_links,
# _capture_assets → extract_pdf_links, _enqueue_links → extract_links + extract_rel_links, plus
# title_of and extract_meta) — so the same document was parsed seven times. Measured on 11 real
# defence pages (54-655 KB, mean 221 KB): 157.5 ms of BeautifulSoup per page, against 40.2 ms when
# the soup is built once and reused. 3.9x on the parse stages, ~15% of the whole per-page CPU
# budget, and it needed no change at any of the seven call sites.
#
# A one-entry cache is the whole mechanism: the seven calls are consecutive and all carry the SAME
# string, so anything larger would only hold pages nobody will ask for again. Keyed on the object's
# identity AND its length — id() alone is unsafe because CPython reuses the address of a freed
# string, and two same-length bodies at the same address are the same body in this call pattern.
#
# NOT thread-safe by design: build_document runs in a worker thread, so two pages can be in here at
# once. The worst case is a miss (one thread overwrites the other's entry) and a re-parse — which is
# exactly what the code did before this cache existed. Never a wrong soup: the key must match.
_SOUP_CACHE: dict = {"key": None, "soup": None}


def _soup(html: str) -> BeautifulSoup:
    key = (id(html), len(html))
    hit = _SOUP_CACHE
    if hit["key"] == key and hit["soup"] is not None:
        return hit["soup"]
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        # lxml's C parser can choke on hostile/garbled markup — fall back to the
        # slower but pure-python html.parser, which never crashes on bad input.
        soup = BeautifulSoup(html, "html.parser")
    _SOUP_CACHE["key"], _SOUP_CACHE["soup"] = key, soup
    return soup


def title_of(html: str) -> str | None:
    try:
        t = _soup(html).title
        return t.get_text(strip=True)[:2000] if t and t.get_text(strip=True) else None
    except Exception:
        return None


_CHROME_TAGS_LIST = ["script", "style", "noscript", "template", "nav", "header", "footer"]


def visible_text(html: str) -> str:
    """Crude full-page visible text (fallback when trafilatura finds nothing).
    Drops nav/header/footer chrome so entity resolution doesn't pick up menu
    junk ("Our Organisations" etc.) as a company mention.

    SKIPS the chrome, never decompose()s it. This function used to delete those tags from the tree,
    which was harmless while every extractor built its own soup and is destructive now that _soup
    hands out a shared one: _needs_render calls this BEFORE _enqueue_links calls extract_links, so
    a mutating version would strip every navigation link out of the document and the crawl would
    quietly stop finding pages. Same output, no side effect on the caller's tree."""
    soup = _soup(html)
    skip: set[int] = set()
    for tag in soup.find_all(_CHROME_TAGS_LIST):
        skip.add(id(tag))
        skip.update(id(d) for d in tag.descendants)
    # `type(s) is NavigableString` reproduces what get_text() counted as text: Comment, Doctype,
    # Script and Stylesheet are all NavigableString SUBCLASSES, and find_all(string=True) returns
    # them. Accepting them put the doctype, every inline <script> body and every HTML comment into
    # the entity-resolution text — on leonardo.com that turned 31 characters of real copy into
    # 89,606 characters of JavaScript.
    parts = [t for s in soup.find_all(string=True)
             if type(s) is NavigableString and id(s) not in skip and (t := s.strip())]
    return _WS.sub(" ", " ".join(parts)).strip()


# ── JSON API pagers ──────────────────────────────────────────────────────────
# A JSON listing has no <a>, so a crawl of any /api/ endpoint stopped at page 1 for ever. Three
# shapes cover every API in the edge-case corpus, and all three are read from the response the
# crawler already has.
_J_NEXT_URL = re.compile(r"^next(?:[_-]?(?:page|url|link|pageurl|page_url))?$", re.I)
_J_NEXT_TOKEN = re.compile(r"^next[_-]?(?:cursor(?:[_-]?mark)?|page[_-]?token|token)$", re.I)
_J_CURSOR_PARAM = re.compile(r"^(?:cursor(?:[_-]?mark)?|scroll[_-]?id|page[_-]?token|"
                             r"continuation|page\[cursor\])$", re.I)
_J_PAGE_PARAM = re.compile(r"^(?:page|paged|pagina|pagenum|page_num|pageno|pagenumber|pg|pn|p|"
                           r"page\[number\])$", re.I)
_J_OFFSET_PARAM = re.compile(r"^(?:offset|start|from|skip|start_?index|first_?result|"
                             r"page\[offset\])$", re.I)
_J_SIZE_PARAM = re.compile(r"^(?:limit|size|rows|per[_-]?page|page_?size|page\[size\])$", re.I)
_J_TOTAL_KEYS = ("total", "totalcount", "total_count", "total-results", "totalresults",
                 "total_results", "count", "nbhits", "hitcount", "numfound")
_J_PAGES_KEYS = ("nbpages", "totalpages", "total_pages", "pagecount", "page_count", "pages")
# A flag whose TRUE value means "that was the last page", and one whose FALSE value means it.
_J_DONE_TRUE = ("endofrecords", "last", "is_last", "islast", "isfinalpage")
_J_DONE_FALSE = ("has_more", "hasmore", "has_next", "hasnext", "more", "hasmoredata")


def _json_envelope(doc) -> tuple[list[tuple[str, object]], int]:
    """(key, scalar) pairs from the first few DICT levels, plus the longest list length seen.

    Lists are MEASURED but never descended into: their elements are the result items, and an
    item field that happens to be called "next" is not a pager."""
    out: list[tuple[str, object]] = []
    biggest = 0

    def rec(o, d):
        nonlocal biggest
        if d > 3 or not isinstance(o, dict):
            return
        for k, v in o.items():
            if isinstance(v, dict):
                rec(v, d + 1)
            elif isinstance(v, list):
                biggest = max(biggest, len(v))
            else:
                out.append((str(k).lower(), v))

    rec(doc, 0)
    return out, biggest


def _json_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def links_from_json(body: str, base_url: str) -> list[str]:
    """The NEXT-PAGE url of a JSON API response, or [].

    Three shapes cover every API in the corpus:

      * an explicit next URL   crates.io ``meta.next_page`` (relative), DataCite ``links.next``
                               (absolute), Europe PMC ``nextPageUrl``
      * an opaque continuation OpenAlex ``meta.next_cursor``, Europe PMC ``nextCursorMark``. The
                               REQUEST url already names the knob (``cursor=``/``cursorMark=``),
                               so the next page is that url with the new token substituted.
      * plain arithmetic       ``?page=N`` -> N+1, ``?offset=N``/``?from=N`` -> N + page size.

    Deliberately BOUNDED: an increment is emitted only when the body shows there IS another page
    — a non-empty result array, no "that was the last one" flag, and a next index inside any
    published bound. npm's search API silently CLAMPS ``?from=`` past the end and re-serves page 1
    for ever, and GBIF's ``endOfRecords`` never turns true; without the bound test either one is
    an endless frontier.

    A cursor that does not CHANGE (Crossref re-issues one token for a whole scroll) yields []
    rather than a self-link: the same URL cannot be a different page to a frontier that dedups on
    URL, so enqueuing it would only churn.

    Never raises — a body that is not JSON, or JSON that is not a listing, is simply no links."""
    body = (body or "").lstrip()
    if body[:1] not in ("{", "["):
        return []
    try:
        doc = json.loads(body)
    except Exception:
        return []
    if not isinstance(doc, dict):
        return []
    pairs, items_len = _json_envelope(doc)
    flat = dict(pairs)

    # 1. An explicit next URL, wherever in the envelope it sits.
    for k, v in pairs:
        if _J_NEXT_URL.match(k) and isinstance(v, str) and v.strip():
            cand = v.strip()
            if not cand.startswith(("http://", "https://", "/", "?")):
                continue
            try:
                absu = urljoin(base_url, cand).split("#", 1)[0]
            except (ValueError, UnicodeError):
                continue
            if absu.startswith(("http://", "https://")) and absu != base_url:
                return [absu]

    try:
        parts = urlsplit(base_url)
        qs = parse_qsl(parts.query, keep_blank_values=True)
    except (ValueError, UnicodeError):
        return []

    def _rebuild(new_qs) -> list[str]:
        try:
            u = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(new_qs), ""))
        except (ValueError, UnicodeError):
            return []
        return [u] if u != base_url else []

    # 2. An opaque continuation token, substituted into the cursor param the request already names.
    token = next((v.strip() for k, v in pairs
                  if _J_NEXT_TOKEN.match(k) and isinstance(v, str) and v.strip()), None)
    if token:
        for i, (k, v) in enumerate(qs):
            if _J_CURSOR_PARAM.match(k):
                if v == token:              # Crossref: same token every hop — not a new page
                    return []
                new = list(qs)
                new[i] = (k, token)
                return _rebuild(new)

    # 3. Arithmetic — only with a non-empty result array to prove this page had content.
    if items_len <= 0:
        return []
    if any(flat.get(k) is True for k in _J_DONE_TRUE):
        return []
    if any(flat.get(k) is False for k in _J_DONE_FALSE):
        return []
    total = next((_json_int(flat[k]) for k in _J_TOTAL_KEYS if k in flat), None)
    pages = next((_json_int(flat[k]) for k in _J_PAGES_KEYS if k in flat), None)

    for i, (k, v) in enumerate(qs):
        n = _json_int(v)
        if n is None:
            continue
        if _J_PAGE_PARAM.match(k):
            if pages is not None and n + 1 >= pages:
                return []       # Algolia publishes nbPages; page nbPages is the empty one
            new = list(qs)
            new[i] = (k, str(n + 1))
            return _rebuild(new)
        if _J_OFFSET_PARAM.match(k):
            step = next((_json_int(vv) for kk, vv in qs if _J_SIZE_PARAM.match(kk)), None)
            step = step if step and step > 0 else items_len
            if total is not None and n + step >= total:
                return []       # npm CLAMPS ?from= past the end and re-serves page 1
            new = list(qs)
            new[i] = (k, str(n + step))
            return _rebuild(new)
    return []


_BASE_HREF_RE = re.compile('<base\\b[^>]*\\bhref\\s*=\\s*["\\\']([^"\\\']*)["\\\']', re.I)


def _effective_base(html: str, base_url: str) -> str:
    """The base a browser resolves relative hrefs against: <base href> if present, else the page URL.

    RFC 3986 section 5.1.1 puts <base href> ABOVE the retrieval URL, and every extractor here went
    straight to the retrieval URL. Measured on service.bund.de, which serves <base href="/"> and
    writes its tender links relative ("IMPORTE/Ausschreibungen/..."): resolving those against the
    page path produced /Content/DE/Ausschreibungen/Suche/IMPORTE/Ausschreibungen/... -- a URL
    carrying "Ausschreibungen" twice, which looks_like_trap() then correctly rejected as a
    repeated-segment loop. 92 links dropped, the crawl stopped at the seed, and the source scored
    zero while the site was serving its tenders fine.

    Silent by construction: the invented URLs frequently 200 as well, so without reading the trap
    counter it reads as a site that simply has no links.
    """
    m = _BASE_HREF_RE.search(html or "")
    if not m:
        return base_url
    href = m.group(1).strip()
    if not href:
        return base_url
    try:
        return urljoin(base_url, href)
    except (ValueError, UnicodeError):
        return base_url


def extract_links(html: str, base_url: str) -> list[str]:
    """Absolute hrefs from <a>, dropping fragments, mailto/js, and asset files.
    Per-<a> try/except: one malformed href (bad port/IPv6 that makes urljoin/urlsplit
    raise ValueError) is skipped, never aborting extraction of the rest of the page.

    A body that STARTS with { or [ is an API answer, not a page, and is handed to
    links_from_json instead — before the soup, because BeautifulSoup happily lifts <a href>
    out of markup QUOTED INSIDE the JSON. Measured on Algolia page 3 of ?query=python, where
    one HN comment made this function return two invented URLs under the API's own host.
    Nothing links to those, and returning them also masked the real pager."""
    base_url = _effective_base(html, base_url)
    if (html or "").lstrip()[:1] in ("{", "["):
        return links_from_json(html, base_url)
    out: list[str] = []
    seen: set[str] = set()
    for a in _soup(html).find_all("a", href=True):
        try:
            href = a["href"].strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            absu = urljoin(base_url, href).split("#", 1)[0]
            if not absu.startswith(("http://", "https://")):
                continue
            path = urlsplit(absu).path.lower()
            if path.endswith(_NON_PAGE_EXT):
                continue
            if absu not in seen:
                seen.add(absu)
                out.append(absu)
        except (ValueError, UnicodeError):
            continue                 # malformed href — skip this one, keep the rest
    return out


# RFC 8288 pager relations. Deliberately NOT folded into extract_links(): _needs_render() calls
# that and reads "thin text + <3 links" as a JS shell, so adding two <head> <link>s to the count
# would suppress renders the page actually needs. _enqueue_links calls these separately.
_PAGER_REL = {"next", "prev", "previous"}
_LINK_HDR_RE = re.compile(r"<([^>]*)>\s*;\s*([^,]*)")
_LINK_HDR_REL_RE = re.compile(r'rel\s*=\s*"?([^";]+)"?')


def extract_rel_links(html: str, base_url: str, rels: set[str] | None = None) -> list[str]:
    """Absolute hrefs of <link rel="next"> / <a rel="next"> (and prev) — the pager pointers
    extract_links() structurally cannot see, because it only looks at <a>.

    gutenberg.org's only in-head next-page pointer is exactly a <link rel="next">. BeautifulSoup
    returns `rel` as a token LIST, which is why old.reddit's rel="nofollow next" matches here and
    does not match a naive rel="next" regex.

    *rels* narrows which relations count; the crawler wants next AND prev (both are pages worth
    fetching), but anything WALKING a chain must pass {"next"} or it steps backwards."""
    base_url = _effective_base(html, base_url)
    want = rels or _PAGER_REL
    out: list[str] = []
    seen: set[str] = set()
    try:
        tags = _soup(html).find_all(["link", "a"], href=True)
    except Exception:
        return out
    for tag in tags:
        rels_here = tag.get("rel") or []
        if isinstance(rels_here, str):
            rels_here = rels_here.split()
        if not any(str(r).lower() in want for r in rels_here):
            continue
        try:
            absu = urljoin(base_url, tag["href"].strip()).split("#", 1)[0]
        except (ValueError, UnicodeError):
            continue
        if absu.startswith(("http://", "https://")) and absu not in seen:
            seen.add(absu)
            out.append(absu)
    return out


def links_from_link_header(value: str | None, base_url: str,
                           rels: set[str] | None = None) -> list[str]:
    """Pager targets from an RFC 8288 ``Link:`` RESPONSE header.

    For some APIs this is the ONLY pagination signal that exists: api.github.com and every
    WordPress wp-json endpoint answer with a JSON array that contains no next link at all, and put
    ``Link: <...>; rel="next"`` in the header. Never raises; a malformed header yields [].

    *rels* narrows which relations count — see extract_rel_links."""
    want = rels or _PAGER_REL
    out: list[str] = []
    seen: set[str] = set()
    for m in _LINK_HDR_RE.finditer(value or ""):
        rm = _LINK_HDR_REL_RE.search(m.group(2).lower())
        if not rm or not (set(rm.group(1).split()) & want):
            continue
        try:
            absu = urljoin(base_url, m.group(1).strip()).split("#", 1)[0]
        except (ValueError, UnicodeError):
            continue
        if absu.startswith(("http://", "https://")) and absu not in seen:
            seen.add(absu)
            out.append(absu)
    return out


def extract_links_with_text(html: str, base_url: str) -> list[tuple[str, str]]:
    """Same filtering as extract_links(), paired with the anchor's visible
    text (empty string if none). For opt-in link-text relevance (harvest.py)."""
    base_url = _effective_base(html, base_url)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in _soup(html).find_all("a", href=True):
        try:
            href = a["href"].strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            absu = urljoin(base_url, href).split("#", 1)[0]
            if not absu.startswith(("http://", "https://")):
                continue
            path = urlsplit(absu).path.lower()
            if path.endswith(_NON_PAGE_EXT):
                continue
            if absu not in seen:
                seen.add(absu)
                out.append((absu, a.get_text(" ", strip=True)[:300]))
        except (ValueError, UnicodeError):
            continue
    return out


# Document extensions treated as downloadable tender attachments (not crawled as pages).
_ATTACH_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx")
# Gov-portal attachment endpoints that serve a file with no file extension in the URL.
# Conservative on purpose — the fetch then classifies by Content-Type, so a hinted URL that
# turns out to be HTML is dropped (only pdf/image bytes are captured). `/sites/default/files/`
# is Drupal's managed-files path (ddpmod & most gov-CMS "Download" links resolve there as
# extensionless PDFs).
_ATTACH_HINTS = ("download.aspx", "getdoc.aspx", "downloadfile", "docdownload", "/getfile",
                 "/sites/default/files/")
# A link LABELLED like an attachment ("Download", "RFP", "corrigendum") — catches gov listings
# whose file URLs carry no extension or hint (the fetch's %PDF magic-check drops any non-PDF).
_ATTACH_TEXT = re.compile(r"\b(download|annexure|corrigendum|rfp|rfi|tender\s+document)\b", re.I)


def extract_pdf_links(html: str, base_url: str) -> list[str]:
    """Absolute links that look like tender attachments — PDFs/office docs by extension, the
    extensionless Download.aspx?id=… / Drupal /sites/default/files/ style gov portals use, plus
    any <a> LABELLED 'Download'/'RFP'/etc. Content-Type + %PDF magic are verified on fetch, so an
    over-broad match that returns HTML is harmless (it won't be stored)."""
    base_url = _effective_base(html, base_url)
    out: list[str] = []
    for a in _soup(html).find_all("a", href=True):
        try:
            absu = urljoin(base_url, a["href"].strip()).split("#", 1)[0]
            low = absu.lower()
            path = low.split("?", 1)[0]
            text = a.get_text(" ", strip=True)
            if (path.endswith(_ATTACH_EXT) or any(h in low for h in _ATTACH_HINTS)
                    or (text and _ATTACH_TEXT.search(text))):
                out.append(absu)
        except (ValueError, UnicodeError):
            continue
    return list(dict.fromkeys(out))


def extract_media_links(html: str, base_url: str) -> list[dict]:
    """Video/audio links as METADATA ONLY (contract §4 — never downloaded).

    Captures <video>/<audio>/<source> sources, common embed iframes (YouTube /
    Vimeo), and <a> links to media files. Returns {url, type, title}."""
    base_url = _effective_base(html, base_url)
    soup = _soup(html)
    out: list[dict] = []
    seen: set[str] = set()

    def _add(url: str, mtype: str, title: str | None):
        try:
            absu = urljoin(base_url, (url or "").strip()).split("#", 1)[0]
        except (ValueError, UnicodeError):
            return
        if absu.startswith(("http://", "https://")) and absu not in seen:
            seen.add(absu)
            out.append({"url": absu, "type": mtype, "title": title or None})

    for tag in soup.find_all(["video", "audio"]):
        mtype = tag.name
        src = tag.get("src")
        if src:
            _add(src, mtype, tag.get("title"))
        for s in tag.find_all("source"):
            if s.get("src"):
                _add(s["src"], mtype, tag.get("title"))
    for ifr in soup.find_all("iframe", src=True):
        s = ifr["src"]
        if any(h in s for h in ("youtube.com/embed", "youtu.be", "player.vimeo.com")):
            _add(s, "video", ifr.get("title"))
    for a in soup.find_all("a", href=True):
        low = a["href"].lower().split("?", 1)[0]
        if low.endswith((".mp4", ".mov", ".avi", ".webm")):
            _add(a["href"], "video", a.get_text(strip=True))
        elif low.endswith((".mp3", ".wav", ".m4a", ".ogg")):
            _add(a["href"], "audio", a.get_text(strip=True))
    return out


def extract_images(html: str, base_url: str) -> list[dict]:
    """Every <img> as {url, alt, width, height} (filtering happens later)."""
    base_url = _effective_base(html, base_url)
    out: list[dict] = []

    def _int(v):
        try:
            return int(str(v).replace("px", ""))
        except (TypeError, ValueError):
            return None

    for img in _soup(html).find_all("img"):
        try:
            src = (img.get("src") or img.get("data-src") or "").strip()
            if not src or src.startswith("data:"):
                continue
            absu = urljoin(base_url, src).split("#", 1)[0]
            out.append({
                "url": absu,
                "alt": (img.get("alt") or "").strip() or None,
                "width": _int(img.get("width")),
                "height": _int(img.get("height")),
            })
        except (ValueError, UnicodeError):
            continue
    return out


def extract_tables(html: str) -> list[dict]:
    """Each <table> -> {title, rows:[{col: val}]}. Best-effort, capped."""
    soup = _soup(html)
    out: list[dict] = []
    for tbl in soup.find_all("table"):
        header_cells = [th.get_text(" ", strip=True) for th in tbl.find_all("th")]
        rows: list[dict] = []
        for tr in tbl.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if not cells:
                continue
            if header_cells and len(header_cells) == len(cells):
                rows.append(dict(zip(header_cells, cells)))
            else:
                rows.append({f"col{i}": c for i, c in enumerate(cells)})
        if rows:
            cap = tbl.find("caption")
            out.append({
                "title": cap.get_text(" ", strip=True) if cap else None,
                "rows": rows[:200],
            })
    return out[:20]



# --- publication date -------------------------------------------------------
# A news page states its date in one of four places. They are tried in order of
# how much they can be trusted, and a date in the FUTURE is refused at every
# step: it is a misparse (a DD/MM read as MM/DD, a "next issue" line, an event
# listing), never a publication date. Future dates are how this corpus ended up
# with August-fetched articles claiming to be published in December.

_URL_DATE_RX = re.compile(r"/(20\d{2})[/\-_](\d{1,2})[/\-_](\d{1,2})(?:/|\b)")
_ISO_HEAD_RX = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")

# class/id fragments that name an element as carrying the publication date
_PUB_HINT = re.compile(
    r"(publish|pubdate|post-?date|entry-?date|article-?date|byline|"
    r"date-?posted|timestamp)", re.I)

_ARTICLE_LD_TYPES = {
    "newsarticle", "article", "reportagenewsarticle", "blogposting",
    "backgroundnewsarticle", "opinionnewsarticle", "analysisnewsarticle",
    "techarticle", "webpage",
}


def _not_future(raw):
    """The raw string back if it is not in the future, else None.

    Only the leading yyyy-mm-dd is checked, and only when the string starts with
    one -- this is a cheap sanity gate, not a parser. Anything it cannot read it
    lets through, because refusing an unparseable date here would silently drop
    every non-ISO format.
    """
    if not raw:
        return None
    m = _ISO_HEAD_RX.match(raw)
    if not m:
        return raw
    from datetime import date, timedelta
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return raw
    # one day of slack: a page published "today" in a timezone ahead of UTC is
    # not a misparse
    return raw if d <= date.today() + timedelta(days=1) else None


def _walk_ld(node, out):
    """Collect every datePublished in a JSON-LD blob, article types first."""
    if isinstance(node, list):
        for n in node:
            _walk_ld(n, out)
        return
    if not isinstance(node, dict):
        return
    for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement"):
        if key in node:
            _walk_ld(node[key], out)
    val = node.get("datePublished") or node.get("dateCreated")
    if isinstance(val, str) and val.strip():
        t = node.get("@type") or ""
        if isinstance(t, list):
            t = " ".join(str(x) for x in t)
        rank = 0 if str(t).lower() in _ARTICLE_LD_TYPES else 1
        out.append((rank, val.strip()))


def _from_jsonld(soup):
    found = []
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:                               # noqa: BLE001
            # Publishers emit invalid JSON-LD often enough that a failure here
            # must not cost the whole page its date.
            continue
        _walk_ld(data, found)
    for _rank, val in sorted(found, key=lambda p: p[0]):
        ok = _not_future(val)
        if ok:
            return ok
    return None


def _from_time_tags(soup):
    """A <time> that is plausibly the article's own date.

    The old code took the first <time> on the page, which on a news article is
    usually a sidebar item or an event listing.
    """
    candidates = []
    for t in soup.find_all("time"):
        raw = (t.get("datetime") or t.get_text(strip=True) or "").strip()
        if not raw:
            continue
        score = 3
        if (t.get("itemprop") or "").lower() == "datepublished":
            score = 0
        elif _PUB_HINT.search(" ".join(t.get("class") or []) + " " + (t.get("id") or "")):
            score = 1
        elif t.find_parent("article") is not None:
            score = 2
        candidates.append((score, raw))
    for _score, raw in sorted(candidates, key=lambda p: p[0]):
        ok = _not_future(raw)
        if ok:
            return ok
    return None


def _from_url(url):
    if not url:
        return None
    m = _URL_DATE_RX.search(url)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return _not_future("%04d-%02d-%02d" % (y, mo, d))


def _published_date(soup, url=None):
    """The page's publication date as a raw string, or None."""
    got = _from_jsonld(soup)
    if got:
        return got

    def _meta(*selectors):
        for attr, val in selectors:
            tag = soup.find("meta", attrs={attr: val})
            if tag and tag.get("content"):
                cand = _not_future(tag["content"].strip())
                if cand:
                    return cand
        return None

    got = _meta(
        ("property", "article:published_time"),
        ("property", "og:published_time"),
        ("name", "article:published_time"),
        ("name", "publishdate"), ("name", "pubdate"), ("name", "publish-date"),
        ("name", "date"), ("itemprop", "datePublished"),
        ("name", "dc.date"), ("name", "DC.date.issued"),
        ("name", "parsely-pub-date"), ("name", "sailthru.date"),
        ("name", "citation_publication_date"),
        ("name", "cXenseParse:recs:publishtime"),
        ("property", "article:modified_time"),
    )
    if got:
        return got

    got = _from_time_tags(soup)
    if got:
        return got
    return _from_url(url)


def extract_meta(html: str, url: str | None = None) -> dict:
    """Pull author + published date hints from meta tags / JSON-LD / <time>."""
    soup = _soup(html)
    meta: dict = {"author": None, "published_raw": None, "lang": None}

    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        meta["lang"] = html_tag["lang"].split("-")[0].lower()

    def _content(*selectors):
        for attr, val in selectors:
            tag = soup.find("meta", attrs={attr: val})
            if tag and tag.get("content"):
                return tag["content"].strip()
        return None

    meta["published_raw"] = _published_date(soup, url)

    meta["author"] = _content(
        ("name", "author"), ("property", "article:author"), ("name", "byl"),
    )
    return meta

"""Re-resolve the CURRENT spreadsheet URL from the page that publishes it.

SIPRI's filenames carry a year and a version — `SIPRI-Milex-data-1949-2025_v1.2.xlsx` — so every
hardcoded URL in config.FILE_DATASETS rots at the annual update and the harvester starts 404ing on
a dataset that is still perfectly published, three clicks away. Each registry entry therefore
records the `landing` page it was found on, and this module re-derives the href from that page.

TWO THINGS THAT ARE EASY TO GET WRONG HERE:

1. parse.extract_links() CANNOT return these URLs. It is the crawl-frontier helper, and its
   _NON_PAGE_EXT filter drops .xlsx/.xls/.zip on purpose — enqueueing a spreadsheet as a page to
   crawl is exactly what it exists to prevent. The right reuse is parse.extract_pdf_links(), which
   despite its name is the generic ATTACHMENT extractor: _ATTACH_EXT covers .xls/.xlsx and
   _ATTACH_HINTS covers Drupal's `/sites/default/files/` managed-files path, which is where every
   SIPRI data file lives. It also matches on link TEXT ("Download"), so it over-returns and we
   filter its output back down to real data files ourselves.

2. Two registry entries share one landing page (armsindustry publishes both the Top-100 workbook
   and the total-revenues workbook). "Take the best link on the page" would silently collapse both
   entries onto the same file. So resolution is scored against a per-entry `hint` — the filename we
   currently hold — using token similarity with pure-digit tokens dropped, which is precisely what
   survives a rename whose only change is the year.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import unquote, urlsplit

from crawler import parse

from . import config, fetcher

log = logging.getLogger("sipri.discover")

# What counts as a published dataset file. .zip is included because some SIPRI releases ship a
# zipped bundle; .csv because a few pages offer it alongside the workbook.
_DATA_EXT = (".xlsx", ".xls", ".csv", ".zip")
# Drupal's managed-files path. A data file that is NOT under here is almost always a stray link to
# somebody else's site, so it is scored down rather than trusted.
_MANAGED = "/sites/default/files/"
_EXT_RANK = {".xlsx": 40, ".xls": 30, ".csv": 20, ".zip": 10}
_YEAR = re.compile(r"(?:19|20)\d{2}")
_TOKEN = re.compile(r"[^a-z0-9]+")


def _filename(url: str) -> str:
    """Percent-decoded basename. SIPRI's hrefs are encoded ('%20%282%29' is ' (2)'), so anything
    that reads the name — tokens, extension, year — must decode first."""
    return unquote(urlsplit(url).path.rsplit("/", 1)[-1])


def _ext(url: str) -> str:
    name = _filename(url).lower()
    for e in _DATA_EXT:
        if name.endswith(e):
            return e
    return ""


def _tokens(name: str) -> set[str]:
    """Comparable words from a filename, with pure-digit tokens dropped.

    Dropping digits is the whole trick: `SIPRI-Milex-data-1949-2025_v1.2.xlsx` and next year's
    `SIPRI-Milex-data-1949-2026_v1.0.xlsx` share every surviving token, so an entry keeps matching
    its own file across the annual rename instead of drifting onto a sibling dataset."""
    out = set()
    for tok in _TOKEN.split(name.lower()):
        if len(tok) > 1 and not tok.isdigit():
            out.add(tok)
    out.discard("xlsx")
    out.discard("xls")
    out.discard("csv")
    out.discard("zip")
    return out


def _similarity(a: set[str], b: set[str]) -> float:
    """Jaccard, NOT raw overlap. Overlap alone cannot separate the two armsindustry files: the
    Top-100 hint is a subset of the revenues filename, so both score 2 and the longer name wins by
    accident. Jaccard penalises the extra tokens and each entry lands on its own file."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _candidates(html: str, base_url: str) -> list[str]:
    """Every link on the page that looks like a downloadable dataset file."""
    # extract_pdf_links is the attachment extractor (handles .xls/.xlsx + /sites/default/files/);
    # extract_links is added only to catch a .csv, which it does not filter out. Both are reused
    # rather than re-parsing the HTML here.
    seen: list[str] = []
    for url in list(parse.extract_pdf_links(html, base_url)) + list(parse.extract_links(html, base_url)):
        if _ext(url) and url not in seen:
            seen.append(url)
    return seen


def _score(url: str, hint_tokens: set[str]) -> float:
    name = _filename(url)
    score = float(_EXT_RANK.get(_ext(url), 0))
    if _MANAGED in urlsplit(url).path:
        score += 25.0
    if _YEAR.search(name):
        # Data files are year-stamped; boilerplate ('template.xlsx', 'form.xls') is not.
        score += 5.0
    # Dominant term when the caller told us which dataset this is.
    score += 60.0 * _similarity(hint_tokens, _tokens(name))
    # "Most specific" tie-break. True 'largest' would need a HEAD per candidate, and we are not
    # spending a request on a server this slow just to break a tie — longer names are more specific
    # in practice ('Total-arms-revenues-SIPRI-Top-100-...' vs 'SIPRI-Top-100-...').
    score += min(len(name), 120) * 0.01
    return score


def resolve_dataset_url(landing_url: str, html: str | None = None, *,
                        hint: str | None = None, fetch=None) -> str | None:
    """The current dataset href on `landing_url`, absolute, or None.

    `html` short-circuits the fetch (tests pass saved HTML; refresh_registry() passes a page it
    already downloaded so a shared landing page is fetched once, not once per entry).
    `hint` is the URL/filename we currently hold for this dataset — supply it whenever one page
    publishes more than one file, or entries will collide.
    `fetch` is callable(url) -> (bytes, content_type) | None, defaulting to the throttled transport.

    None means "found nothing" — never a guess. The caller keeps the registry URL, which is stale
    but at least was real, instead of a fabricated path that is guaranteed to 404.
    """
    if html is None:
        html = _fetch_html(landing_url, fetch)
        if html is None:
            log.info("landing page unavailable: %s", landing_url)
            return None

    hint_tokens = _tokens(_filename(hint)) if hint else set()
    cands = _candidates(html, landing_url)
    if not cands:
        log.info("no dataset-shaped links on %s", landing_url)
        return None
    # Sort by URL first so equal scores resolve deterministically run to run.
    best = max(sorted(cands), key=lambda u: _score(u, hint_tokens))
    return best


def _fetch_html(url: str, fetch=None) -> str | None:
    got = (fetch or fetcher.get)(url)
    if not got:
        return None
    body = got[0]
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return body


def refresh_registry(entries: list[dict] | None = None, *, fetch=None) -> list[dict]:
    """config.FILE_DATASETS with `url` re-resolved from each landing page.

    Returns COPIES — the registry is import-time constant data and mutating it would make the
    harvester's behaviour depend on whether discovery ran first. Each copy carries:
        url_source: 'resolved' (found on the page) | 'registry' (fell back to the hardcoded URL)
        url_changed: True when discovery found a different file than the registry holds

    Landing pages are fetched at most once each even though several entries can share one, because
    every fetch here costs REQUEST_DELAY_S against a slow server we are a guest on.
    """
    entries = config.FILE_DATASETS if entries is None else entries
    pages: dict[str, str | None] = {}
    out: list[dict] = []

    for entry in entries:
        item = dict(entry)
        landing = item.get("landing")
        registry_url = item.get("url")
        resolved = None

        if landing:
            if landing not in pages:
                pages[landing] = _fetch_html(landing, fetch)
            html = pages[landing]
            if html is not None:
                try:
                    resolved = resolve_dataset_url(landing, html, hint=registry_url)
                except Exception as exc:                    # noqa: BLE001
                    # One unparseable landing page must not cost us the other three datasets.
                    log.warning("resolve failed for %s: %s", item.get("key"), exc)

        item["url"] = resolved or registry_url
        item["url_source"] = "resolved" if resolved else "registry"
        item["url_changed"] = bool(resolved and resolved != registry_url)
        if item["url_changed"]:
            log.info("%s moved: %s -> %s", item.get("key"), registry_url, resolved)
        out.append(item)
    return out

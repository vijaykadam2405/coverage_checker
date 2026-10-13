"""Turn an operator's uploaded spreadsheet of crawl targets into normalised orchestrator rows.

WHY THIS EXISTS
    The Sources tab lets an operator drop in whatever sheet they already keep — a tender tracker, an
    OSINT list, something a colleague mailed over — and get crawl jobs out of it. What arrives is
    never our schema: the url column is called "Website" or "Link", the page budget reads
    "~5,000 pages", and the category is free text like "News" or "Gov Portal". This module is the
    whole translation layer: bytes in, (columns, rows) out, then a second pass that maps the
    operator's vocabulary onto the crawler's job fields.

WHY THERE IS AN XLSX READER IN HERE INSTEAD OF `import openpyxl`
    The orchestrator's dependencies are exactly fastapi, uvicorn, sqlalchemy, pydantic and httpx.
    An `import openpyxl` would work on a laptop that happens to have it and ImportError inside the
    image, which is the worst of both worlds. .xlsx is a ZIP of XML (ECMA-376), so `zipfile` +
    `xml.etree` read it with no dependency and identical behaviour everywhere. The same reader
    already earns its keep in cralwer/sipri_harvester/datasets.py; this is that approach trimmed to
    a single sheet with a known header row.

THE BUG THIS MODULE IS BUILT AROUND
    xlsx omits empty cells entirely, so a row that renders as [example.com, , 500] is stored as two
    <c> elements. A reader that walks cells positionally puts 500 in the category column, and every
    row after the first blank cell lands wrong — no exception, no clue in the UI, just bad jobs.
    Each cell's real column therefore comes from its r= attribute ("C7" -> 2). See _col_index, and
    the gap row asserted explicitly in demo().

NOTHING IS DISCARDED
    Every normalised row carries "_raw" with the operator's original dict, because the review table
    shows their columns verbatim beside what we made of them.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

# Bound the damage an upload endpoint can do to the API process. Exceeded is an explicit failure,
# never a silent truncation: importing 200k of an operator's 250k rows and saying nothing is worse
# than refusing the file.
MAX_ROWS = 200_000

# The orchestrator's taxonomy. Source of truth is sources.CATEGORY_TIER — duplicated (not imported)
# so this module still runs standalone as `python importer.py` for its self-check.
CATEGORIES: tuple[str, ...] = (
    "gov_primary", "manufacturer_ir", "defence_org", "tender_portal", "trade_press",
    "think_tank", "business_press", "aggregator", "blog_forum_social", "general", "unknown",
)

# Cadences the scheduler understands (models.Source.frequency / models.Job.frequency).
FREQUENCIES: frozenset[str] = frozenset({"once", "6h", "daily", "weekly", "monthly", "tiered"})

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_OVERFLOW = "__overflow__"          # csv.DictReader's restkey; cells past the last header column


# ── column aliases ───────────────────────────────────────────────────────────
# Matched on the header name lowercased with every non-alphanumeric character removed, so
# "Max Pages", "max_pages", "MAX-PAGES" and "maxPages" are one column. Order is precedence:
# an explicit "url" beats a "domain" column when the sheet carries both.

_URL_KEYS = ("url", "link", "website", "site", "domain", "homepage", "seed", "seedurl", "address")
_CATEGORY_KEYS = ("category", "type", "sourcetype", "sector", "class")
_MAX_PAGES_KEYS = ("maxpages", "pages", "pagelimit", "estmaxpages", "estpages",
                   "estmaxpage", "maxpage", "pagebudget", "pagecount", "limit")
_MAX_DEPTH_KEYS = ("maxdepth", "depth", "crawldepth")
# Whether the site needs a browser. The crawler decides this per page anyway, but an operator who
# has already checked is worth believing — it saves a wasted C1 attempt on a known JS-only site.
_RENDER_KEYS = ("renderjs", "render", "javascript", "js", "needsjs", "needsrender", "dynamic")
_FREQUENCY_KEYS = ("frequency", "freq", "cadence", "schedule")
_REGION_KEYS = ("region", "country", "geo")
_NOTES_KEYS = ("notes", "note", "comment", "comments", "remarks")

# Operator vocabulary -> taxonomy. Two passes use this: an exact match on the whole cell, then a
# longest-alias-wins substring scan for free text ("Government portal (state)"). Longest-first is
# what keeps "procurement portal" out of gov_primary and "defence news" out of defence_org.
_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "gov_primary": ("gov", "govt", "government", "governmentportal", "govportal", "ministry",
                    "mod", "official", "portal", "publicsector", "regulator", "statebody"),
    "manufacturer_ir": ("manufacturer", "manufacture", "manufacturing", "oem", "company",
                        "corporate", "industry", "vendor", "supplier", "primecontractor",
                        "investorrelations", "ir"),
    "defence_org": ("defence", "defense", "defenceorg", "defenseorg", "military", "armedforces",
                    "alliance", "nato"),
    "tender_portal": ("tender", "tenders", "tenderportal", "procurement", "eprocurement",
                      "rfp", "rfq", "bid", "bids", "bidding", "contractnotice"),
    "trade_press": ("news", "media", "press", "tradepress", "newssite", "newsmedia", "magazine",
                    "journal", "publication", "defencenews", "defensenews"),
    "think_tank": ("thinktank", "research", "institute", "academic", "university", "analysis"),
    "business_press": ("business", "businesspress", "businessnews", "financial", "financialpress",
                       "finance", "economic", "markets"),
    "aggregator": ("aggregator", "aggregate", "newsaggregator", "feed", "rss", "directory",
                   "listing"),
    # Mainstream press. "generalnews" is listed as well as "general" so the intent is readable,
    # though longest-alias-wins already gets "General News" right ("general" is the longer match,
    # so it beats "news" -> trade_press). Verified: "General News" -> general, and "Defence News"
    # still -> trade_press.
    #
    # Nothing vague maps here on purpose. "other" and "misc" stay on `unknown`: those are what a
    # spreadsheet says when the author did not know, and a row nobody classified must not arrive
    # pre-classified.
    "general": ("general", "generalnews", "generalpress", "mainstream", "mainstreamnews"),
    "blog_forum_social": ("blog", "forum", "social", "socialmedia", "community", "twitter",
                          "reddit", "youtube", "telegram", "linkedin"),
    "unknown": ("unknown", "other", "misc", "na", "tbd"),
}
_CATEGORY_BY_ALIAS: dict[str, str] = {a: c for c, aliases in _CATEGORY_ALIASES.items() for a in aliases}
for _cat in CATEGORIES:             # a sheet that already speaks our taxonomy must round-trip
    _CATEGORY_BY_ALIAS.setdefault(re.sub(r"[^a-z0-9]", "", _cat), _cat)
# Aliases under 3 characters are exact-match only: "ir" inside "airport" and "na" inside "canada"
# would otherwise categorise half the sheet.
_SUBSTRING_ALIASES: tuple[str, ...] = tuple(
    sorted((a for a in _CATEGORY_BY_ALIAS if len(a) >= 3), key=lambda a: (-len(a), a)))


# ── shared helpers ───────────────────────────────────────────────────────────

def _text(value) -> str:
    """Every cell that leaves this module is a stripped string; a missing one is ""."""
    return "" if value is None else str(value).strip()


# Qualifier words operators pad headers with, collapsed to the short form the alias tables use.
# Without this, "Maximum Pages" reduces to "maximumpages" and matches NOTHING — which is exactly
# what happened to a real 84-row import: every per-site page and depth budget was silently dropped
# and every job fell back to the class default. Alias tables cannot enumerate English; normalising
# the header before lookup is what makes them hold.
# Deliberately only unambiguous expansions. Deleting generic words ("total", "the", "count") was
# tempting and wrong: it turns "Theme" into "me" and invents matches the operator never wrote.
_HEADER_SYNONYMS = (
    ("maximum", "max"), ("minimum", "min"),
    ("estimated", "est"), ("estimate", "est"), ("approximate", "approx"),
)


def _key(name) -> str:
    """Header name reduced to its comparable form: lowercase, alphanumerics only, with common
    qualifier words normalised so "Maximum Pages", "Max Pages" and "max_pages" all agree."""
    k = re.sub(r"[^a-z0-9]", "", str(name).lower())
    for long, short in _HEADER_SYNONYMS:
        k = k.replace(long, short)
    return k


def _dedupe(names: list[str]) -> list[str]:
    """Column names, blanks named and duplicates suffixed. Two columns both called "Notes" would
    otherwise collapse onto one dict key and lose a column's worth of the operator's data."""
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(names):
        name = (raw or "").strip() or f"column_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        out.append(name)
    return out


# ── xlsx: a ZIP of XML ───────────────────────────────────────────────────────

def _local(tag: str) -> str:
    """Local name of a namespaced tag. Producers disagree on namespace URIs; nobody disagrees on the
    local name, so matching on it is the portable choice."""
    return tag.rsplit("}", 1)[-1]


def _col_index(ref: str | None) -> int | None:
    """'AB12' -> 27. The letters in a cell's r= attribute are the ONLY reliable position: xlsx omits
    empty cells, so [example.com, , 500] is stored as two <c> elements and a positional read shifts
    500 into the wrong column. This is the silent corruption every hand-rolled xlsx reader ships."""
    if not ref:
        return None
    idx = 0
    for ch in ref:
        if ch.isalpha():
            idx = idx * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return idx - 1 if idx else None


def _all_text(el) -> str:
    """Concatenate every <t> under an element. A shared string can be rich text — several <r> runs
    each with its own <t> — and taking only the first drops the rest of the label."""
    return "".join(t.text or "" for t in el.iter() if _local(t.tag) == "t")


def _number_text(raw: str) -> str:
    """Numbers are doubles in xlsx, and producers write 5000 or 5000.0 interchangeably; an operator
    who typed 5000 and reads "5000.0" back in the review table thinks we mangled their file. Dates
    arrive as serial numbers — resolving those needs the style table, which we deliberately do not
    parse, so they pass through as what Excel actually stored."""
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return (raw or "").strip()
    return str(int(f)) if f.is_integer() and abs(f) < 2 ** 53 else str(f)


def _cell_text(c, shared: list[str]) -> str:
    """One cell as text. t="s" is an INDEX into the shared-strings table, not the text — reading it
    as a value yields a sheet of small integers where the labels should be."""
    t = c.get("t")
    if t == "inlineStr":
        return _all_text(c).strip()
    v = next((ch.text for ch in c if _local(ch.tag) == "v"), None)
    if v is None:
        return ""
    if t == "s":
        try:
            return shared[int(v)].strip()
        except (ValueError, IndexError):
            return ""               # a broken index is an absence, never the integer it looks like
    if t == "b":
        return "TRUE" if v == "1" else "FALSE"
    if t == "e":
        return ""                   # #N/A and friends are an absence too
    if t == "str":
        return (v or "").strip()
    return _number_text(v)


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    name = next((n for n in zf.namelist() if n.endswith("sharedStrings.xml")), None)
    if not name:
        return []                   # a workbook may legitimately use only inline strings
    out: list[str] = []
    with zf.open(name) as fh:
        for _ev, el in ET.iterparse(fh, events=("end",)):
            if _local(el.tag) == "si":
                out.append(_all_text(el))
                el.clear()
    return out


def _first_sheet_path(zf: zipfile.ZipFile) -> str:
    """Zip path of the FIRST worksheet in workbook order.

    namelist() order and sheet1.xml are both wrong in general: the part names reflect creation
    order, not tab order, so the sheet the operator sees first can be sheet3.xml. workbook.xml holds
    the real order and its rels resolve the name to a part.
    """
    names = set(zf.namelist())
    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in names:
        with zf.open("xl/_rels/workbook.xml.rels") as fh:
            for rel in ET.parse(fh).getroot():
                rid, target = rel.get("Id"), rel.get("Target")
                if rid and target:
                    rels[rid] = target
    if "xl/workbook.xml" in names:
        with zf.open("xl/workbook.xml") as fh:
            for el in ET.parse(fh).getroot().iter():
                if _local(el.tag) != "sheet":
                    continue
                rid = next((v for k, v in el.attrib.items() if k.endswith("}id") or k == "id"), "")
                target = rels.get(rid, "")
                path = target.lstrip("/") if target.startswith("/") else f"xl/{target.lstrip('./')}"
                if path in names:
                    return path
                hit = next((n for n in names if target and n.endswith(target.lstrip("./"))), "")
                if hit:
                    return hit
                break               # first <sheet> is the one we want; a broken rel falls through
    # No workbook part, or a rel we could not resolve. sheet1 < sheet2 < sheet10 by (length, name).
    sheets = sorted((n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml")),
                    key=lambda n: (len(n), n))
    return sheets[0] if sheets else ""


def _sheet_grid(zf: zipfile.ZipFile, path: str, shared: list[str]) -> list[dict[int, str]]:
    """[{column index: text}] for every non-blank row, sparse by design: only cells that carry a
    value are kept, keyed by their true column index so gaps stay gaps."""
    grid: list[dict[int, str]] = []
    cur: dict[int, str] = {}
    with zf.open(path) as fh:
        for ev, el in ET.iterparse(fh, events=("start", "end")):
            tag = _local(el.tag)
            if ev == "start":
                if tag == "row":
                    cur = {}
                continue
            if tag == "c":
                idx = _col_index(el.get("r"))
                text = _cell_text(el, shared)
                if idx is not None and text:
                    cur[idx] = text
                el.clear()
            elif tag == "row":
                if cur:                 # a fully blank row is spacing, not a record
                    grid.append(cur)
                cur = {}
                el.clear()
                if len(grid) > MAX_ROWS:
                    raise ValueError(f"sheet has more than {MAX_ROWS:,} rows — split the file")
    return grid


def _parse_xlsx(data: bytes) -> tuple[list[str], list[dict]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            path = _first_sheet_path(zf)
            if not path:
                return [], []
            shared = _shared_strings(zf)
            grid = _sheet_grid(zf, path, shared)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a readable .xlsx workbook: {exc}") from exc
    if not grid:
        return [], []

    # Union of the header's columns and every column the data actually uses: a header cell left
    # blank above a populated column would otherwise drop that column silently.
    header, body = grid[0], grid[1:]
    indices = sorted(set(header) | {i for row in body for i in row})
    # A blank header is named from its real column index, not its position in this list: with a gap
    # earlier in the header row those two differ, and "column_2" over Excel's column C sends the
    # operator hunting through the wrong column of their own file.
    columns = _dedupe([header.get(i, "") or f"column_{i + 1}" for i in indices])
    rows = [{name: row.get(idx, "") for idx, name in zip(indices, columns)} for row in body]
    return columns, rows


# ── csv ──────────────────────────────────────────────────────────────────────

def _delimiter(text: str) -> str:
    """Operators export semicolon- and tab-separated files and still call them .csv, so the
    delimiter is sniffed. The candidates are explicit: given a free hand on a short sample, Sniffer
    will cheerfully decide the delimiter is "." or "e"."""
    sample = "\n".join(text.splitlines()[:20])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","                  # one column, or too little to tell — comma reads it fine


def _parse_csv(data: bytes) -> tuple[list[str], list[dict]]:
    # errors="replace" over strict: a Latin-1 export must land as slightly wrong text, never as a
    # 500 on an upload. The BOM Excel writes ahead of the first header would otherwise become part
    # of that column's name ("\ufeffWebsite") — a column no alias could ever match. The escape is
    # spelled out rather than pasted in: an invisible character in source is a bug waiting to be
    # "tidied away" by the next editor.
    text = data.decode("utf-8", "replace").lstrip("\ufeff")
    if not text.strip():
        return [], []

    reader = csv.DictReader(io.StringIO(text), delimiter=_delimiter(text),
                            restval="", restkey=_OVERFLOW)
    columns = _dedupe([n or "" for n in (reader.fieldnames or [])])  # this getter eats the header
    reader.fieldnames = columns     # rows come back keyed by the cleaned names
    rows: list[dict] = []
    for row in reader:
        row.pop(_OVERFLOW, None)    # cells beyond the last header column have nowhere to be shown
        rows.append({c: _text(row.get(c)) for c in columns})
        if len(rows) > MAX_ROWS:
            raise ValueError(f"file has more than {MAX_ROWS:,} rows — split the file")
    return columns, rows


# ── public: parse ────────────────────────────────────────────────────────────

def sniff_format(data: bytes, filename: str) -> str:
    """"xlsx" | "csv", decided on content.

    xlsx is a ZIP, so it starts with PK\\x03\\x04. The magic beats the extension every time:
    operators rename files constantly, and a .csv that is really a workbook parsed as text gives a
    single garbage column instead of an error anyone would notice.
    """
    if data[:4] == b"PK\x03\x04":
        return "xlsx"
    # The extension is a hint, never the decision — it only breaks the tie on an empty body, where
    # there are no magic bytes to trust.
    if not data.strip() and filename.lower().endswith((".xlsx", ".xlsm")):
        return "xlsx"
    return "csv"


def parse(data: bytes, filename: str) -> tuple[list[str], list[dict]]:
    """(column names, rows) from an uploaded file's bytes, dispatched on content not extension.

    Column names are the operator's own header text, stripped — the review table shows their
    columns verbatim. Values are stripped strings, "" for a cell the sheet does not have. An empty
    upload returns ([], []); a body that cannot honestly be read as a table raises ValueError.
    """
    if not data or not data.strip():
        return [], []
    if sniff_format(data, filename) == "xlsx":
        return _parse_xlsx(data)
    return _parse_csv(data)


# ── public: normalise ────────────────────────────────────────────────────────

def _by_key(row: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in row.items():
        k = _key(name)
        if k and not out.get(k):    # first non-empty wins; two headers can reduce to one key
            out[k] = _text(value)
    return out


def _pick(byk: dict[str, str], aliases: tuple[str, ...]) -> str:
    """First alias with a value, in precedence order."""
    for alias in aliases:
        if byk.get(alias):
            return byk[alias]
    return ""


def _bool(value: str) -> bool | None:
    """Tri-state: True / False / None-if-the-operator-did-not-say. None matters — an absent column
    must not read as an explicit "no", or a spreadsheet with no Render JS column would pin every
    site to no-JS instead of letting the crawler decide."""
    v = (value or "").strip().lower()
    if v in ("true", "yes", "y", "1", "t"):
        return True
    if v in ("false", "no", "n", "0", "f"):
        return False
    return None


def _url(value: str) -> str:
    """Bare domains are the norm in operator sheets, so a missing scheme is added rather than
    treated as an error. Anything that cannot be a host — "N/A", "TBD", "see notes" — returns ""
    so the caller reports the row instead of queueing a crawl of https://N/A."""
    parts = value.split()
    if not parts:
        return ""
    url = parts[0]                  # "https://x.com (main site)" is a url with a comment after it
    if not _SCHEME_RE.match(url):
        url = "https://" + url.lstrip("/")
    try:
        host = (urlsplit(url).hostname or "").strip()
    except ValueError:
        # urlsplit raises on a stray bracket ("a[b.com" reads as a malformed IPv6 literal). One
        # junk cell must cost one row, not the whole import.
        return ""
    if host == "localhost":
        return url
    # A host needs a dot with a real label either side: "N/A" has no dot and ".." has no labels.
    label, _dot, tld = host.rpartition(".")
    return url if label.strip(".-") and tld.strip(".-") else ""


def _category(value: str) -> str:
    """Free operator text onto exactly one taxonomy value; unrecognised is "unknown", never a
    guess at tier 1 (see the fail-to-low-trust rule in sources.py)."""
    v = _key(value)
    if not v:
        return "unknown"
    if v in _CATEGORY_BY_ALIAS:
        return _CATEGORY_BY_ALIAS[v]
    for alias in _SUBSTRING_ALIASES:
        if alias in v:
            return _CATEGORY_BY_ALIAS[alias]
    return "unknown"


def _int(value: str, *, signed: bool = False) -> int | None:
    """Operators type budgets as prose: "~5,000 pages", "approx 500", "1 000". Thousands separators
    go first, then the FIRST run of digits wins — so "1,000-2,000" reads as 1000 rather than the
    nonsense 10002000 that stripping every non-digit would give."""
    cleaned = re.sub(r"(?<=\d)[,\s_](?=\d)", "", value.strip())
    m = re.search(r"-?\d+" if signed else r"\d+", cleaned)
    if not m:
        return None
    try:
        return int(m.group())
    except ValueError:              # a digit run too long to be a real budget
        return None


def _frequency(value: str) -> str | None:
    """Only cadences the scheduler actually implements pass through. An operator's "fortnightly"
    becomes None (the caller applies its default) rather than a value nothing will ever run."""
    v = value.strip().lower()
    return v if v in FREQUENCIES else None


def normalise(rows: list[dict]) -> list[dict]:
    """Operator rows -> crawler job fields, one dict per input row, order preserved.

    Keys: url, category, max_pages, max_depth, frequency, region, notes, and "_raw" holding the
    operator's original row verbatim so the UI can show their columns beside ours. A row with no
    usable url gets url="" and is still returned — the caller reports it; parsing never raises over
    one bad row in a thousand-row sheet.
    """
    out: list[dict] = []
    for row in rows:
        byk = _by_key(row)
        out.append({
            "url": _url(_pick(byk, _URL_KEYS)),
            "category": _category(_pick(byk, _CATEGORY_KEYS)),
            "max_pages": _int(_pick(byk, _MAX_PAGES_KEYS)),
            "max_depth": _int(_pick(byk, _MAX_DEPTH_KEYS), signed=True),  # -1 = unlimited
            "frequency": _frequency(_pick(byk, _FREQUENCY_KEYS)),
            "region": _pick(byk, _REGION_KEYS) or None,
            "notes": _pick(byk, _NOTES_KEYS) or None,
            "render_js": _bool(_pick(byk, _RENDER_KEYS)),
            "_raw": dict(row),
        })
    return out


def catalog_max_pages(v: int | None) -> int | None:
    """The import budget rule, applied when a row enters the CATALOG — deliberately NOT in
    normalise(), which stays a pure parsing contract ("~5,000 pages" -> 5000).

    A spreadsheet's page count is the operator's ESTIMATE of a site's size, not a crawl budget: a
    site that turns out a little bigger would stop dead at exactly the estimate. So each site gets
    headroom as it is catalogued:

        under 100   -> 100    (a tiny estimate starves a crawl before it finds anything)
        100 or more -> +50    (354 -> 404)

    Two values pass through untouched:
      None -- the sheet named no budget, so the catalog/class default applies; inventing one here
              would silently override it.
      0    -- 0 means UNLIMITED to the crawler. Clamping it to 100 would CRIPPLE that crawl rather
              than give it headroom, so the sentinel is preserved (guarded elsewhere too, see the
              max_pages-of-zero test in the API suite).
    """
    if v is None or v <= 0:
        return v
    return 100 if v < 100 else v + 50


# ── self-check ───────────────────────────────────────────────────────────────

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def _demo_xlsx() -> bytes:
    """A real workbook, built here rather than committed as a binary fixture so the bytes under
    test are readable in the diff — and so the gap below is visibly a gap.

    Exercises everything that goes wrong in practice: shared strings (t="s" holds an index), an
    inline string, a numeric cell, a decoy second sheet, and row 3 with NO <c r="B3"> at all.
    """
    shared = ["Website", "Type", "Estimated Max Pages",      # 0-2  header
              "example.com", "News", "~5,000 pages",         # 3-5  row 2
              "gap-site.com",                                # 6    row 3 (the gap row)
              "Tender"]                                      # 7    row 4
    si = "".join(f"<si><t>{s}</t></si>" for s in shared)
    sheet1 = (
        f'<?xml version="1.0"?><worksheet xmlns="{_NS_MAIN}"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
        '<c r="C1" t="s"><v>2</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>3</v></c><c r="B2" t="s"><v>4</v></c>'
        '<c r="C2" t="s"><v>5</v></c></row>'
        # THE GAP: column B is absent, not empty. A positional reader files 42 under "Type".
        '<row r="3"><c r="A3" t="s"><v>6</v></c><c r="C3"><v>42</v></c></row>'
        '<row r="4"><c r="A4" t="inlineStr"><is><t>inline-site.com</t></is></c>'
        '<c r="B4" t="s"><v>7</v></c><c r="C4"><v>7</v></c></row>'
        '</sheetData></worksheet>')
    sheet2 = (f'<?xml version="1.0"?><worksheet xmlns="{_NS_MAIN}"><sheetData>'
              '<row r="1"><c r="A1" t="inlineStr"><is><t>DECOY</t></is></c></row>'
              '</sheetData></worksheet>')
    parts = {
        "[Content_Types].xml":
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
            'content-types"><Default Extension="xml" ContentType="application/xml"/></Types>',
        "xl/workbook.xml":
            f'<?xml version="1.0"?><workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}"><sheets>'
            '<sheet name="Targets" sheetId="1" r:id="rId1"/>'
            '<sheet name="Notes" sheetId="2" r:id="rId2"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels":
            f'<?xml version="1.0"?><Relationships xmlns="{_NS_PKG}">'
            '<Relationship Id="rId1" Type="x" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="x" Target="worksheets/sheet2.xml"/>'
            '<Relationship Id="rId3" Type="x" Target="sharedStrings.xml"/></Relationships>',
        "xl/sharedStrings.xml":
            f'<?xml version="1.0"?><sst xmlns="{_NS_MAIN}" count="{len(shared)}" '
            f'uniqueCount="{len(shared)}">{si}</sst>',
        "xl/worksheets/sheet1.xml": sheet1,
        "xl/worksheets/sheet2.xml": sheet2,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in parts.items():
            zf.writestr(name, body)
    return buf.getvalue()


def demo() -> None:
    """Self-check: python importer.py"""
    # ── xlsx ─────────────────────────────────────────────────────────────────
    book = _demo_xlsx()
    assert book[:4] == b"PK\x03\x04", "fixture is not a zip"
    assert sniff_format(book, "targets.csv") == "xlsx", "extension beat the magic bytes"
    assert sniff_format(b"a,b\n1,2\n", "targets.xlsx") == "csv", "extension beat the magic bytes"

    cols, rows = parse(book, "targets.xlsx")
    assert cols == ["Website", "Type", "Estimated Max Pages"], f"shared strings unresolved: {cols}"
    assert rows[0]["Type"] == "News", f"t=\"s\" read as a value, not an index: {rows[0]}"
    assert "DECOY" not in repr(rows), "read the second worksheet instead of the first"
    assert rows[2]["Website"] == "inline-site.com", f"inlineStr dropped: {rows[2]}"

    # The gap case: row 3 has no B cell at all. Positional reading puts 42 under "Type".
    assert rows[1] == {"Website": "gap-site.com", "Type": "",
                       "Estimated Max Pages": "42"}, f"blank middle cell shifted the row: {rows[1]}"

    # ── csv ──────────────────────────────────────────────────────────────────
    semi = b"Website;Type;Region\nexample.org;Gov Portal;IN\nsecond.org;Tender;UK\n"
    cols, rows = parse(semi, "targets.csv")
    assert cols == ["Website", "Type", "Region"], f"delimiter sniffed wrong: {cols}"
    assert rows[0]["Type"] == "Gov Portal", f"delimiter sniffed wrong: {rows[0]}"

    tabbed = "Website\tType\nexample.net\tNews\n".encode()
    assert parse(tabbed, "t.csv")[0] == ["Website", "Type"], "tab-separated .csv not detected"

    bom = "\ufeffWebsite,Type\nexample.net,Tender\n".encode("utf-8")
    cols, rows = parse(bom, "bom.csv")
    assert cols == ["Website", "Type"], f"BOM mangled the first header: {cols!r}"

    assert parse(b"", "empty.csv") == ([], []), "empty upload should not raise"
    assert parse(b"   \n", "blank.csv") == ([], []), "whitespace-only upload should not raise"

    # ── normalise ────────────────────────────────────────────────────────────
    _cols, xrows = parse(book, "targets.xlsx")
    first = normalise(xrows)[0]
    assert first["url"] == "https://example.com", f"bare domain not coerced: {first['url']}"
    assert first["category"] == "trade_press", f'"News" mapped to {first["category"]}'
    assert first["max_pages"] == 5000, f'"~5,000 pages" -> {first["max_pages"]}'
    assert first["_raw"] == xrows[0], "_raw lost the operator's own columns"

    messy = normalise([{"Seed URL": " https://mod.gov.in/tenders ", "Source Type": "Gov Portal",
                       "MAX-PAGES": "1,200", "maxDepth": "-1", "Cadence": "Daily",
                       "Country": "India", "Remarks": "primary"}])[0]
    assert messy["url"] == "https://mod.gov.in/tenders", messy["url"]
    assert messy["category"] == "gov_primary", messy["category"]
    assert messy["max_pages"] == 1200 and messy["max_depth"] == -1, messy
    assert messy["frequency"] == "daily", messy["frequency"]
    assert messy["region"] == "India" and messy["notes"] == "primary", messy

    # THE HEADER THAT SHIPPED BROKEN. A real 84-row import used "Maximum Pages"/"Maximum Depth";
    # both reduced to keys no alias matched, so every per-site budget was silently dropped to None
    # and every generated job fell back to the class default. Silent, and only visible by noticing
    # a 1,227-page site had become a 100,000-page one.
    spelled_out = normalise([{"URL": "https://www.csgroup.eu/", "Maximum Depth": "6",
                              "Maximum Pages": "1227", "Render JS": "TRUE"}])[0]
    assert spelled_out["max_pages"] == 1227, f'"Maximum Pages" -> {spelled_out["max_pages"]}'
    assert spelled_out["max_depth"] == 6, f'"Maximum Depth" -> {spelled_out["max_depth"]}'
    assert spelled_out["render_js"] is True, f'"Render JS" -> {spelled_out["render_js"]}'
    for header in ("Max Pages", "max_pages", "MAXIMUM PAGES", "Estimated Maximum Pages"):
        got = normalise([{"url": "x.com", header: "42"}])[0]["max_pages"]
        assert got == 42, f"{header!r} -> {got}"
    # Tri-state: an absent Render JS column is "operator did not say", NOT an explicit no.
    assert normalise([{"url": "x.com"}])[0]["render_js"] is None
    assert normalise([{"url": "x.com", "Render JS": "false"}])[0]["render_js"] is False
    # ...and the synonym rewrite must not invent matches inside unrelated words.
    assert normalise([{"url": "x.com", "Theme": "dark"}])[0]["notes"] is None

    # A row with no url column at all — reported, never raised.
    nourl = normalise([{"Name": "Something", "Type": "News"}])[0]
    assert nourl["url"] == "", f"invented a url: {nourl['url']}"
    for junk in ("N/A", "see notes", "..", "-", "a[b.com"):   # the last one makes urlsplit raise
        assert _url(junk) == "", f"{junk!r} became a url"
    assert _url("mod.gov.in/tenders") == "https://mod.gov.in/tenders"

    # Absent optional fields stay absent instead of defaulting to something crawlable.
    bare = normalise([{"url": "x.com"}])[0]
    assert (bare["max_pages"], bare["max_depth"], bare["frequency"]) == (None, None, None), bare

    # ── catalog budget rule: under 100 -> 100, else +50 ──────────────────────
    assert catalog_max_pages(354) == 404, catalog_max_pages(354)
    assert catalog_max_pages(100) == 150, catalog_max_pages(100)   # 100 is not "below 100"
    assert catalog_max_pages(99) == 100 and catalog_max_pages(1) == 100
    assert catalog_max_pages(5000) == 5050, catalog_max_pages(5000)
    assert catalog_max_pages(None) is None                          # no budget -> catalog default
    assert catalog_max_pages(0) == 0                                # 0 = unlimited, never clamped
    # Parsing stays pure — the rule lives at the catalog boundary, not in normalise().
    assert normalise([{"url": "x.com", "Max Pages": "354"}])[0]["max_pages"] == 354
    assert bare["category"] == "unknown", bare["category"]
    assert normalise([{"url": "x.com", "freq": "fortnightly"}])[0]["frequency"] is None

    # Category vocabulary: every mapping lands inside the taxonomy, and the ambiguous pairs the
    # longest-alias rule exists for go the right way.
    for text, want in [("news", "trade_press"), ("Media", "trade_press"),
                       ("Defence News", "trade_press"), ("manufacturer", "manufacturer_ir"),
                       ("OEM", "manufacturer_ir"), ("Industry", "manufacturer_ir"),
                       ("procurement portal", "tender_portal"), ("RFP", "tender_portal"),
                       ("govt", "gov_primary"), ("Ministry of Defence", "gov_primary"),
                       ("think tank", "think_tank"), ("Research Institute", "think_tank"),
                       ("Business Press", "business_press"), ("nato", "defence_org"),
                       ("blog", "blog_forum_social"), ("", "unknown"), ("qwerty", "unknown")]:
        got = _category(text)
        assert got == want, f"{text!r} -> {got}, expected {want}"
    assert set(_CATEGORY_BY_ALIAS.values()) <= set(CATEGORIES), "alias table left the taxonomy"

    print("all importer self-checks passed")


if __name__ == "__main__":
    demo()

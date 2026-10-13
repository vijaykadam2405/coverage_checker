"""Download SIPRI's published spreadsheets, store the raw bytes, and read them into rows.

WHY THERE IS AN XLSX READER IN HERE INSTEAD OF `import openpyxl`
    There is no spreadsheet library in this project — not in requirements.txt, not in the venv, not
    in the system interpreter — and requirements.txt is not ours to change. An `import openpyxl`
    would work on a laptop that happens to have it and ImportError inside the Docker image, which is
    the worst of both worlds. .xlsx is just a ZIP of XML (ECMA-376), and `zipfile` + `xml.etree` are
    stdlib, so the reader below is ~150 lines with no dependency and identical behaviour everywhere.
    If pandas/openpyxl ever lands in requirements.txt this can be swapped out behind parse_workbook().

WHY HEADER DETECTION IS A HEURISTIC AND NOT `rows[0]`
    A SIPRI sheet opens with a title banner and a units note, usually a blank row, THEN the real
    header, and closes with footnotes (".. = data unavailable"). Taking row 1 as the header yields
    one column called "SIPRI Military Expenditure Database 2025" and every real column silently
    unnamed. The detector below finds the first row wide enough to BE the data grid, and the row it
    picked is reported in the summary so the decision is auditable rather than magic.

    Note the trap: SIPRI's header cells are YEARS. Any "headers are text, data is numeric" rule —
    the obvious one — picks the wrong row on every milex sheet. Width and shape are used instead.

HONESTY RULE
    Nothing is ever presented as more interpreted than it is. Three outcomes, all explicit in the
    per-sheet summary, verified against the four live files:
      * header found      -> header_row is the Excel row number used, note=None.
      * grid but no header-> columns are col0/col1..., note SAYS the header was not found. milex's
                             'Footnotes' glossary is the real case: adopting its first definition
                             row as column names invented a column named after a footnote and ate
                             that row, so the rows are kept and the columns are honestly unnamed.
      * no grid at all    -> zero rows and a reason ('Front page' is a cover image, no cells).
    The one thing never done is inventing column names from data and presenting the result as a
    typed table.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import time
import zipfile
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from . import config, fetcher

log = logging.getLogger("sipri.datasets")

# Bound the damage from a pathological workbook; truncation is reported, never silent.
MAX_ROWS_PER_SHEET = 500_000
# How far down to look for a header before giving up. SIPRI banners run 2-6 rows; 40 is slack.
HEADER_SCAN_ROWS = 40
# A header row must be at least this fraction of the sheet's widest row. Banners and footnotes are
# 1-2 cells wide against a 70-column grid, so they fall out immediately.
HEADER_MIN_WIDTH_RATIO = 0.6

_CONTENT_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "csv": "text/csv",
    "zip": "application/zip",
}


# ── xlsx: the OPC package ────────────────────────────────────────────────────

def _local(tag: str) -> str:
    """Local name of a namespaced tag. Producers differ on namespace URIs; nobody differs on the
    local name, so matching on it is the portable choice."""
    return tag.rsplit("}", 1)[-1]


def _col_index(ref: str | None) -> int | None:
    """'AB12' -> 27. The column letters in a cell's r= attribute are the ONLY reliable position:
    xlsx omits empty cells entirely, so a row that renders as [Benin, , , , 9] is stored as two
    <c> elements. Reading cells positionally shifts every value left into the wrong column — the
    classic silent corruption in hand-rolled xlsx readers."""
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
    """Concatenate every <t> under an element. A shared string can be rich text — several <r> runs,
    each with its own <t> — and taking only the first drops the rest of the label."""
    return "".join(t.text or "" for t in el.iter() if _local(t.tag) == "t")


def _num(raw: str):
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return raw
    return int(f) if f.is_integer() and abs(f) < 2 ** 53 else f


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    name = next((n for n in zf.namelist() if n.endswith("sharedStrings.xml")), None)
    if not name:
        return []                       # a workbook can legitimately use only inline strings
    out: list[str] = []
    with zf.open(name) as fh:
        for _ev, el in ET.iterparse(fh, events=("end",)):
            if _local(el.tag) == "si":
                out.append(_all_text(el))
                el.clear()
    return out


def _sheet_targets(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """[(sheet name, zip path)] in workbook order. Sheet order is meaningful in these files (the
    first sheet is the headline series), so it is preserved rather than using namelist() order."""
    wb = next((n for n in zf.namelist() if n.endswith("xl/workbook.xml") or n == "workbook.xml"), None)
    if not wb:
        return []
    rels_name = wb.rsplit("/", 1)[0]
    rels_name = f"{rels_name}/_rels/workbook.xml.rels" if "/" in wb else "_rels/workbook.xml.rels"
    rels: dict[str, str] = {}
    if rels_name in zf.namelist():
        with zf.open(rels_name) as fh:
            for rel in ET.parse(fh).getroot():
                rid, target = rel.get("Id"), rel.get("Target")
                if rid and target:
                    rels[rid] = target

    base = wb.rsplit("/", 1)[0] if "/" in wb else ""
    names = set(zf.namelist())
    out: list[tuple[str, str]] = []
    with zf.open(wb) as fh:
        for el in ET.parse(fh).getroot().iter():
            if _local(el.tag) != "sheet":
                continue
            title = el.get("name") or f"sheet{len(out) + 1}"
            rid = next((v for k, v in el.attrib.items() if k.endswith("}id")), None)
            target = rels.get(rid or "", "")
            if target.startswith("/"):
                path = target.lstrip("/")
            else:
                path = f"{base}/{target}" if base else target
            if path not in names:       # some producers write the target without the xl/ prefix
                path = next((n for n in names if n.endswith(target.lstrip("/"))), "") if target else ""
            if path:
                out.append((title, path))
    return out


def _cell_value(c, shared: list[str]):
    t = c.get("t")
    if t == "inlineStr":
        return _all_text(c) or None
    v = next((ch.text for ch in c if _local(ch.tag) == "v"), None)
    if v is None:
        return None
    if t == "s":
        try:
            return shared[int(v)]
        except (ValueError, IndexError):
            return None
    if t == "b":
        return v == "1"
    if t == "e":
        return None                     # #N/A etc. is an absence, not a value worth propagating
    if t in ("str", "d"):
        return v
    return _num(v)


def _sheet_rows(zf: zipfile.ZipFile, path: str, shared: list[str],
                max_rows: int = MAX_ROWS_PER_SHEET) -> tuple[list[tuple[int, dict]], bool]:
    """[(excel row number, {column index: value})], blank rows omitted, plus a truncated flag.

    Sparse by design: only non-empty cells are kept, and the real 1-based Excel row number is
    carried so the header row reported in the summary is the row a human sees when they open the
    file — not an index into a list we compacted.
    """
    rows: list[tuple[int, dict]] = []
    cur: dict[int, object] = {}
    rowno = 0
    truncated = False
    with zf.open(path) as fh:
        for ev, el in ET.iterparse(fh, events=("start", "end")):
            tag = _local(el.tag)
            if ev == "start":
                if tag == "row":
                    cur = {}
                    try:
                        rowno = int(el.get("r") or (rows[-1][0] + 1 if rows else 1))
                    except (TypeError, ValueError):
                        rowno = rows[-1][0] + 1 if rows else 1
                continue
            if tag == "c":
                idx = _col_index(el.get("r"))
                val = _cell_value(el, shared)
                if idx is not None and val is not None and val != "":
                    cur[idx] = val
                el.clear()
            elif tag == "row":
                if cur:
                    rows.append((rowno, cur))
                cur = {}
                el.clear()
                if len(rows) >= max_rows:
                    truncated = True
                    break
    return rows, truncated


# ── header detection ─────────────────────────────────────────────────────────

def _is_prose(value) -> bool:
    """True if a cell reads as a sentence rather than a column label.

    Measured against the real files: milex's 'Footnotes' sheet is a two-column glossary with NO
    header at all, so the first definition row — '†' | 'Figures for these countries do not include
    military pensions.' — was being adopted as the column names, inventing a column called after a
    footnote and swallowing that row's data.

    The discriminator is the full stop, not the word count. Word count cannot separate them: the
    footnote above is 9 words and the Top-100 header 'Arms revenues 2002 (constant 2024 prices)
    note (d)' is 8. Headers end in a word or a bracket; sentences end in a period.
    """
    s = str(value).strip()
    return len(s) > 120 or (s.endswith(".") and len(s.split()) >= 5)


def detect_header_row(rows: list[tuple[int, dict]]) -> int | None:
    """Index into `rows` of the header, or None if the sheet has no readable grid.

    The rule: the header is the FIRST row that is as wide as the data grid, reads like labels, and
    is actually followed by data. Concretely a row qualifies when it has >= 2 cells, is at least
    HEADER_MIN_WIDTH_RATIO of the widest row on the sheet, carries distinct non-prose labels, and is
    followed by >= 2 rows of comparable width. Topmost wins — a header is above its data by
    definition.

    Width rather than type is deliberate: SIPRI's headers are years, so any rule that expects text
    labels over numeric data chooses a data row on every milex sheet.
    """
    if not rows:
        return None
    scan = rows[:HEADER_SCAN_ROWS]
    widest = max(len(cells) for _r, cells in rows)
    floor = max(2, int(widest * HEADER_MIN_WIDTH_RATIO))

    for i, (_rowno, cells) in enumerate(scan):
        if len(cells) < floor:
            continue                    # banner, units note or footnote — too narrow to be a header
        labels = [str(v).strip() for v in cells.values() if str(v).strip()]
        if len(set(labels)) < max(2, len(labels) - 1):
            continue                    # repeated values read as data, not as column names
        if any(_is_prose(v) for v in cells.values()):
            continue                    # a glossary definition, not a column name
        following = [c for _r, c in rows[i + 1:i + 5] if len(c) >= floor]
        if len(following) >= 2:
            return i
        # Last resort: a tiny sheet (header + one row) still deserves to be read.
        if len(rows) - i - 1 == 1 and following:
            return i
    return None


def _columns(header_cells: dict, data_indices=()) -> list[tuple[int, str]]:
    """[(column index, name)] left to right, over the union of the header's columns and every column
    the data actually uses.

    The union matters: 'Total arms revenues' is a transposed table whose header row holds the years
    starting at column B, leaving A blank above the row labels ('current US$ (billion)', '% change').
    Taking column names from the header row alone leaves column A nameless and drops the label that
    says which series each row IS — the single most important field on the sheet.

    Blanks are named col{index}; duplicates are disambiguated, because two columns both called
    'Notes' otherwise collapse into one dict key and silently lose a column's worth of data.
    """
    out: list[tuple[int, str]] = []
    used: dict[str, int] = {}
    for idx in sorted(set(header_cells) | set(data_indices)):
        name = str(header_cells.get(idx, "")).strip() or f"col{idx}"
        if name in used:
            used[name] += 1
            name = f"{name}_{used[name]}"
        else:
            used[name] = 1
        out.append((idx, name))
    return out


def _read_sheet(zf: zipfile.ZipFile, title: str, path: str, shared: list[str]) -> dict:
    raw, truncated = _sheet_rows(zf, path, shared)
    info: dict = {"name": title, "header_row": None, "columns": [], "rows": 0,
                  "footnote_rows": 0, "truncated": truncated, "note": None}
    if not raw:
        info["note"] = "sheet is empty"
        return info

    hidx = detect_header_row(raw)
    if hidx is None:
        grid = [c for _r, c in raw if len(c) >= 2]
        if len(grid) < 2:
            # A cover sheet or a free-form notes tab: no grid, nothing to interpret. Say so.
            info["note"] = "no header row found — sheet has no tabular grid (cover/notes sheet?)"
            return info
        # There IS a grid but nothing in it reads as column names — milex's 'Footnotes' glossary is
        # the real case. Keep every row, name the columns positionally, and SAY that is what
        # happened. Inventing names from the first data row is the failure this avoids; dropping
        # 114 rows of real content on the floor is the other one.
        cols = _columns({}, {i for _r, c in raw for i in c})
        info.update(header_row=None, columns=[n for _i, n in cols],
                    rows=len(raw), data=[{n: c.get(i) for i, n in cols} for _r, c in raw],
                    note="no header row found — columns are positional (col0, col1, ...)")
        return info

    body = raw[hidx + 1:]
    cols = _columns(raw[hidx][1], {i for _r, c in body for i in c})
    width = len(cols)
    out_rows: list[dict] = []
    footnotes = 0
    for _rowno, cells in body:
        if len(cells) < 2 and width >= 4:
            footnotes += 1              # trailing "Notes: .. = data unavailable" and friends
            continue
        out_rows.append({name: cells.get(idx) for idx, name in cols})

    info.update(header_row=raw[hidx][0], columns=[n for _i, n in cols],
                rows=len(out_rows), footnote_rows=footnotes, data=out_rows)
    return info


# ── public parsing entry point ───────────────────────────────────────────────

def _looks_like_xlsx(data: bytes) -> bool:
    """xlsx is a ZIP, so it starts with PK. A 200 that actually served an HTML error page or a login
    wall reaches here as 'xlsx' by URL extension; storing that would give downstream a workbook that
    will not open. Same lesson as pdfextract._looks_like_pdf()."""
    return data[:2] == b"PK"


def parse_workbook(data: bytes, kind: str = "xlsx") -> dict:
    """{sheets: [...], rows: int, error: str | None}.

    Each sheet reports its name, the header row NUMBER it used (1-based, as Excel shows it), the
    column names, and the row count; `data` holds the parsed rows. `error` is set when nothing could
    be read at all — never raises, because one bad workbook must not end a harvest run.
    """
    kind = (kind or "xlsx").lower()
    try:
        if kind == "csv":
            return _parse_csv(data)
        if kind == "zip":
            # Deliberately not expanded: what is inside varies per release, and guessing at it is how
            # you end up parsing a readme as a data table.
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                members = [n for n in zf.namelist() if not n.endswith("/")]
            return {"sheets": [], "rows": 0,
                    "error": f"archive not expanded ({len(members)} members): {members[:10]}"}
        if kind == "xls" and not _looks_like_xlsx(data):
            # Real BIFF .xls is a compound binary file, not a zip. Reading it needs a library we do
            # not have; the bytes are still stored, so this is a gap, not a loss.
            return {"sheets": [], "rows": 0,
                    "error": "legacy .xls (BIFF) is not readable without a spreadsheet library"}
        if not _looks_like_xlsx(data):
            head = data[:64].decode("utf-8", "replace").strip()
            return {"sheets": [], "rows": 0,
                    "error": f"not a spreadsheet — body starts {head!r} (error page instead of a file?)"}

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            shared = _shared_strings(zf)
            targets = _sheet_targets(zf)
            if not targets:
                return {"sheets": [], "rows": 0, "error": "zip is not a workbook (no sheets found)"}
            sheets = []
            for title, path in targets:
                try:
                    sheets.append(_read_sheet(zf, title, path, shared))
                except Exception as exc:                    # noqa: BLE001
                    # One malformed sheet must not cost the other five.
                    log.warning("sheet %r failed: %s", title, exc)
                    sheets.append({"name": title, "header_row": None, "columns": [], "rows": 0,
                                   "footnote_rows": 0, "truncated": False,
                                   "note": f"unreadable: {type(exc).__name__}: {exc}"})
        total = sum(s["rows"] for s in sheets)
        unreadable = [s["name"] for s in sheets if s["note"]]
        error = None
        if total == 0:
            error = f"no rows interpreted; sheets: {unreadable or [s['name'] for s in sheets]}"
        return {"sheets": sheets, "rows": total, "error": error}
    except zipfile.BadZipFile as exc:
        return {"sheets": [], "rows": 0, "error": f"corrupt zip container: {exc}"}
    except Exception as exc:                                # noqa: BLE001
        return {"sheets": [], "rows": 0, "error": f"{type(exc).__name__}: {exc}"}


def _parse_csv(data: bytes) -> dict:
    text = data.decode("utf-8-sig", "replace")
    reader = list(csv.reader(io.StringIO(text)))
    rows = [(i + 1, {j: v for j, v in enumerate(r) if v.strip()}) for i, r in enumerate(reader) if any(c.strip() for c in r)]
    if not rows:
        return {"sheets": [], "rows": 0, "error": "csv is empty"}
    hidx = detect_header_row(rows)
    if hidx is None:
        return {"sheets": [{"name": "csv", "header_row": None, "columns": [], "rows": 0,
                            "footnote_rows": 0, "truncated": False, "note": "no header row found"}],
                "rows": 0, "error": "no header row found"}
    body = rows[hidx + 1:]
    cols = _columns(rows[hidx][1], {i for _r, c in body for i in c})
    out = [{name: cells.get(idx) for idx, name in cols} for _r, cells in body]
    return {"sheets": [{"name": "csv", "header_row": rows[hidx][0], "columns": [n for _i, n in cols],
                        "rows": len(out), "footnote_rows": 0, "truncated": False, "note": None,
                        "data": out}],
            "rows": len(out), "error": None}


# ── download + harvest ───────────────────────────────────────────────────────

def _download(url: str, fetch=None) -> tuple[bytes, str]:
    """(body, content_type). Raises RuntimeError when every attempt failed.

    ONE retry loop, not two. The default transport already retries MAX_RETRIES times behind a
    process-wide throttle, so wrapping it in a second loop would put MAX_RETRIES**2 requests on a
    server we are explicitly a guest on. An injected fetch makes no such promise, so it gets the
    loop — with REQUEST_DELAY_S between attempts.
    """
    if fetch is None:
        got = fetcher.get(url)
        if not got:
            raise RuntimeError(f"download failed after {config.MAX_RETRIES} attempts: {url}")
        return got[0], got[1]

    last = "no attempt made"
    for attempt in range(max(1, config.MAX_RETRIES)):
        try:
            got = fetch(url)
            if got and got[0]:
                return got[0], (got[1] if len(got) > 1 else "") or ""
            last = "empty response"
        except Exception as exc:                            # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < max(1, config.MAX_RETRIES):
            time.sleep(config.REQUEST_DELAY_S)
    raise RuntimeError(f"download failed after {config.MAX_RETRIES} attempts ({last}): {url}")


def _body_mismatch(data: bytes, kind: str) -> str | None:
    """Reason the body is not the file type we asked for, or None if it is plausible.

    A WAF challenge, a login wall or a Drupal 404 all arrive as HTTP 200 with HTML, and the URL
    extension still says .xlsx. pdfextract._looks_like_pdf() exists for exactly this reason and
    drops the attachment rather than storing something that will not open; a dataset gets the same
    treatment, because a bucket object full of 'Access Denied' is worse than a missing one.
    """
    if not data:
        return "empty body"
    if kind in ("xlsx", "zip") and not _looks_like_xlsx(data):
        head = data[:80].decode("utf-8", "replace").strip()
        return f"expected a {kind} (ZIP) but body starts {head!r}"
    if kind == "xls" and not (_looks_like_xlsx(data) or data[:4] == b"\xd0\xcf\x11\xe0"):
        head = data[:80].decode("utf-8", "replace").strip()
        return f"expected an .xls but body starts {head!r}"
    return None


def _prev_sha(prev) -> str | None:
    """sha256 off whatever store.get_dataset() hands back. store.py is another module's business and
    may return a dict or a row object; reading it defensively is cheaper than coupling to its shape,
    and a wrong guess here would silently re-parse every file on every run."""
    if not prev:
        return None
    if isinstance(prev, dict):
        return prev.get("sha256")
    return getattr(prev, "sha256", None)


def _blob_key(entry: dict, sha: str, url: str) -> str:
    """Content-addressed object name: a re-harvest that finds new data writes a NEW object instead
    of overwriting the bytes the previous run's row still points at."""
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) or f"{entry.get('key', 'dataset')}.bin"
    safe = "".join(ch if ch.isalnum() or ch in "-._" else "_" for ch in name)[:120]
    return f"datasets/{entry.get('key', 'dataset')}/{sha[:12]}-{safe}"


def harvest_file_dataset(entry: dict, store, fetch=None) -> dict:
    """Fetch one published SIPRI spreadsheet, store it, read it, and report what happened.

    entry: a config.FILE_DATASETS row — {key, title, landing, url, kind}.
    store: the storage module — get_dataset(key), put_blob(key, data, content_type),
           record_dataset(**kw). The bucket (config.MINIO_BUCKET) is store's business, not ours.
    fetch: callable(url) -> (bytes, content_type), injected by tests so nothing here needs network.

    Returns {key, status, bytes, sha256, sheets, rows, error, ...}. status is:
        unchanged — byte-identical to what we already hold; NOT re-parsed, NOT re-uploaded
        fetched   — new or changed bytes, stored and read
        failed    — could not be downloaded or stored

    A download failure is REPORTED, not raised: a harvest run covers several datasets and one 404
    must not take the other three with it.

    status='fetched' with a non-None error means the bytes are safely in the object store but the
    content could not be interpreted — deliberately not 'failed', because the raw file is captured
    and re-parsing later costs nothing.
    """
    key = entry.get("key") or "unknown"
    url = entry.get("url") or ""
    summary: dict = {"key": key, "status": "failed", "bytes": 0, "sha256": None,
                     "sheets": [], "rows": 0, "error": None, "url": url,
                     "minio_key": None, "kind": entry.get("kind", "xlsx")}
    if not url:
        summary["error"] = "entry has no url"
        return summary

    try:
        data, content_type = _download(url, fetch)
    except Exception as exc:                                # noqa: BLE001
        summary["error"] = str(exc)
        log.warning("%s: %s", key, exc)
        return summary

    sha = hashlib.sha256(data).hexdigest()
    kind = (entry.get("kind") or _kind_from(url, content_type)).lower()
    summary.update(bytes=len(data), sha256=sha, content_type=content_type, kind=kind)

    mismatch = _body_mismatch(data, kind)
    if mismatch:
        summary["error"] = mismatch
        log.warning("%s: %s", key, mismatch)
        return summary                  # status stays 'failed'; nothing is stored

    # Idempotency. SIPRI serves no ETag we can trust across their annual rebuilds, so the compare is
    # on content: the download still happens, but the expensive half — XML parse, upload, DB write —
    # is skipped entirely when the bytes have not moved.
    try:
        if _prev_sha(store.get_dataset(key)) == sha:
            summary.update(status="unchanged")
            log.info("%s unchanged (%s)", key, sha[:12])
            return summary
    except Exception as exc:                                # noqa: BLE001
        # Store unavailable — carry on and re-harvest. Redundant work beats skipping a real update.
        log.warning("%s: get_dataset failed, treating as new: %s", key, exc)

    try:
        minio_key = store.put_blob(_blob_key(entry, sha, url), data,
                                   content_type or _CONTENT_TYPES.get(kind, "application/octet-stream"))
    except Exception as exc:                                # noqa: BLE001
        summary["error"] = f"put_blob failed: {type(exc).__name__}: {exc}"
        log.warning("%s: %s", key, summary["error"])
        return summary
    if not minio_key:
        # store.put_blob() answers None when the object did NOT land, and its docstring spells out
        # the consequence of ignoring that: recording a sha256 for a blob that was never stored
        # makes the next run's skip-check say "unchanged" forever, so the file is never fetched
        # again. Fail here instead, and do not write the registry row.
        summary["error"] = "put_blob returned None — bytes were not stored, registry row withheld"
        log.warning("%s: %s", key, summary["error"])
        return summary
    summary["minio_key"] = minio_key

    parsed = parse_workbook(data, kind)
    sheets = [{k: v for k, v in s.items() if k != "data"} for s in parsed["sheets"]]
    summary.update(status="fetched", sheets=sheets, rows=parsed["rows"], error=parsed["error"])

    # Keyword-for-keyword against store.record_dataset: source_url (not url), size_bytes (not
    # bytes), and no sheets/error — it takes an explicit signature, not **kw, so a near-miss here is
    # a TypeError on every dataset rather than a silently missing column.
    try:
        ok = store.record_dataset(key=key, title=entry.get("title") or "", source_url=url,
                                  landing_url=entry.get("landing") or "", kind=kind,
                                  sha256=sha, size_bytes=len(data), minio_key=minio_key,
                                  row_count=parsed["rows"])
    except Exception as exc:                                # noqa: BLE001
        summary["status"] = "failed"
        summary["error"] = f"record_dataset failed: {type(exc).__name__}: {exc}"
        log.warning("%s: %s", key, summary["error"])
        return summary
    if ok is False:                                         # documented: False means the row did not land
        summary["status"] = "failed"
        summary["error"] = "record_dataset returned False — registry row did not land"
        log.warning("%s: %s", key, summary["error"])
    return summary


def _kind_from(url: str, content_type: str) -> str:
    name = unquote(urlsplit(url).path).lower()
    for ext in ("xlsx", "xls", "csv", "zip"):
        if name.endswith("." + ext):
            return ext
    if "spreadsheetml" in (content_type or ""):
        return "xlsx"
    return "xlsx"


def harvest_all(store, entries: list[dict] | None = None, fetch=None) -> list[dict]:
    """Every registry dataset, one at a time. Sequential on purpose — the transport's throttle is
    process-wide, so parallelism here would buy nothing but a rude request rate."""
    out = []
    for entry in (config.FILE_DATASETS if entries is None else entries):
        out.append(harvest_file_dataset(entry, store, fetch))
    return out

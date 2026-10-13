"""SIPRI spreadsheet harvest: URL self-healing, byte-level idempotency, header detection.

Fully offline. The .xlsx fixtures are built here byte by byte (xlsx is a ZIP of XML and there is no
spreadsheet library in this project), and every fetch is injected, so nothing in this file touches
www.sipri.org — which matters more than usual: their server is slow and we are a guest on it.

What these tests are really defending:
  * the annual rename. SIPRI ships 'SIPRI-Milex-data-1949-2025_v1.2.xlsx' and next year the same
    dataset is at a different path. Resolution has to follow it WITHOUT drifting onto the sibling
    dataset published on the same page.
  * re-harvest cost. Unchanged bytes must not be re-parsed or re-uploaded.
  * silence. A sheet that cannot be read must say so, not emit positional junk that reads as data.
"""
from __future__ import annotations

import inspect
import os
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must precede the config import: the real 8s politeness delay would make the retry tests take
# half a minute. Same trick conftest.py uses to force the crawler offline.
os.environ["SIPRI_REQUEST_DELAY_S"] = "0"

import io  # noqa: E402

from sipri_harvester import config, datasets, discover  # noqa: E402

config.REQUEST_DELAY_S = 0.0


# ── building a real .xlsx without a spreadsheet library ──────────────────────

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_RELNS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def make_xlsx(sheets: list[tuple[str, list[tuple[int, list]]]]) -> bytes:
    """A minimal but genuine xlsx. `sheets` is [(name, [(excel_row_no, [cell, ...])])]; a None cell
    is OMITTED from the XML entirely, which is how Excel itself stores blanks and is the whole
    reason cell positions must be read from the r= attribute rather than counted."""
    shared: list[str] = []

    def sref(v: str) -> int:
        if v not in shared:
            shared.append(v)
        return shared.index(v)

    sheet_parts: list[str] = []
    for _name, rows in sheets:
        xml_rows = []
        for rowno, cells in rows:
            cs = []
            for ci, val in enumerate(cells):
                if val is None or val == "":
                    continue
                ref = f"{_col_letter(ci)}{rowno}"
                if isinstance(val, str):
                    cs.append(f'<c r="{ref}" t="s"><v>{sref(val)}</v></c>')
                else:
                    cs.append(f'<c r="{ref}"><v>{val}</v></c>')
            xml_rows.append(f'<row r="{rowno}">{"".join(cs)}</row>')
        sheet_parts.append(
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{_MAIN}"><sheetData>{"".join(xml_rows)}</sheetData></worksheet>')

    n = len(sheets)
    sheet_tags = "".join(
        f'<sheet name="{escape(name)}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
        for i, (name, _rows) in enumerate(sheets))
    rel_tags = "".join(
        f'<Relationship Id="rId{i + 1}" Type="{_RELNS}/worksheet" Target="worksheets/sheet{i + 1}.xml"/>'
        for i in range(n))
    rel_tags += f'<Relationship Id="rId{n + 1}" Type="{_RELNS}/sharedStrings" Target="sharedStrings.xml"/>'
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i + 1}.xml" ContentType="application/vnd.openxml'
        f'formats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(n))
    si = "".join(f"<si><t>{escape(s)}</t></si>" for s in shared)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
                    'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxml'
                    'formats-officedocument.spreadsheetml.sheet.main+xml"/>' + overrides + "</Types>")
        zf.writestr("_rels/.rels",
                    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    f'<Relationship Id="rId1" Type="{_RELNS}/officeDocument" Target="xl/workbook.xml"/>'
                    f'</Relationships>')
        zf.writestr("xl/workbook.xml",
                    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    f'<workbook xmlns="{_MAIN}" xmlns:r="{_RELNS}"><sheets>{sheet_tags}</sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels",
                    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    f'{rel_tags}</Relationships>')
        zf.writestr("xl/sharedStrings.xml",
                    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    f'<sst xmlns="{_MAIN}" count="{len(shared)}" uniqueCount="{len(shared)}">{si}</sst>')
        for i, part in enumerate(sheet_parts):
            zf.writestr(f"xl/worksheets/sheet{i + 1}.xml", part)
    return buf.getvalue()


# A SIPRI-shaped sheet: two banner rows, a blank, the real header (whose columns are YEARS), three
# data rows one of which is sparse, a blank, then a footnote.
MILEX_SHEET = [
    (1, ["SIPRI Military Expenditure Database 2025"]),
    (2, ["Figures are in US$ m., at constant (2023) prices and exchange rates."]),
    (4, ["Country", "Notes", 1949, 1950, 1951]),
    (5, ["Algeria", None, 1.1, 2.2, 3.3]),
    (6, ["Angola", "note a", 4, 5, 6]),
    (7, ["Benin", None, 7, None, 9]),          # sparse: D7 omitted entirely
    (9, ["Notes: .. = data unavailable."]),
]
# A cover sheet with no grid at all — must be reported as uninterpretable, not guessed at.
NOTES_SHEET = [
    (1, ["About the SIPRI Military Expenditure Database"]),
    (2, ["Contact: milex@sipri.org"]),
]

WORKBOOK = make_xlsx([("Constant (2023) US$", MILEX_SHEET), ("Notes", NOTES_SHEET)])

MILEX_LANDING = """
<html><body><div class="content">
  <h1>SIPRI Military Expenditure Database</h1>
  <p>Downloads</p>
  <ul>
    <li><a href="/sites/default/files/SIPRI-Milex-data-1949-2025_v1.2.xlsx">Data for all countries 1949-2025 (Excel)</a></li>
    <li><a href="/sites/default/files/SIPRI-Milex-regional-totals-2025.xlsx">Regional totals (Excel)</a></li>
    <li><a href="/sites/default/files/2025-04/milex_methodology.pdf">Sources and methods (PDF)</a></li>
    <li><a href="/media/press-release/2025/world-military-spending">Press release</a></li>
    <li><a href="/about">About SIPRI</a></li>
  </ul>
</div></body></html>"""

# Same page a year later: every filename has moved.
MILEX_LANDING_NEXT_YEAR = MILEX_LANDING.replace(
    "SIPRI-Milex-data-1949-2025_v1.2.xlsx", "SIPRI-Milex-data-1949-2026_v2.0.xlsx").replace(
    "SIPRI-Milex-regional-totals-2025.xlsx", "SIPRI-Milex-regional-totals-2026.xlsx")

# One landing page, two datasets — the case that collapses if resolution ignores which entry it is.
ARMSINDUSTRY_LANDING = """
<html><body><ul>
  <li><a href="/sites/default/files/SIPRI-Top-100-2003-2025.xlsx">SIPRI Top 100 (Excel)</a></li>
  <li><a href="/sites/default/files/Total-arms-revenues-SIPRI-Top-100-2003-2025.xlsx">Total arms revenues (Excel)</a></li>
</ul></body></html>"""


class FakeStore:
    """The store.py interface, with record_dataset's signature copied VERBATIM from store.py rather
    than written as a permissive **kw.

    That is deliberate. A forgiving fake is how the crawler's `outcome.ok` bug survived its tests
    (the field is `accepted`), and it is how this file's first draft passed while calling
    record_dataset(url=..., bytes=...) when the real parameters are source_url/size_bytes — a
    TypeError on every dataset in production, green tests here. If store.py's signature moves, these
    tests must break.
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.put_calls = 0
        self.record_calls = 0

    def get_dataset(self, key) -> dict | None:
        return self.rows.get(key)

    def put_blob(self, key, data, content_type) -> str | None:
        self.put_calls += 1
        self.blobs[key] = data
        return key

    def record_dataset(self, key: str, *, title: str = "", source_url: str = "",
                       landing_url: str = "", kind: str = "", sha256: str = "",
                       size_bytes: int = 0, minio_key: str = "",
                       row_count: int | None = None) -> bool:
        self.record_calls += 1
        # Keyed by the store's real COLUMN names ('bytes' is the column, size_bytes the parameter).
        self.rows[key] = {"key": key, "title": title, "source_url": source_url,
                          "landing_url": landing_url, "kind": kind, "sha256": sha256,
                          "bytes": size_bytes, "minio_key": minio_key, "row_count": row_count}
        return True


def fetch_ok(data=WORKBOOK, ctype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
    calls = []

    def _fetch(url):
        calls.append(url)
        return data, ctype

    _fetch.calls = calls
    return _fetch


def entry(**over):
    e = {"key": "milex", "title": "SIPRI Military Expenditure Database",
         "landing": "https://www.sipri.org/databases/milex",
         "url": "https://www.sipri.org/sites/default/files/SIPRI-Milex-data-1949-2025_v1.2.xlsx",
         "kind": "xlsx"}
    e.update(over)
    return e


# ── header detection ─────────────────────────────────────────────────────────

def t_header_row_is_found_below_the_banner() -> None:
    """Row 1 is a title, row 2 is a units note, the header is on row 4. Taking rows[0] would name
    the first column 'SIPRI Military Expenditure Database 2025' and leave the rest unnamed."""
    out = datasets.parse_workbook(WORKBOOK, "xlsx")
    assert out["error"] is None, out["error"]
    sheet = out["sheets"][0]
    assert sheet["header_row"] == 4, f"header detected on row {sheet['header_row']}, expected 4"
    assert sheet["columns"] == ["Country", "Notes", "1949", "1950", "1951"], sheet["columns"]
    assert sheet["rows"] == 3, f"expected 3 data rows, got {sheet['rows']}"


def t_numeric_headers_are_not_mistaken_for_data() -> None:
    """SIPRI's column labels are years, so any 'headers are text / data is numeric' rule picks the
    wrong row on every milex sheet. Guards against that rule being reintroduced."""
    sheet = datasets.parse_workbook(WORKBOOK, "xlsx")["sheets"][0]
    assert "1949" in sheet["columns"] and "Algeria" not in sheet["columns"], sheet["columns"]


def t_footnote_row_is_not_counted_as_data() -> None:
    sheet = datasets.parse_workbook(WORKBOOK, "xlsx")["sheets"][0]
    assert sheet["footnote_rows"] == 1, f"footnote handling: {sheet}"
    assert sheet["rows"] == 3, "the trailing 'Notes: ..' line leaked into the data rows"


def t_sparse_cells_keep_their_column() -> None:
    """xlsx omits empty cells, so Benin's row is stored as A7/C7/E7. A reader that counts cells
    positionally puts 9 under 1950. This is the single most common way a hand-rolled xlsx reader
    corrupts data while looking like it works."""
    with zipfile.ZipFile(io.BytesIO(WORKBOOK)) as zf:
        shared = datasets._shared_strings(zf)
        name, path = datasets._sheet_targets(zf)[0]
        sheet = datasets._read_sheet(zf, name, path, shared)
    benin = [r for r in sheet["data"] if r["Country"] == "Benin"][0]
    assert benin["1951"] == 9, f"sparse row misaligned: {benin}"
    assert benin["1950"] is None, f"a value slid into the empty column: {benin}"
    assert benin["1949"] == 7, benin


def t_uninterpretable_sheet_says_so() -> None:
    """The 'Notes' tab has no grid. It must contribute zero rows AND carry a reason — emitting
    col0/col1 rows there would put convincing nonsense in front of the next reader."""
    notes = datasets.parse_workbook(WORKBOOK, "xlsx")["sheets"][1]
    assert notes["name"] == "Notes"
    assert notes["rows"] == 0 and notes["header_row"] is None, notes
    assert notes["note"] and "no header" in notes["note"], notes


def t_rich_text_shared_strings_are_joined() -> None:
    """A shared string can be several <r> runs. Reading only the first <t> silently truncates the
    label — 'Coun' instead of 'Country'."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/sharedStrings.xml",
                    f'<sst xmlns="{_MAIN}"><si><r><t>Coun</t></r><r><t>try</t></r></si>'
                    f'<si><t>Plain</t></si></sst>')
    with zipfile.ZipFile(io.BytesIO(buf.getvalue())) as zf:
        assert datasets._shared_strings(zf) == ["Country", "Plain"]


def t_column_letters_decode() -> None:
    assert datasets._col_index("A1") == 0
    assert datasets._col_index("Z9") == 25
    assert datasets._col_index("AA1") == 26
    assert datasets._col_index("AB12") == 27
    assert datasets._col_index(None) is None


def t_duplicate_column_names_do_not_collapse() -> None:
    """Two columns called 'Notes' share a dict key; without disambiguation one column's data is
    silently lost."""
    wb = make_xlsx([("S", [(1, ["Country", "Notes", "Notes", "Year"]),
                           (2, ["Algeria", "a", "b", 1949]),
                           (3, ["Angola", "c", "d", 1950])])])
    sheet = datasets.parse_workbook(wb, "xlsx")["sheets"][0]
    assert sheet["columns"] == ["Country", "Notes", "Notes_2", "Year"], sheet["columns"]


def t_headerless_glossary_keeps_its_rows_positionally() -> None:
    """milex's real 'Footnotes' sheet, reduced. It is a two-column glossary with NO header, so the
    first definition row used to be adopted as the column names — inventing a column called after a
    footnote and eating that row. Correct behaviour is positional columns, every row kept, and a
    note saying so."""
    wb = make_xlsx([("Footnotes", [
        (1, ["Footnotes and special notes"]),
        (4, ["The following special notes are used in each worksheet"]),
        (5, ["†", "Figures for these countries do not include military pensions."]),
        (6, ["‡", "Figures for these countries are for current spending only."]),
        (7, ["§", "Figures for these countries are for the adopted budget."]),
    ])])
    sheet = datasets.parse_workbook(wb, "xlsx")["sheets"][0]
    assert sheet["header_row"] is None, f"adopted a glossary definition as the header: {sheet['columns']}"
    assert sheet["note"] and "positional" in sheet["note"], sheet["note"]
    assert sheet["columns"] == ["col0", "col1"], sheet["columns"]
    assert sheet["rows"] == 5, f"lost glossary rows: {sheet['rows']}"
    assert any(r["col0"] == "†" for r in sheet["data"]), "the first definition row was eaten"


def t_bracketed_header_is_not_mistaken_for_prose() -> None:
    """The prose guard keys on the full stop, not word count, and this is why: the real Top-100
    header 'Arms revenues 2002 (constant 2024 prices) note (d)' is EIGHT words and the footnote it
    must be distinguished from is NINE. A word-count rule breaks this sheet."""
    wb = make_xlsx([("2002", [
        (1, ["SIPRI Arms Industry Database"]),
        (4, ["Rank (2002) *Note (b)", "Company (c)", "Arms revenues 2002 (constant 2024 prices) note (d)"]),
        (5, [1, "Lockheed Martin", 26.0]),
        (6, [2, "Boeing", 24.5]),
    ])])
    sheet = datasets.parse_workbook(wb, "xlsx")["sheets"][0]
    assert sheet["header_row"] == 4, f"real header rejected as prose: {sheet}"
    assert sheet["columns"][2] == "Arms revenues 2002 (constant 2024 prices) note (d)", sheet["columns"]
    assert sheet["rows"] == 2, sheet


def t_transposed_table_keeps_its_row_labels() -> None:
    """'Total arms revenues' is transposed: years across the header from column B, leaving A blank
    above the row labels. Taking columns from the header row alone leaves column A nameless and
    drops the label saying which series each row IS — the most important field on the sheet."""
    wb = make_xlsx([("Sheet1", [
        (1, ["Total arms sales for the SIPRI Top 100"]),
        (3, [None, 2002, 2003, 2004]),
        (4, ["current US$ (billion)", 200.26, 237.89, 272.66]),
        (5, ["% change", None, 0.18, 0.14]),
        (6, ["constant 2024 US$ (billion)", 339.5, 378.58, 412.27]),
    ])])
    sheet = datasets.parse_workbook(wb, "xlsx")["sheets"][0]
    assert sheet["header_row"] == 3, sheet
    assert sheet["columns"] == ["col0", "2002", "2003", "2004"], sheet["columns"]
    assert sheet["data"][0]["col0"] == "current US$ (billion)", \
        f"row label lost from the unnamed first column: {sheet['data'][0]}"
    assert sheet["data"][0]["2002"] == 200.26, sheet["data"][0]


# ── url re-resolution ────────────────────────────────────────────────────────

def t_resolves_current_url_from_landing_html() -> None:
    got = discover.resolve_dataset_url("https://www.sipri.org/databases/milex", MILEX_LANDING,
                                       hint=entry()["url"])
    assert got == "https://www.sipri.org/sites/default/files/SIPRI-Milex-data-1949-2025_v1.2.xlsx", got


def t_resolution_follows_the_annual_rename() -> None:
    """The whole point of discovery: next year's filename is found from the same landing page,
    without drifting onto the regional-totals workbook published beside it."""
    got = discover.resolve_dataset_url("https://www.sipri.org/databases/milex",
                                       MILEX_LANDING_NEXT_YEAR, hint=entry()["url"])
    assert got.endswith("SIPRI-Milex-data-1949-2026_v2.0.xlsx"), f"self-healing failed: {got}"


def t_two_datasets_on_one_page_do_not_collide() -> None:
    """armsindustry publishes the Top-100 workbook and the total-revenues workbook on ONE page.
    'Best link on the page' would point both registry entries at the same file."""
    landing = "https://www.sipri.org/databases/armsindustry"
    top = discover.resolve_dataset_url(
        landing, ARMSINDUSTRY_LANDING,
        hint="https://www.sipri.org/sites/default/files/SIPRI-Top-100-2002-2024%20%282%29.xlsx")
    rev = discover.resolve_dataset_url(
        landing, ARMSINDUSTRY_LANDING,
        hint="https://www.sipri.org/sites/default/files/Total-arms-revenues-SIPRI-Top-100-2002-2024.xlsx")
    assert top.endswith("SIPRI-Top-100-2003-2025.xlsx"), top
    assert rev.endswith("Total-arms-revenues-SIPRI-Top-100-2003-2025.xlsx"), rev
    assert top != rev, "both registry entries resolved to the same file"


def t_non_data_links_are_ignored() -> None:
    """A PDF methodology note and an /about page are on the same list as the workbook."""
    got = discover.resolve_dataset_url("https://www.sipri.org/databases/milex", MILEX_LANDING)
    assert got.endswith(".xlsx"), got


def t_no_match_returns_none_not_a_guess() -> None:
    """None lets the caller keep the registry URL — stale but real. A fabricated path is a
    guaranteed 404 dressed up as a result."""
    html = "<html><body><a href='/about'>About</a><a href='/media'>Media</a></body></html>"
    assert discover.resolve_dataset_url("https://www.sipri.org/databases/milex", html) is None


def t_extract_links_still_drops_spreadsheets() -> None:
    """Documents WHY discover.py reuses parse.extract_pdf_links(): extract_links() filters .xlsx out
    by design (it feeds the crawl frontier). If this ever starts passing spreadsheets through, the
    comment in discover.py is stale and the candidate set should be revisited."""
    from crawler import parse
    links = parse.extract_links(MILEX_LANDING, "https://www.sipri.org/databases/milex")
    assert not any(u.endswith(".xlsx") for u in links), \
        "extract_links now returns spreadsheets — discover.py's rationale needs updating"
    attach = parse.extract_pdf_links(MILEX_LANDING, "https://www.sipri.org/databases/milex")
    assert any(u.endswith(".xlsx") for u in attach), "extract_pdf_links no longer yields xlsx"


def t_refresh_registry_falls_back_to_the_registry_url() -> None:
    """A landing page that is down must not blank out a URL that still works."""
    def dead_fetch(url):
        return None

    out = discover.refresh_registry([entry()], fetch=dead_fetch)
    assert out[0]["url"] == entry()["url"], out[0]
    assert out[0]["url_source"] == "registry" and out[0]["url_changed"] is False, out[0]


def t_refresh_registry_fetches_a_shared_landing_page_once() -> None:
    """Two entries share the armsindustry page. Fetching it twice doubles our footprint on a slow
    server for no information."""
    calls = []

    def counting_fetch(url):
        calls.append(url)
        return ARMSINDUSTRY_LANDING.encode(), "text/html"

    entries = [
        {"key": "armsindustry_top100", "landing": "https://www.sipri.org/databases/armsindustry",
         "url": "https://www.sipri.org/sites/default/files/SIPRI-Top-100-2002-2024%20%282%29.xlsx",
         "kind": "xlsx"},
        {"key": "armsindustry_revenues", "landing": "https://www.sipri.org/databases/armsindustry",
         "url": "https://www.sipri.org/sites/default/files/Total-arms-revenues-SIPRI-Top-100-2002-2024.xlsx",
         "kind": "xlsx"},
    ]
    out = discover.refresh_registry(entries, fetch=counting_fetch)
    assert len(calls) == 1, f"fetched the same landing page {len(calls)} times"
    assert out[0]["url"] != out[1]["url"], "shared landing page collapsed both entries onto one file"
    assert all(e["url_changed"] for e in out), out


def t_refresh_registry_does_not_mutate_the_registry() -> None:
    """config.FILE_DATASETS is import-time constant data; mutating it would make harvest behaviour
    depend on whether discovery happened to run first."""
    before = [dict(e) for e in config.FILE_DATASETS]
    discover.refresh_registry(config.FILE_DATASETS, fetch=lambda u: None)
    assert [dict(e) for e in config.FILE_DATASETS] == before, "refresh_registry mutated the registry"


# ── harvest: storage, idempotency, failure ───────────────────────────────────

def t_first_harvest_stores_and_parses() -> None:
    store = FakeStore()
    out = datasets.harvest_file_dataset(entry(), store, fetch=fetch_ok())
    assert out["status"] == "fetched", out
    assert out["sha256"] and out["bytes"] == len(WORKBOOK), out
    assert out["rows"] == 3, out
    assert [s["name"] for s in out["sheets"]] == ["Constant (2023) US$", "Notes"], out["sheets"]
    assert store.put_calls == 1 and store.record_calls == 1
    assert store.blobs, "raw bytes were never stored"
    assert list(store.blobs.values())[0] == WORKBOOK, "stored bytes differ from what was fetched"


def t_record_dataset_gets_the_contracted_fields() -> None:
    """The handshake with store.py: sha256, bytes, minio_key, row_count. A rename on either side
    silently degrades to a row with no checksum, which quietly disables idempotency forever."""
    store = FakeStore()
    datasets.harvest_file_dataset(entry(), store, fetch=fetch_ok())
    row = store.rows["milex"]
    for field in ("key", "sha256", "bytes", "minio_key", "row_count"):
        assert field in row, f"record_dataset missing {field}: {sorted(row)}"
    assert row["row_count"] == 3 and row["bytes"] == len(WORKBOOK)
    assert row["minio_key"].startswith("datasets/milex/"), row["minio_key"]
    assert row["source_url"] == entry()["url"] and row["landing_url"] == entry()["landing"], row


def t_store_contract_matches_the_real_store() -> None:
    """Bind the kwargs harvest_file_dataset ACTUALLY passes against store.py's REAL signature.

    store.record_dataset takes an explicit keyword-only signature, not **kw, so calling it with
    url=/bytes= instead of source_url=/size_bytes= raises TypeError on every dataset. A fake alone
    cannot catch that; binding against the real function can. Skipped, not failed, if store.py is
    absent — it belongs to another module and this suite must not depend on it existing."""
    try:
        from sipri_harvester import store as real_store
    except ModuleNotFoundError as exc:          # genuinely absent — this suite does not own it
        print(f"      (skipped — store.py not present: {exc})")
        return
    # Anything OTHER than "the module isn't there" — a SyntaxError, an AttributeError, a bad
    # import inside store.py — is exactly the breakage this test exists to catch. Swallowing it
    # under a bare `except Exception` reported the defect as a pass.

    captured: dict = {}

    class Capturing(FakeStore):
        def record_dataset(self, key, **kw):    # permissive HERE on purpose: capture, then verify
            captured.update(kw, key=key)
            return True

    out = datasets.harvest_file_dataset(entry(), Capturing(), fetch=fetch_ok())
    assert out["status"] == "fetched", out
    # Raises TypeError if any kwarg is misnamed, missing or surplus.
    inspect.signature(real_store.record_dataset).bind(**captured)
    inspect.signature(real_store.put_blob).bind("some/key", b"bytes", "text/csv")
    inspect.signature(real_store.get_dataset).bind("milex")
    assert "sha256" in real_store._DATASET_COLS, \
        "the idempotency check reads row['sha256'] — that column is gone"


def t_unstored_blob_is_never_recorded() -> None:
    """store.put_blob() returns None when the object did NOT land. Recording a sha256 anyway makes
    every later run report 'unchanged' for a file that was never stored — permanently. store.py's
    own docstring calls this out; this is the test that keeps us honouring it."""
    class NoBlobStore(FakeStore):
        def put_blob(self, key, data, content_type):
            self.put_calls += 1
            return None                         # e.g. MinIO unreachable

    store = NoBlobStore()
    out = datasets.harvest_file_dataset(entry(), store, fetch=fetch_ok())
    assert out["status"] == "failed", out
    assert store.record_calls == 0, "wrote a registry row for bytes that were never stored"
    assert store.get_dataset("milex") is None, "a later run would now skip this dataset forever"


def t_failed_registry_write_is_reported() -> None:
    class RefusingStore(FakeStore):
        def record_dataset(self, key, **kw):
            return False                        # documented: False means the row did not land

    out = datasets.harvest_file_dataset(entry(), RefusingStore(), fetch=fetch_ok())
    assert out["status"] == "failed" and "did not land" in out["error"], out


def t_unchanged_second_harvest_skips_parsing() -> None:
    """Re-harvest must be cheap: identical bytes mean no XML parse, no upload, no DB write."""
    store = FakeStore()
    first = datasets.harvest_file_dataset(entry(), store, fetch=fetch_ok())
    assert first["status"] == "fetched"

    parses = []
    real_parse = datasets.parse_workbook
    datasets.parse_workbook = lambda data, kind="xlsx": (parses.append(kind), real_parse(data, kind))[1]
    try:
        second = datasets.harvest_file_dataset(entry(), store, fetch=fetch_ok())
    finally:
        datasets.parse_workbook = real_parse

    assert second["status"] == "unchanged", second
    assert second["sha256"] == first["sha256"]
    assert parses == [], "re-parsed a file whose bytes had not changed"
    assert store.put_calls == 1, f"re-uploaded unchanged bytes ({store.put_calls} puts)"
    assert store.record_calls == 1, "rewrote the row for unchanged bytes"


def t_changed_bytes_are_re_harvested() -> None:
    """The other half of idempotency — a real update must NOT be skipped."""
    store = FakeStore()
    datasets.harvest_file_dataset(entry(), store, fetch=fetch_ok())
    updated = make_xlsx([("Constant (2023) US$", MILEX_SHEET + [(8, ["Chad", None, 1, 2, 3])])])
    out = datasets.harvest_file_dataset(entry(), store, fetch=fetch_ok(updated))
    assert out["status"] == "fetched", out
    assert out["rows"] == 4, out
    assert store.put_calls == 2 and len(store.blobs) == 2, \
        "content-addressed keys should keep both versions, not overwrite"


def t_download_failure_is_reported_not_raised() -> None:
    """A harvest run covers several datasets; one 404 must not take the other three with it."""
    def boom(url):
        raise ConnectionError("connection reset by peer")

    store = FakeStore()
    out = datasets.harvest_file_dataset(entry(), store, fetch=boom)
    assert out["status"] == "failed", out
    assert out["error"] and "connection reset" in out["error"], out["error"]
    assert out["key"] == "milex" and out["rows"] == 0
    assert store.put_calls == 0 and store.record_calls == 0, "stored something despite failing"


def t_download_is_retried_then_gives_up() -> None:
    attempts = []

    def flaky(url):
        attempts.append(url)
        if len(attempts) < 2:
            return None                 # empty response, as fetcher.get returns on a non-200
        return WORKBOOK, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    out = datasets.harvest_file_dataset(entry(), FakeStore(), fetch=flaky)
    assert out["status"] == "fetched", out
    assert len(attempts) == 2, f"expected a retry, got {len(attempts)} attempts"

    dead = []

    def never(url):
        dead.append(url)
        return None

    out = datasets.harvest_file_dataset(entry(), FakeStore(), fetch=never)
    assert out["status"] == "failed", out
    assert len(dead) == config.MAX_RETRIES, f"unbounded retry: {len(dead)} attempts"


def t_html_error_page_is_not_stored_as_a_workbook() -> None:
    """SIPRI can answer 200 with a WAF challenge or a Drupal 404. The URL still ends .xlsx, so
    without a magic check the bucket fills with 'Access Denied' objects that will not open."""
    store = FakeStore()
    out = datasets.harvest_file_dataset(
        entry(), store, fetch=fetch_ok(b"<html><body>Access Denied</body></html>", "text/html"))
    assert out["status"] == "failed", out
    assert out["error"] and "ZIP" in out["error"], out["error"]
    assert store.put_calls == 0, "stored an HTML error page as a dataset blob"


def t_corrupt_zip_is_reported_not_raised() -> None:
    out = datasets.parse_workbook(b"PK\x03\x04garbage that is not a real zip", "xlsx")
    assert out["rows"] == 0 and out["error"], out
    assert out["sheets"] == []


def t_store_failure_is_reported() -> None:
    class BadStore(FakeStore):
        def put_blob(self, key, data, content_type):
            raise OSError("minio unreachable")

    out = datasets.harvest_file_dataset(entry(), BadStore(), fetch=fetch_ok())
    assert out["status"] == "failed" and "minio unreachable" in out["error"], out


def t_unreadable_store_does_not_skip_a_real_update() -> None:
    """If get_dataset() blows up we must re-harvest, not assume 'unchanged' and skip the update."""
    class FlakyStore(FakeStore):
        def get_dataset(self, key):
            raise RuntimeError("pg down")

    store = FlakyStore()
    out = datasets.harvest_file_dataset(entry(), store, fetch=fetch_ok())
    assert out["status"] == "fetched", out
    assert store.put_calls == 1


def t_csv_dataset_parses() -> None:
    body = b"SIPRI arms transfers\n\nCountry,Year,TIV\nIndia,2024,3000\nChina,2024,1200\n"
    out = datasets.parse_workbook(body, "csv")
    assert out["rows"] == 2, out
    assert out["sheets"][0]["columns"] == ["Country", "Year", "TIV"], out["sheets"][0]


def t_summary_shape_is_stable() -> None:
    """The caller (transfers.py / the dashboard) reads these keys."""
    out = datasets.harvest_file_dataset(entry(), FakeStore(), fetch=fetch_ok())
    for field in ("key", "status", "bytes", "sha256", "sheets", "rows", "error"):
        assert field in out, f"summary missing {field}: {sorted(out)}"
    assert out["status"] in ("fetched", "unchanged", "failed")


def main() -> None:
    for fn in (t_header_row_is_found_below_the_banner, t_numeric_headers_are_not_mistaken_for_data,
               t_footnote_row_is_not_counted_as_data, t_sparse_cells_keep_their_column,
               t_uninterpretable_sheet_says_so, t_rich_text_shared_strings_are_joined,
               t_column_letters_decode, t_duplicate_column_names_do_not_collapse,
               t_headerless_glossary_keeps_its_rows_positionally,
               t_bracketed_header_is_not_mistaken_for_prose,
               t_transposed_table_keeps_its_row_labels,
               t_resolves_current_url_from_landing_html, t_resolution_follows_the_annual_rename,
               t_two_datasets_on_one_page_do_not_collide, t_non_data_links_are_ignored,
               t_no_match_returns_none_not_a_guess, t_extract_links_still_drops_spreadsheets,
               t_refresh_registry_falls_back_to_the_registry_url,
               t_refresh_registry_fetches_a_shared_landing_page_once,
               t_refresh_registry_does_not_mutate_the_registry,
               t_first_harvest_stores_and_parses, t_record_dataset_gets_the_contracted_fields,
               t_store_contract_matches_the_real_store, t_unstored_blob_is_never_recorded,
               t_failed_registry_write_is_reported,
               t_unchanged_second_harvest_skips_parsing, t_changed_bytes_are_re_harvested,
               t_download_failure_is_reported_not_raised, t_download_is_retried_then_gives_up,
               t_html_error_page_is_not_stored_as_a_workbook, t_corrupt_zip_is_reported_not_raised,
               t_store_failure_is_reported, t_unreadable_store_does_not_skip_a_real_update,
               t_csv_dataset_parses, t_summary_shape_is_stable):
        fn()
        print(f"  ok  {fn.__name__}")
    print("ALL SIPRI DATASET TESTS PASSED")



def test_sipri_datasets_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()

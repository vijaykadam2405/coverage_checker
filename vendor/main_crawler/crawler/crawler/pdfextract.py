"""PDF handling — fetch a PDF and extract its plain text (§4).

PDF-capture jobs fetch + extract PDFs; the extracted text lands in
``attachments[].extracted_text`` as raw material for Layer 2. Never
fabricate: if extraction yields nothing, the attachment is recorded with the
storage path but ``extracted_text=None``. Table/spec structuring (beyond
plain text) is Layer 2's job, not raw harvest.
"""
from __future__ import annotations

import io
import os

from . import storage
from .fetcher import Fetcher
from .models import Attachment


def extract_text(pdf_bytes: bytes, max_chars: int = 200_000) -> str | None:
    # max_chars bounds how much raw text is retained at all (extraction-time
    # safety cap).
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts: list[str] = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(p.strip() for p in parts if p.strip())
        text = text.replace("\x00", "").strip()
        return text[:max_chars] or None
    except Exception:
        return None


def extract_tables(pdf_bytes: bytes, max_tables: int = 20, max_pages: int = 12) -> list[dict]:
    """Structured tables from a (spec-heavy tender) PDF via pdfplumber → list of
    {title?, rows:[{col:val}]} dicts ready for the Document.tables `Table` model.
    pypdf's flat text collapses columns; this preserves the spec grid. Empty list if
    pdfplumber isn't installed or the PDF has no extractable tables.

    Bounded to the first ``max_pages`` pages: pdfplumber/pdfminer is pure-Python and holds the GIL,
    so an unbounded scan of a big/complex PDF freezes the whole async crawl on one core."""
    try:
        import pdfplumber
    except Exception:
        return []
    out: list[dict] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages[:max_pages]:
                for tbl in (page.extract_tables() or []):
                    if not tbl or len(tbl) < 2:
                        continue
                    header = [(c or "").strip() or f"col{i}" for i, c in enumerate(tbl[0])]
                    rows = []
                    for r in tbl[1:]:
                        rows.append({header[i]: (r[i] or "").strip()
                                     for i in range(min(len(header), len(r)))})
                    if rows:
                        out.append({"rows": rows})
                    if len(out) >= max_tables:
                        return out
    except Exception:
        return out
    return out


def _looks_like_pdf(data: bytes) -> bool:
    """True only if the bytes are actually a PDF. A .pdf link that returned an HTML page
    (redirect / login wall / 404) is classified 'pdf' by URL extension and reaches here with
    HTML bytes — storing that as a .pdf gives a broken attachment the viewer can't open."""
    return b"%PDF" in data[:1024]


def fetch_attachments_and_tables(pdf_urls: list[str], fetcher: Fetcher,
                                 referer: str | None = None,
                                 cookies: list | None = None) -> tuple[list[Attachment], list[dict]]:
    """Download several PDFs concurrently and store each. Returns (attachments, tables).
    Bytes that aren't actually a PDF (a .pdf URL that served HTML) are dropped — only real PDFs
    become attachments. ``referer``/``cookies`` feed the 403 bypass ladder for WAF-protected hosts.

    By DEFAULT this only FETCHES + STORES the raw PDF (extracted_text=None). Content extraction
    (pypdf text, pdfplumber tables) is CPU-heavy and holds the GIL — a single big PDF freezes the
    whole async crawl on one core — and is Layer 2's job anyway (it can read the stored bytes).
    Opt in per deployment: CRAWLER_PDF_EXTRACT=1 (plain text), CRAWLER_PDF_TABLES=1 (tables, bounded)."""
    extract_on = os.environ.get("CRAWLER_PDF_EXTRACT", "0") == "1"
    tables_on = os.environ.get("CRAWLER_PDF_TABLES", "0") == "1"
    table_max_bytes = int(os.environ.get("CRAWLER_PDF_TABLE_MAX_BYTES", str(4 * 1024 * 1024)))
    results = fetcher.fetch_assets(pdf_urls, referer=referer, cookies=cookies)
    atts: list[Attachment] = []
    tables: list[dict] = []
    for res in results:
        if res.error or not res.body_bytes or not _looks_like_pdf(res.body_bytes):
            continue
        storage_path = storage.put(res.body_bytes, kind="doc", ext="pdf")
        atts.append(Attachment(url=res.url, storage_path=storage_path, type="pdf",
                               extracted_text=extract_text(res.body_bytes) if extract_on else None))
        if extract_on and tables_on and len(res.body_bytes) <= table_max_bytes:
            tables.extend(extract_tables(res.body_bytes))
    return atts, tables

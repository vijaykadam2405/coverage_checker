"""Stage 3 — EXTRACT. Turn a kept page into one ``document`` bundle (§3).

``build_document`` assembles the source object (clean main_text, content_hash,
images, PDF attachments, screenshot, tables, time signals). It does NOT resolve
entities or construct separately-typed records; all deep processing (entity
resolution, record classification into tender/partnership/geo_footprint/
innovation/company_event/competitive_signal) is Layer 2's job, operating on the
raw text handed over here.
"""
from __future__ import annotations

import uuid

from . import pdfextract, textextract
from .canonicalize import registered_domain
from .extractutil import parse_date
from .fetcher import Fetcher
from .harvest import HarvestedPage
from .models import Document, Job, version_id
from .seed import Seed


def _reject_future(iso):
    """None for a publication date that has not happened yet.

    One day of slack: a page published "today" in a timezone ahead of UTC is not
    a misparse. Anything unparseable is left alone rather than dropped.
    """
    if not iso:
        return None
    from datetime import date, timedelta
    try:
        y, m, d = int(iso[0:4]), int(iso[5:7]), int(iso[8:10])
        got = date(y, m, d)
    except (ValueError, IndexError):
        return iso
    return iso if got <= date.today() + timedelta(days=1) else None


def _strip_nav_footer(text: str, max_menu_words: int = 4) -> str:
    """Drop leading/trailing runs of short, menu-shaped lines (e.g. 'Home |
    About | Products | Contact') from plain rendered body text. Only trims
    the outer envelope — never the middle — so dynamically-revealed content
    (the reason this path bypasses trafilatura in the first place) is safe."""
    lines = text.splitlines()

    def _is_menu_line(line: str) -> bool:
        stripped = line.strip()
        return bool(stripped) and len(stripped.split()) <= max_menu_words

    start = 0
    while start < len(lines) and _is_menu_line(lines[start]):
        start += 1
    end = len(lines)
    while end > start and _is_menu_line(lines[end - 1]):
        end -= 1
    return "\n".join(lines[start:end]).strip()


# --- document assembly ---------------------------------------------------
def build_document(job: Job, page: HarvestedPage, seed: Seed,
                   fetcher: Fetcher, enrich: bool = True,
                   crawl_run_id: str | None = None,
                   static_text: bool = False) -> Document | None:
    """Assemble the document. With ``enrich=False`` it stops after text +
    metadata (cheap) so the gate can decide before we spend effort on
    images/PDF/screenshot; call ``enrich_assets`` afterwards on kept docs."""
    res = page.fetch
    html = res.text_html or ""
    is_pdf_page = res.kind == "pdf"

    if is_pdf_page:
        body_text = pdfextract.extract_text(res.body_bytes or b"") or ""
        title = textextract.normalize_for_hash(body_text)[:80] or res.url
        meta = {"published_raw": res.published_hint, "lang": None}
    else:
        # When interactions ran, use inner_text directly — trafilatura's
        # boilerplate filter would otherwise drop dynamically-revealed content.
        # static_text=True forces the RAW-HTML extraction even when a rendered inner_text is
        # available. It is the duplicate-recovery path, not a general option: see the caller in
        # async_engine, which reaches for it only when the rendered text hashed to a page we
        # already have. Measured 2026-08-18 on serviziocontrattipubblici.it — nine distinct
        # URLs whose rendered inner_text collapsed to ONE hash (8 dropped as duplicates, 1
        # kept), while the same pages extracted from raw HTML gave 1943 / 1036 / 786 chars
        # and three different hashes. The rendered text was the shared shell; the per-page
        # content was in the markup all along.
        if res.inner_text and not static_text:
            body_text = _strip_nav_footer(textextract._safe(res.inner_text.strip()))
        else:
            body_text = textextract.main_text(html)
        from . import parse
        title = parse.title_of(html) or res.title or res.url
        meta = parse.extract_meta(html, res.url)

    if not body_text.strip() and not is_pdf_page:
        return None  # no usable main_text -> nothing to emit (acceptance rule 1)
    # A PDF page with no text layer (scanned / graphic brochure) is still KEPT — the value is the
    # stored file itself (fetch-only two-pass), which L2 can OCR/parse later.

    # Dedup key: normally the text hash; for a text-less PDF use the file bytes so distinct scanned
    # PDFs don't all collapse to the same empty-text hash.
    if is_pdf_page and not body_text.strip():
        import hashlib
        chash = "sha256:" + hashlib.sha256(res.body_bytes or b"").hexdigest()
    else:
        chash = textextract.content_hash(body_text)

    # L1 is PURE CAPTURE: no source classification, translation, summary, tables, language detection,
    # or date-precision — L2 does all analysis on the raw main_text/html we dump. We keep only cheap
    # provenance: the raw <html lang> hint, the page's own published date if its meta declares one,
    # and the bare registered domain as source_id (L2 re-resolves the real source).
    lang = meta.get("lang") or "und"
    published_iso, _ = parse_date(meta.get("published_raw") or res.published_hint)
    # A publication date in the future is a misparse, not news. The check in
    # parse.py only sees the RAW string and can therefore only sanity-check the
    # ISO-leading ones; "30 December 2026" reaches parse_date unexamined and
    # comes back as a future ISO date. This is the one place the value is always
    # ISO, so it is the only place the check is complete.
    published_iso = _reject_future(published_iso)

    doc = Document(
        url=res.url,
        content_hash=chash or "sha256:empty",
        fetched_at=res.fetched_at,
        source_id=registered_domain(res.url) or res.url,
        title=(title or res.url)[:500],
        published_at=published_iso,
        language=lang,
        main_text=body_text,
        html=html,
        depth=page.depth,
        parent_url=page.parent_url,
        served_by=page.served_by or "c1_live",
        served_from=page.served_from,
        archived_at=getattr(page, "archived_at", None),
        # Deterministic on (url, content): an unchanged re-crawl upserts in place, a changed page
        # mints a new id and is kept as a new version. See models.version_id.
        document_id=version_id(res.url, chash or "sha256:empty"),
        crawl_run_id=crawl_run_id,
    )

    if enrich:
        enrich_assets(job, doc, page, fetcher)
    return doc


def pdf_budget(job: Job) -> int:
    """How many PDF links this job may take from ONE page. **<= 0 means UNLIMITED** — capture the
    whole table of RFP PDFs. Per-job override wins (tender/archive listings need EVERY Download
    link, not just 3); else the env default.

    Callers MUST honour the <=0 convention explicitly (`links if n <= 0 else links[:n]`). Do NOT
    write `links[:n or None]` — that looks equivalent but silently drops the LAST link for any
    negative n, and max_pdfs_per_page is Optional[int] with no ge=0, so -1 is reachable.
    """
    import os
    return (job.max_pdfs_per_page if job.max_pdfs_per_page is not None
            else int(os.environ.get("CRAWLER_MAX_PDFS_PER_PAGE", "3")))


def enrich_assets(job: Job, doc: Document, page: HarvestedPage, fetcher: Fetcher) -> None:
    """Download + store the page's PDF attachments (the only asset L1 captures — text/html/pdf only).
    Called after the gate so dropped pages cost nothing here. Images/screenshots/media were removed:
    L1 dumps TEXT + HTML + PDF only; L2 does everything else."""
    res = page.fetch
    is_pdf_page = res.kind == "pdf"
    from . import storage
    from .models import Attachment

    # Referer + the render's cookies let a WAF-protected asset host (Akamai) see the same
    # session that loaded the page — replayed on the 403 bypass ladder (curl_cffi/CamoFox).
    referer = res.final_url or res.url
    cookies = res.cookies

    # PDF attachments (tender RFPs / primary docs) — downloaded concurrently, bytes stored to MinIO.
    # L1 only fetches + stores the file; L2 parses text/tables from the bytes.
    if "pdf" in job.capture and page.pdf_links:
        max_pdfs = pdf_budget(job)
        links = page.pdf_links if max_pdfs <= 0 else page.pdf_links[:max_pdfs]
        atts, _pdf_tables = pdfextract.fetch_attachments_and_tables(
            links, fetcher, referer=referer, cookies=cookies)
        doc.attachments.extend(atts)
    if is_pdf_page:  # the page itself is the PDF
        sp = storage.put(res.body_bytes, kind="doc", ext="pdf")
        doc.attachments.append(Attachment(url=res.url, storage_path=sp, type="pdf",
                                          extracted_text=doc.main_text))



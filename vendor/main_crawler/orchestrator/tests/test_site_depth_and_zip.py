"""Per-site depth drill-down and zip download, both PROXIED from ingest.

Ingest owns the documents; the console only carries the buttons. The two things worth pinning are
that the zip STREAMS (safran-group.com alone is 6.4 GB of HTML — buffering it would OOM the console
every operator uses) and that the depth view is a GROUP BY, not the load-everything index the
ingest dashboard's own site->depth view uses.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from mallory_orchestrator import api

SRC = Path(api.__file__).read_text(encoding="utf-8")
HTML = (Path(api.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


def test_both_routes_are_registered():
    paths = {r.path for r in api.router.routes}
    assert "/api/data/site/{domain}/depth" in paths
    assert "/api/data/site/{domain}/download" in paths


def test_depth_is_proxied_not_reimplemented():
    """The orchestrator must not grow its own copy of the documents query."""
    body = SRC.split("def data_site_depth")[1].split("@router")[0]
    assert "_ingest_get" in body
    assert "psycopg" not in body and "SELECT" not in body.upper()


def test_the_zip_is_streamed():
    body = SRC.split("def data_site_download")[1].split("@router")[0]
    assert "StreamingResponse" in body
    assert "c.stream(" in body and "iter_bytes" in body
    assert "r.content" not in body, "reading the whole zip into memory would OOM the console"


def test_the_zip_read_timeout_is_generous():
    """The archive is BUILT on demand — first bytes wait for Postgres to be walked. A default 5s
    read timeout would abort every site large enough to be worth downloading."""
    body = SRC.split("def data_site_download")[1].split("@router")[0]
    m = re.search(r"read=(\d+(?:\.\d+)?)", body)
    assert m and float(m.group(1)) >= 600, "read timeout too short for a large site"


def test_www_is_stripped_on_both():
    for fn in ("data_site_depth", "data_site_download"):
        assert 'removeprefix("www.")' in SRC.split("def %s" % fn)[1].split("@router")[0]


def test_an_empty_domain_is_refused():
    for fn in ("data_site_depth", "data_site_download"):
        assert "HTTPException(400" in SRC.split("def %s" % fn)[1].split("@router")[0]


def test_ingest_being_down_does_not_500_the_panel():
    """The depth dialog sits on the main console; an ingest hiccup must not take the page with it."""
    body = SRC.split("def data_site_depth")[1].split("@router")[0]
    assert "except Exception" in body and '"depths": []' in body


# ── the dashboard side ────────────────────────────────────────────────────────────────────────

def test_the_site_name_opens_the_depth_view():
    assert 'data-depth="${esc(r.source_id||"")}"' in HTML
    assert "function showDepth" in HTML


def test_the_zip_link_is_on_the_row():
    assert "/download" in HTML and "encodeURIComponent(r.source_id" in HTML


def test_the_depth_handler_is_DELEGATED():
    """renderPerSite() replaces the tbody on every poll, so a per-row listener would be discarded
    seconds after it was attached — the same reason the coverage button is delegated."""
    assert 'closest("[data-depth]")' in HTML


def test_sample_urls_in_the_dialog_keep_the_scheme_guard():
    """Those URLs came off crawled pages like every other one on this console."""
    assert "const urlLink" in HTML and "safeHref(u)" in HTML


def test_the_script_block_still_parses():
    import os, shutil, subprocess, tempfile
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    blocks = re.findall(r"<script>([\s\S]*?)</script>", HTML)
    assert len(blocks) == 1
    fd, path = tempfile.mkstemp(suffix=".js"); os.close(fd)
    Path(path).write_text(blocks[0], encoding="utf-8")
    try:
        r = subprocess.run([node, "--check", path], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[:400]
    finally:
        os.unlink(path)


# ── viewing the actual content: html / text / pdf ─────────────────────────────────────────────

def test_the_three_view_routes_exist():
    paths = {r.path for r in api.router.routes}
    for p in ("/api/data/doc/{document_id}/text", "/api/data/doc/{document_id}/html",
              "/api/data/doc/{document_id}/files", "/api/data/artifact"):
        assert p in paths, p


def test_html_is_served_as_PLAIN_TEXT():
    """It is a third-party page. Rendering it as HTML inside this console's origin would run
    someone else's scripts with the operator's session — and this console can stop crawls and
    delete the corpus. The point is to READ the markup."""
    body = SRC.split("def data_doc_html")[1].split("@router")[0]
    assert 'media_type="text/plain' in body
    assert "HTMLResponse" not in body


def test_artifacts_stream():
    """A stored PDF can be large; buffering it in the console to hand it on is the same mistake as
    buffering the site zip."""
    body = SRC.split("def data_artifact")[1].split("@router")[0]
    assert "StreamingResponse" in body and "iter_bytes" in body


def test_an_empty_artifact_path_is_refused():
    assert "HTTPException(400" in SRC.split("def data_artifact")[1].split("@router")[0]


def test_samples_carry_a_document_id_for_the_links():
    """Without document_id on each sample the view links cannot be built at all."""
    from pathlib import Path
    pg = Path(api.__file__).parents[3] / "crawler" / "ingest_api" / "pgstore.py"
    if not pg.exists():                       # orchestrator deployed without the crawler tree
        pytest.skip("crawler source not present")
    src = pg.read_text(encoding="utf-8")
    fn = src.split("def depth_breakdown")[1].split("\ndef ")[0]
    assert '"document_id": did' in fn and '"pdfs"' in fn


def test_the_sample_is_deduped_by_url():
    """mhi.com showed https://mhi.com/ twice at d0 — two revisions of one page. The count above it
    is already exact, so a duplicated EXAMPLE is just noise in the one place meant to be readable."""
    from pathlib import Path
    pg = Path(api.__file__).parents[3] / "crawler" / "ingest_api" / "pgstore.py"
    if not pg.exists():
        pytest.skip("crawler source not present")
    fn = pg.read_text(encoding="utf-8").split("def depth_breakdown")[1].split("\ndef ")[0]
    assert "seen" in fn and "if u in seen" in fn


def test_the_pdf_list_is_fetched_on_demand():
    """Most pages have no attachments; inlining the list into every sample row would be a
    per-document read for nothing."""
    assert 'closest("[data-files]")' in HTML
    assert "/files`" in HTML


def test_the_view_links_are_rendered():
    for frag in ("/text\" target=\"_blank\"", "/html\" target=\"_blank\"", "data-files="):
        assert frag in HTML, frag

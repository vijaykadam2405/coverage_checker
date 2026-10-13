"""Per-site export bundle for the :9090 dashboard.

One click on a site row → one ``<host>.zip`` holding everything we captured for that site:

  pages/NNNN.html + NNNN.txt   raw html and extracted text of every page (open them directly)
  pdf/doc/<sha>.pdf            the page attachments = the exact MinIO objects for this site
  documents.sql               a SELF-CONTAINED Postgres dump (schema + COPY of every row) — run
                              ``psql -f documents.sql`` and the recipient has the same table
  manifest.json               url -> which html/txt/pdf files belong to which page
  README.txt                  the two ways to use it (just look, or spin up in Postgres)

Streams row-by-row from Postgres via a server-side cursor and spools the COPY data to a temp file on
DISK, so a 1000-page site never holds all its html in RAM at once. Requires DATABASE_URL (it is, by
definition, a Postgres dump).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone

from crawler import config, storage

from . import pgstore

_PREFIX = config.STORAGE_URI_PREFIX + "/"


def _safe(host: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", host)[:60] or "site"


def _copy_field(v) -> str:
    """One Python value → a Postgres COPY (text format) field. NULL, then the four required
    backslash escapes; JSONB columns come back as list/dict → serialise to json text first."""
    if v is None:
        return r"\N"
    if isinstance(v, (list, dict)):
        v = json.dumps(v, ensure_ascii=False)
    else:
        v = str(v)
    return (v.replace("\\", "\\\\").replace("\n", "\\n")
             .replace("\r", "\\r").replace("\t", "\\t"))


def build_site_zip(host: str) -> tuple[str, int, int]:
    """Build ``<host>.zip`` in a temp file. Returns (zip_path, n_pages, n_pdfs). Caller unlinks the
    zip after streaming it (and 404s / unlinks when n_pages == 0)."""
    zip_fd, zip_path = tempfile.mkstemp(prefix=f"mallory_{_safe(host)}_", suffix=".zip")
    os.close(zip_fd)
    sql_fd, sql_path = tempfile.mkstemp(prefix="mallory_copy_", suffix=".tsv")
    os.close(sql_fd)

    n_pages = n_pdf = 0
    cols: list[str] | None = None
    manifest: list[dict] = []
    seen_blobs: set[str] = set()

    zf = zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED)
    try:
        # ONE streaming pass: html/txt/pdf go straight into the zip as complete entries; the COPY
        # data (which includes the heavy html) spools to a temp tsv, never the whole site in RAM.
        with open(sql_path, "w", encoding="utf-8", newline="") as sqldata:
            for row in pgstore.iter_source_rows(host):
                if cols is None:
                    cols = list(row.keys())
                n_pages += 1
                n = f"{n_pages:04d}"

                html_val = row.get("html") or ""
                text_val = row.get("main_text") or ""
                url = row.get("url") or ""
                entry = {"n": n, "url": url, "title": row.get("title") or "",
                         "depth": row.get("depth"), "served_by": row.get("served_by"),
                         "chars": len(text_val), "html_bytes": len(html_val), "pdf": []}

                if html_val:
                    zf.writestr(f"pages/{n}.html", html_val)
                    entry["html"] = f"pages/{n}.html"
                if text_val:
                    hdr = f"# {url}\n# {row.get('title') or ''}\n# {row.get('fetched_at') or ''}\n\n"
                    zf.writestr(f"pages/{n}.txt", hdr + text_val)
                    entry["text"] = f"pages/{n}.txt"

                for att in (row.get("attachments") or []):
                    sp = att.get("storage_path")
                    if not sp:
                        continue
                    rel = sp[len(_PREFIX):] if sp.startswith(_PREFIX) else sp.split("://", 1)[-1]
                    if ".." in rel:                        # never write outside the pdf/ prefix
                        continue
                    arc = f"pdf/{rel}"
                    entry["pdf"].append(arc)
                    if rel not in seen_blobs:              # same pdf can attach to many pages — store once
                        blob = storage.get(sp)
                        if blob is not None:
                            zf.writestr(arc, blob)
                            seen_blobs.add(rel)
                            n_pdf += 1

                sqldata.write("\t".join(_copy_field(row[c]) for c in cols) + "\n")
                manifest.append(entry)

        # documents.sql = schema + a COPY block, streamed from the spooled tsv (no re-buffering).
        if cols:
            with zf.open("documents.sql", "w") as out:
                out.write(("-- Mallory Layer-1 export — site: %s\n"
                           "-- %d pages, %d attachment blobs\n"
                           "-- Load:  psql \"postgres://user:pass@host/db\" -f documents.sql\n\n"
                           % (host, n_pages, n_pdf)).encode("utf-8"))
                out.write(pgstore._SCHEMA.encode("utf-8"))
                out.write(("\nCOPY documents (%s) FROM stdin;\n" % ", ".join(cols)).encode("utf-8"))
                with open(sql_path, "rb") as f:
                    shutil.copyfileobj(f, out)
                out.write(b"\\.\n")

        zf.writestr("manifest.json", json.dumps(
            {"host": host,
             "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "pages": n_pages, "pdfs": n_pdf, "documents": manifest},
            ensure_ascii=False, indent=1))
        zf.writestr("README.txt", _README % (host, n_pages, n_pdf))
    finally:
        zf.close()
        try:
            os.unlink(sql_path)
        except OSError:
            pass
    return zip_path, n_pages, n_pdf


_README = """Mallory Layer-1 export — %s
=====================================
%d pages, %d attachment blobs (PDFs).

WHAT'S HERE
  pages/NNNN.html    raw captured HTML of each page
  pages/NNNN.txt     extracted clean text (url / title / date header on top)
  pdf/doc/<sha>.pdf  the page attachments = the exact MinIO objects for this site
  documents.sql      self-contained Postgres dump (schema + COPY of every row)
  manifest.json      url -> files map (which html/txt/pdf belong to which page)

JUST LOOK AT IT
  Open the pages/*.html or *.txt and the pdf/ files directly. No database needed.

SPIN IT UP IN YOUR OWN POSTGRES
  createdb mallory
  psql "postgres://user:pass@localhost/mallory" -f documents.sql
  # one row per url in table `documents`; read the best copy via the `documents_current` view.

RESTORE THE BLOBS TO MINIO (optional — only if you serve the PDFs from object storage)
  the files under pdf/ keep their object key (doc/<sha>.pdf under bucket `mallory-raw`):
  mc mirror pdf/ myminio/mallory-raw/
"""


def _demo() -> None:
    # ponytail: covers the fiddly bit (COPY text escaping); the zip/stream path needs a live DB.
    assert _copy_field(None) == "\\N"
    assert _copy_field(42) == "42"
    assert _copy_field("a\tb\nc\\d\re") == "a\\tb\\nc\\\\d\\re"
    assert _copy_field([{"storage_path": "s3://x"}]) == '[{"storage_path": "s3://x"}]'
    print("ok")


if __name__ == "__main__":
    _demo()

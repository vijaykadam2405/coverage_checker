"""Reference data + the browsable databases — everything SIPRI exposes that isn't a trade row.

Three sources, three shapes:

  countries   — /countries/getAllCountriesTrimmed, a flat JSON list. Small, but it is the key that
                makes the trade rows joinable (buyer/seller arrive as display names, not codes).
  armaments   — /armaments/search/, the weapon catalogue behind the `desg`/`desc` fields.
  pages       — embargoes / national reports / budget archive / peace operations have neither an API
                nor a published spreadsheet: they are browsable Drupal pages. The page itself is the
                artifact, so it lands in MinIO with a row in sipri.documents rather than being
                forced into a typed table it does not fit.

All of it goes through the same throttled transport, so C3 carries it too.
"""
from __future__ import annotations

import hashlib
import logging

from . import config
from .fetcher import get, post_json

log = logging.getLogger("sipri.reference")


def _get_json(path: str) -> list | None:
    """GET a public JSON endpoint. These reference routes are GET-only — POSTing them returns 405,
    the same trap /trades/getTotal set."""
    import json as _json
    r = get(f"{config.API_BASE}{path}", headers=config.API_HEADERS)
    if not r:
        return None
    try:
        out = _json.loads(r[0].decode("utf-8", "replace"))
    except Exception:                                    # noqa: BLE001
        return None
    return out if isinstance(out, list) else None


def harvest_countries(store) -> dict:
    """The country list — small, and what makes buyer/seller joinable."""
    rows = _get_json(config.COUNTRIES_ALL)
    if rows is None:
        return {"dataset": "countries", "status": "failed", "records": 0}

    import json as _json
    blob = _json.dumps(rows, ensure_ascii=False).encode("utf-8")
    sha = hashlib.sha256(blob).hexdigest()
    key = store.put_blob(f"reference/countries/{sha[:12]}-countries.json", blob, "application/json")
    ok = store.record_dataset("countries", title="SIPRI country list",
                              source_url=f"{config.API_BASE}{config.COUNTRIES_ALL}",
                              landing_url=config.SPA_ORIGIN, kind="json", sha256=sha,
                              size_bytes=len(blob), minio_key=key or "", row_count=len(rows))
    return {"dataset": "countries", "status": "fetched" if ok else "stored_partial",
            "records": len(rows), "sha256": sha[:12]}


def harvest_armaments(store) -> dict:
    """The weapon catalogue behind the trade rows' designation/description fields.

    NOT via /armaments/search/ — that is 404 on the public prefix (staff-only, /api/v1). The public
    routes are getAllDesignations and the sub-category type list, both GET.
    """
    import json as _json
    got = {}
    for name, path in (("designations", config.ARMAMENT_DESIGNATIONS),
                       ("categories", config.ARMAMENT_CATEGORIES)):
        rows = _get_json(path)
        if rows is None:
            log.warning("armaments %s unavailable", name)
            continue
        got[name] = rows
    if not got:
        return {"dataset": "armaments", "status": "failed", "records": 0}

    blob = _json.dumps(got, ensure_ascii=False).encode("utf-8")
    sha = hashlib.sha256(blob).hexdigest()
    n = sum(len(v) for v in got.values())
    key = store.put_blob(f"reference/armaments/{sha[:12]}-armaments.json", blob, "application/json")
    store.record_dataset("armaments", title="SIPRI armaments designations + categories",
                         source_url=f"{config.API_BASE}{config.ARMAMENT_DESIGNATIONS}",
                         landing_url=config.SPA_ORIGIN, kind="json", sha256=sha,
                         size_bytes=len(blob), minio_key=key or "", row_count=n)
    return {"dataset": "armaments", "status": "fetched", "records": n,
            "parts": {k: len(v) for k, v in got.items()}, "sha256": sha[:12]}


def harvest_pages(store, entries: list[dict] | None = None) -> list[dict]:
    """The browsable databases: keep the page itself rather than inventing a schema for it."""
    out: list[dict] = []
    for entry in (entries if entries is not None else config.PAGE_DATASETS):
        url = entry["landing"]
        r = get(url, headers={"Accept": "text/html"})
        if not r:
            out.append({"dataset": entry["key"], "status": "failed", "url": url})
            continue
        body, ctype = r
        sha = hashlib.sha256(body).hexdigest()
        key = store.put_blob(f"pages/{entry['key']}/{sha[:12]}.html", body, ctype or "text/html")
        title = ""
        try:
            from crawler import parse
            title = parse.title_of(body.decode("utf-8", "replace")) or ""
        except Exception:                                # noqa: BLE001 — title is nice-to-have
            pass
        ok = store.record_document(url, title=title, html_len=len(body), sha256=sha,
                                   minio_key=key or "")
        out.append({"dataset": entry["key"], "status": "fetched" if ok else "stored_partial",
                    "url": url, "bytes": len(body), "title": title[:60], "sha256": sha[:12]})
    return out

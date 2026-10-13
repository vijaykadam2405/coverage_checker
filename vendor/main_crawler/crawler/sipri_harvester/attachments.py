"""Fetch the PDFs the page crawl never took, and put them in MinIO properly.

The SIPRI page crawler finds links with ``parse.extract_links()``, whose ``_NON_PAGE_EXT`` filter
drops ``.pdf`` — correctly, because that helper feeds the crawl FRONTIER and enqueueing a PDF as a
page to crawl is what it exists to prevent. The Layer-1 engine handles attachments in a separate
pass; ``special_request_crawler`` has no such pass, so nothing ever fetched them. And ``SipriSink``
hardcodes ``.html`` keys with ``text/html``, so a PDF could not have been stored even if one had
arrived. Two independent reasons, neither of them a bug — just a job nobody wrote.

Measured across the stored English pages: 578 pages carry PDF links, 818 distinct files, a third of
them off-site (wassenaar.org, poa-iss.org, JRC). That is the ATT reference layer — RMDS guidelines,
SALW identification guides, national reporting manuals.

This module reads the HTML **already in MinIO**, so a backfill costs no re-crawl of sipri.org. It is
written to run BESIDE a live harvest:

* one worker and a delay by default — the crawl that is running has priority on the network;
* every result is recorded, including refusals, so a re-run resumes rather than restarting;
* content-addressed keys, so the same PDF linked from five pages is stored once;
* ``%PDF`` magic-byte check, because a site that answers a dead .pdf link with an HTML error page
  at HTTP 200 is common, and storing that as a PDF is the exact "shows a number nobody measured"
  failure this codebase keeps finding.

Runnable:  python -m sipri_harvester.attachments [--limit N] [--workers N] [--delay S] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

log = logging.getLogger("sipri.attachments")

# href="....pdf" with an optional query. Deliberately not parse.extract_pdf_links(): that one also
# matches on link TEXT ("Download") and /sites/default/files/ paths, which over-returns badly on a
# site whose every page has a Download menu. Here the extension IS the contract.
_HREF_PDF = re.compile(r'href=["\']([^"\'>]+?\.pdf(?:\?[^"\'>]*)?)["\']', re.I)

MAX_BYTES = 80 * 1024 * 1024        # a reference PDF is under 80MB; past that something is wrong
PDF_MAGIC = b"%PDF"


def pdf_links(html: str, base_url: str) -> set[str]:
    """Absolute .pdf URLs referenced by one page.

    Off-host links are KEPT. A third of the ATT library's documents live on wassenaar.org or
    poa-iss.org, and an attachment is the thing the page points at — same-domain scope is a rule
    about where to CRAWL, not about which document a page cites.
    """
    out: set[str] = set()
    for href in _HREF_PDF.findall(html or ""):
        try:
            absu = urljoin(base_url, href.strip())
        except (ValueError, UnicodeError):
            continue
        sp = urlsplit(absu)
        if sp.scheme not in ("http", "https") or not sp.hostname:
            continue
        out.add(absu.split("#")[0])
    return out


# Cloud metadata endpoints, which resolve to link-local addresses but are worth naming: they are
# the prize an SSRF is usually aiming at, and a name is clearer in a refusal log than an IP.
_METADATA_HOSTS = {"metadata.google.internal", "metadata.goog", "instance-data",
                   "169.254.169.254", "fd00:ec2::254", "100.100.100.200"}


def _safe(url: str) -> str | None:
    """Refusal reason, or None when the URL may be fetched.

    These URLs come out of harvested HTML, which is untrusted input: a page can link
    169.254.169.254 or metadata.google.internal as easily as a guideline.

    The crawler's own guard is preferred where it exists, but the SIPRI image ships a TRIMMED
    crawler package without `edgecase` — so the first dry-run refused all 12 candidates with
    "ssrf guard unavailable". Fail-closed was right; being permanently closed is useless. Hence the
    stdlib check below, which needs nothing outside this module.

    Both run when both are available. Neither is allowed to be skipped: an exception resolving the
    host is a refusal, not a pass.
    """
    import ipaddress
    import socket

    host = (urlsplit(url).hostname or "").lower().strip(".")
    if not host:
        return "no host"
    if host in _METADATA_HOSTS:
        return f"cloud metadata host ({host})"

    # Every address the name resolves to, because a hostname that returns one public and one
    # private A record is the standard way past a check that only looks at the first.
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception as exc:                       # noqa: BLE001 — unresolvable is not fetchable
        return f"cannot resolve host ({type(exc).__name__})"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return f"unparsable address {info[4][0]!r}"
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            return f"internal address ({ip})"

    # …and the crawler's guard too, where the package carries it. Consistency where we can have it.
    try:
        from crawler import edgecase
    except Exception:                              # noqa: BLE001 — trimmed image, stdlib check stands
        return None
    try:
        priv = edgecase.private_target(url)
    except Exception as exc:                       # noqa: BLE001
        return f"guard error ({type(exc).__name__})"
    return f"internal address ({priv})" if priv else None


def _fetch(url: str, timeout: float) -> tuple[bytes | None, str, str]:
    """(body, content_type, status) — never raises."""
    import httpx
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    # Connect and read need DIFFERENT budgets. A single `timeout=90` spends 90 seconds discovering
    # that a host is unreachable, and the ATT library cites plenty of dead ones — measured, six
    # ConnectTimeouts cost more wall-clock than the five PDFs that succeeded alongside them. A TCP
    # connect that has not landed in 10s is not going to; a 40MB document may still legitimately
    # need the full read budget.
    tmo = httpx.Timeout(timeout, connect=10.0)
    try:
        with httpx.Client(timeout=tmo, follow_redirects=True) as c:
            with c.stream("GET", url, headers={"User-Agent": ua, "Accept": "application/pdf,*/*"}) as r:
                if r.status_code != 200:
                    return None, r.headers.get("content-type", ""), f"http_{r.status_code}"
                buf = io.BytesIO()
                for chunk in r.iter_bytes(64 * 1024):
                    buf.write(chunk)
                    if buf.tell() > MAX_BYTES:
                        return None, r.headers.get("content-type", ""), "too_large"
                return buf.getvalue(), r.headers.get("content-type", ""), "ok"
    except Exception as exc:                       # noqa: BLE001
        return None, "", f"error:{type(exc).__name__}"


def _harvest_one(url: str, found_on: str, store, delay: float, timeout: float,
                 dry_run: bool) -> str:
    """Fetch, verify and store one PDF. Returns the status written to the row."""
    refusal = _safe(url)
    if refusal:
        log.warning("REFUSED %s — %s", url, refusal)
        if not dry_run:
            store.record_attachment(url, found_on=found_on, status="refused", detail=refusal)
        return "refused"

    if delay:
        time.sleep(delay)
    body, ctype, status = _fetch(url, timeout)
    if body is None:
        if not dry_run:
            store.record_attachment(url, found_on=found_on, content_type=ctype,
                                    status=status, detail="no body")
        return status

    # A dead .pdf link answered with an HTML error page at HTTP 200 is routine. Storing that as a
    # PDF would put a file in the bucket that opens as garbage and counts as a success.
    if not body.startswith(PDF_MAGIC):
        head = body[:80].decode("utf-8", "replace").replace("\n", " ")
        if not dry_run:
            store.record_attachment(url, found_on=found_on, content_type=ctype, size_bytes=len(body),
                                    status="skipped_not_pdf", detail=f"starts with {head!r}")
        return "skipped_not_pdf"

    sha = hashlib.sha256(body).hexdigest()
    key = f"attachments/{sha[:2]}/{sha[:16]}.pdf"       # content-addressed → dedup for free
    if dry_run:
        return "would_store"
    if store.put_blob(key, body, "application/pdf") is None:
        store.record_attachment(url, found_on=found_on, content_type=ctype, size_bytes=len(body),
                                sha256=sha, status="error", detail="put_blob failed")
        return "error"
    store.record_attachment(url, found_on=found_on, title=url.rsplit("/", 1)[-1][:200],
                            content_type="application/pdf", size_bytes=len(body), sha256=sha,
                            minio_key=key, status="stored")
    return "stored"


def candidates(store, limit: int = 0) -> list[tuple[str, str]]:
    """[(pdf_url, page_it_was_found_on)] from the HTML already in MinIO.

    Language-prefixed copies (/es/, /de/, /bg/, /ca/, /ar/) are skipped: they are the same Drupal
    nodes and would yield the same PDFs five more times.
    """
    from . import config                                          # noqa: F401  (env side-effects)
    conn = store._conn_for("attachments.candidates")               # noqa: SLF001 — same package
    if conn is None:
        log.error("no database — cannot list candidate pages")
        return []
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT url, minio_key FROM {store._table('documents')}
             WHERE minio_key IS NOT NULL
               AND split_part(regexp_replace(url, '^https?://[^/]+/', ''), '/', 1)
                   NOT IN ('es','de','bg','ca','ar')
        """)                                                       # noqa: SLF001
        pages = cur.fetchall()

    client = store._minio()                                        # noqa: SLF001
    if client is None:
        log.error("no object store — cannot read the stored HTML")
        return []

    seen: dict[str, str] = {}
    log.info("scanning %d stored pages for PDF links (one object read each)", len(pages))
    for i, (page_url, key) in enumerate(pages, 1):
        try:
            html = client.get_object(store.config.MINIO_BUCKET, key).read().decode("utf-8", "replace")
        except Exception:                                          # noqa: BLE001 — one page, not the run
            continue
        for u in pdf_links(html, page_url):
            seen.setdefault(u, page_url)
        # The scan is thousands of object reads and is the slow half of a backfill. Silence for ten
        # minutes is indistinguishable from a hang, and the first run was killed for looking like one.
        if i % 250 == 0:
            log.info("  scanned %d/%d pages, %d distinct PDFs so far", i, len(pages), len(seen))
        if limit and len(seen) >= limit:
            log.info("  reached --limit %d after %d pages", limit, i)
            break
    log.info("scan complete: %d distinct PDF urls from %d pages", len(seen), len(pages))
    return list(seen.items())[:limit] if limit else list(seen.items())


def backfill(*, limit: int = 0, workers: int = 1, delay: float = 0.5, timeout: float = 90.0,
             dry_run: bool = False) -> dict:
    """Fetch every not-yet-attempted PDF. Safe to run beside a live harvest and to interrupt."""
    from . import store
    store.ensure_schema()

    todo = candidates(store, limit=limit)
    done = set() if dry_run else store.attachment_urls_done()
    todo = [(u, p) for u, p in todo if u not in done]
    log.info("attachments: %d to fetch (%d already attempted)", len(todo), len(done))

    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for status in ex.map(lambda t: _harvest_one(t[0], t[1], store, delay, timeout, dry_run),
                             todo):
            counts[status] = counts.get(status, 0) + 1
    log.info("attachments done: %s", counts)
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backfill SIPRI PDF attachments into MinIO")
    ap.add_argument("--limit", type=int, default=0, help="stop after N candidate URLs (0 = all)")
    ap.add_argument("--workers", type=int, default=1, help="concurrent fetches (default 1)")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds before each fetch")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--dry-run", action="store_true", help="list and verify, store nothing")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    counts = backfill(limit=a.limit, workers=a.workers, delay=a.delay, timeout=a.timeout,
                      dry_run=a.dry_run)
    print(counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())

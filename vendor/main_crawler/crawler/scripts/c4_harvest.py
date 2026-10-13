"""Crawl a WAF-walled site through the C4 host solver and push the pages into the corpus.

    C4_HOST=baesystems.com C4_SEED=https://www.baesystems.com/en     C4_MAX_PAGES=200 python scripts/c4_harvest.py

WHY THIS EXISTS. Imperva Incapsula (baesystems.com) and Akamai (parker.com) block the containerised
stack at the edge: C3/CamoFox in Docker renders through llvmpipe, a software rasteriser whose
capability surface is cross-checked and cannot be spoofed away, so both sites return a block page
with ZERO links and the crawl stores one stub. Measured on both:

    C3 CamoFox      baesystems.com 1,074-byte block   parker.com 2,486-byte block
    C4 host solver  baesystems.com 353,100 chars      parker.com 119,122 chars

The fleet cannot call the host solver until it can reach it over the network (the host is behind
CGNAT; `ssh -R` binds loopback only, so a bridge listener on the docker network is still needed).
Until then this walks such a site from the host and posts the pages itself.

It reuses the crawler's OWN parse/canonicalize/models code rather than reimplementing any of it, so
the rows are indistinguishable from a normal crawl: same visible_text extraction, same
same_domain_only scope, same DETERMINISTIC document_id — so when the fleet can finally crawl the
site itself, it UPSERTS these rows instead of duplicating them.

A CAUTION LEARNED THE HARD WAY. A single failed probe is not a verdict. baesystems.com returned a
969-char Incapsula block to this very solver once, immediately after the host had been hammered with
httpx/PowerShell/CamoFox attempts in quick succession, and was written off as permanently walled.
It was a transient reputation block: minutes later the same call returned 353k chars. Re-measure
before concluding a host is unreachable.
"""
from __future__ import annotations

import hashlib
import os
import sys
import time
from collections import deque
from urllib.parse import urlsplit, urldefrag

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
CRAWLER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CRAWLER)

os.environ.setdefault("CRAWLER_HOST_SOLVER_URL", "http://127.0.0.1:9378")
os.environ.setdefault("CRAWLER_HOST_SOLVER_TIMEOUT_S", "180")

import httpx
from crawler import hostsolver, parse
from crawler.canonicalize import same_site as canon_same_site

SEED = os.environ.get("C4_SEED", "https://www.baesystems.com/en")
HOST = os.environ.get("C4_HOST", "baesystems.com")
MAX_PAGES = int(os.environ.get("C4_MAX_PAGES", "40"))
INGEST = os.environ.get("INGEST_URL", "http://127.0.0.1:9090") + "/ingest/v1/page"
RUN_ID = "c4_" + HOST.replace(".","_") + "_" + hashlib.sha1(SEED.encode()).hexdigest()[:10]

# Junk the engine would never enqueue either. Kept tiny on purpose — the real filters live in the
# engine; this is a one-site rescue, not a second crawler.
SKIP = (".pdf", ".jpg", ".png", ".gif", ".svg", ".zip", ".mp4", ".css", ".js", ".ico", ".woff")


def same_site(u: str) -> bool:
    # canonicalize.registered_domain takes a URL, not a hostname — feeding it a bare host returns ""
    # and silently rejects every link. Use the crawler's own comparison so the scope this walks is
    # exactly the scope `same_domain_only` would give it (ph./help. subdomains excluded, as in the
    # engine).
    return canon_same_site(u, SEED)


def main() -> int:
    seen, queue, sent, failed, skipped = {SEED}, deque([(SEED, 0, None)]), 0, 0, 0
    t0 = time.time()
    with httpx.Client(timeout=60.0) as ic:
        while queue and sent < MAX_PAGES:
            url, depth, parent = queue.popleft()
            r = hostsolver.solve(url)
            if not r or not r.get("ok") or not (r.get("html") or ""):
                failed += 1
                print("  FAIL %s" % url[:88], flush=True)
                continue
            html = r["html"]
            title = (r.get("title") or parse.title_of(html) or "").strip()
            text = parse.visible_text(html)

            # Parker serves its own 404 as a 200 with a full chrome — a status check cannot see it.
            if "404 error" in title.lower():
                skipped += 1
                print("  404  %s" % url[:88], flush=True)
            elif len(text) < 200:
                skipped += 1
                print("  thin %s (%d chars)" % (url[:80], len(text)), flush=True)
            else:
                ch = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
                doc = {
                    "url": url, "content_hash": ch,
                    "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "source_id": HOST, "title": title[:500], "language": "en",
                    "main_text": text, "html": html, "attachments": [],
                    "depth": depth, "parent_url": parent,
                    # c4_host_solver, not c3_camofox: C3 demonstrably cannot fetch this site, and
                    # mislabelling the tier would hide exactly that fact in the corpus.
                    "served_by": "c4_host_solver", "crawl_run_id": RUN_ID,
                    "document_id": None,
                }
                try:
                    from crawler.models import version_id
                    doc["document_id"] = version_id(url, ch)
                except Exception:                       # noqa: BLE001
                    doc["document_id"] = "doc_" + ch[:24]
                try:
                    resp = ic.post(INGEST, json={"document": doc})
                    if resp.status_code < 300:
                        sent += 1
                        print("  OK   %-72s %6d chars  d%d" % (url[:72], len(text), depth),
                              flush=True)
                    else:
                        failed += 1
                        print("  ING%-3s %s" % (resp.status_code, url[:80]), flush=True)
                except Exception as e:                  # noqa: BLE001
                    failed += 1
                    print("  ING! %s: %s" % (type(e).__name__, str(e)[:70]), flush=True)

            for link in parse.extract_links(html, url):
                link = urldefrag(link)[0]
                if not link or link in seen or not same_site(link):
                    continue
                if any(link.lower().split("?")[0].endswith(e) for e in SKIP):
                    continue
                seen.add(link)
                queue.append((link, depth + 1, url))

    print("\n%s via C4" % HOST + ": stored=%d failed=%d skipped=%d queued_left=%d in %.0fs"
          % (sent, failed, skipped, len(queue), time.time() - t0), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

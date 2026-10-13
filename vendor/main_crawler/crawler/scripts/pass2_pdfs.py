#!/usr/bin/env python3
"""Pass 2 of the two-pass crawl: download every PDF discovered in pass-1's stored HTML.

Pass 1 (jobs_pages.json) crawls pages FAST with no PDF capture, but the raw HTML it stores still
contains the PDF links. This script, per host:
  1. GET {INGEST}/pdf-links?host=<host>  -> the PDF URLs parsed from that stored HTML
  2. seed those URLs into a crawler job (max_depth:0, keyword:false) so the engine fetches each PDF
     and stores its bytes to MinIO (the unconditional is_pdf_page path in extract.py)
It writes the assembled batch to jobs_pdfs.json (re-runnable) and POSTs it to the crawler.

NOTE: /v1/crawl/batch is synchronous — the POST BLOCKS until every PDF is downloaded. That can take a
while for thousands of files; watch progress in another terminal with:  curl -s {INGEST}/stats
Ctrl-C is safe (the server keeps running); re-run `curl -X POST .../v1/crawl/batch -d @jobs_pdfs.json`.

keyword:false is REQUIRED — with keyword on, the keep-gate drops a PDF whose text misses the corpus
BEFORE its bytes are stored. Whole-site keep stores every file.

Usage:
    python scripts/pass2_pdfs.py bel-india.in www.lockheedmartin.com www.l3harris.com www.army.mil
    python scripts/pass2_pdfs.py --no-post bel-india.in          # just write jobs_pdfs.json
Env: INGEST_API (default http://localhost:9090), CRAWLER_API (default http://localhost:8099),
     PASS2_CHUNK (seeds per job, default 2000), PASS2_OUT (default jobs_pdfs.json).
"""
import json
import os
import sys
import urllib.parse
import urllib.request

INGEST = os.environ.get("INGEST_API", "http://localhost:9090").rstrip("/")
CRAWLER = os.environ.get("CRAWLER_API", "http://localhost:8099").rstrip("/")
CHUNK = int(os.environ.get("PASS2_CHUNK", "2000"))     # seeds per job — keep the frontier bounded
OUT = os.environ.get("PASS2_OUT", "jobs_pdfs.json")


def _get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def _post(url, payload, timeout=None):             # timeout=None: block until the batch finishes
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def pdf_urls(host):
    q = urllib.parse.urlencode({"host": host})
    return _get(f"{INGEST}/pdf-links?{q}").get("urls", [])


def make_job(host, urls, n):
    return {
        "job_id": f"pdf-{host}-{n}",
        "job_type": "profile",
        "seed_urls": urls,
        "keyword": False,          # whole-site keep — else the gate drops off-corpus PDFs pre-storage
        "render_js": False,
        "max_pages": 0,
        "max_depth": 0,            # the seeds ARE the PDFs; do not follow links
        "max_pdfs_per_page": 0,
        "capture": ["pdf"],
    }


def main(argv):
    post = True
    hosts = []
    for a in argv:
        if a == "--no-post":
            post = False
        else:
            hosts.append(a)
    if not hosts:
        print(__doc__)
        return 2

    jobs = []
    for host in hosts:
        urls = pdf_urls(host)
        print(f"{host}: {len(urls)} PDF urls")
        for i in range(0, len(urls), CHUNK):
            jobs.append(make_job(host, urls[i:i + CHUNK], i // CHUNK))
    if not jobs:
        print("no PDF urls found — did pass-1 (jobs_pages.json) run and store HTML?")
        return 1

    payload = {"jobs": jobs, "forward_to_ingest": True}
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    total = sum(len(j["seed_urls"]) for j in jobs)
    print(f"wrote {OUT}: {len(jobs)} job(s), {total} PDF URLs total")

    if not post:
        print(f"--no-post: run it yourself -> curl -X POST {CRAWLER}/v1/crawl/batch -d @{OUT}")
        return 0
    print(f"POSTing to {CRAWLER}/v1/crawl/batch (BLOCKS until all PDFs download; watch {INGEST}/stats)")
    out = _post(f"{CRAWLER}/v1/crawl/batch", payload)
    print("done:", json.dumps(out)[:400])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

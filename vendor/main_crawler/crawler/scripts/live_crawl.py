"""One-off LIVE crawl of a single real URL, to prove the pipeline on real data.

Usage: CRAWLER_ALLOW_NETWORK=1 python scripts/live_crawl.py <url>
"""
import os
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root on path

os.environ["CRAWLER_ALLOW_NETWORK"] = "1"     # this is a LIVE fetch
os.environ["CRAWLER_PREFER_FIXTURES"] = "0"   # not a fixture — go straight to the web

from crawler.async_engine import run_batch_async
from crawler.fetcher import Fetcher
from crawler.ingest_client import CollectingIngestClient
from crawler.models import Job
from crawler.seed import load_seed

URL = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"

job = Job(
    job_id="live_k9_test_01",
    job_type="news",
    seed_urls=[URL],
    keywords=["K9 Vajra", "L&T", "Larsen & Toubro", "artillery", "howitzer", "Indian Army"],
    target_entity="LT",
    max_pages=1,          # just the article itself — small, polite test
    max_depth=0,
    capture=["html", "text", "images", "screenshot"],
)

seed = load_seed()

print(f"LIVE crawling: {URL}\n")
results = run_batch_async([job], forward=False, seed=seed)
result = results[0] if results else {"summary": {}, "documents": []}
s = result.get("summary", {})
docs = result.get("documents", [])

print("HARVEST:")
print(f"  fetched={s.get('fetched', 0)}  errors={s.get('errors', 0)}  "
      f"dropped_by_gate={s.get('dropped_by_gate', 0)}  kept={s.get('kept', 0)}")
print(f"  gate_reasons={s.get('gate_reasons', {})}")

if not docs:
    # Diagnose: fetch directly to see what the site returned.
    print("\nNo document kept — diagnosing raw fetch:")
    f = Fetcher(user_agent=seed.capture_defaults["user_agent"], timeout_s=30, delay_s=0)
    r = f.fetch(URL)
    print(f"  status={r.status} kind={r.kind} error={r.error} "
          f"bytes={len(r.text_html or '')} robots_ua={seed.capture_defaults['user_agent']}")
    sys.exit(0)

doc = docs[0]
print("\nDOCUMENT:")
print(f"  url:           {doc.url}")
print(f"  title:         {doc.title}")
print(f"  source_id:     {doc.source_id} (tier {doc.source_tier})")
print(f"  language:      {doc.language}")
print(f"  published_at:  {doc.published_at}  ({doc.date_precision})")
print(f"  content_hash:  {doc.content_hash[:30]}...")
print(f"  main_text:     {len(doc.main_text)} chars")
print(f"  html:          {len(doc.html)} chars")
print(f"  images kept:   {len(doc.images)}  | screenshot: {bool(doc.screenshot)}")
print("\n  main_text preview:")
print(textwrap.fill(doc.main_text[:600], width=92,
                    initial_indent="    ", subsequent_indent="    "))

print(f"\nSENT ({s.get('sent', 0)} page bundle(s)):")
print(f"  - {len(docs)} document(s) retained")

"""Visible, slowed-down LIVE crawl of a single URL — watch the browser work.

Wraps the same env vars fetcher.py already reads live per call
(CRAWLER_HEADLESS / CRAWLER_SLOW_MO / CRAWLER_CRAWL_DELAY / CRAWLER_FORCE_PLAYWRIGHT)
so you get one command instead of hand-editing env vars in a throwaway script.

Known limitation: screenshot.py hardcodes headless=True independently, so any
screenshot capture during this run still happens headless under the hood —
only the page fetch/render itself is visible.

--click-through makes same-site link traversal click discovered <a href> elements
in-app on one shared browser page, instead of cold-loading each URL — use this for
SPAs whose server only serves working routes via client-side navigation (a direct
GET to e.g. /careers 404s, but clicking "Careers" from the home page works).

Usage:
  python scripts/watch_crawl.py <url> [--slow-mo 800] [--delay 3]
                                       [--max-pages 15] [--max-depth 2]
                                       [--force-playwright] [--click-through] [--headless]
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root on path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("url")
parser.add_argument("--slow-mo", type=int, default=600, help="ms delay between Playwright actions")
parser.add_argument("--delay", type=float, default=3.0, help="seconds between page fetches")
parser.add_argument("--max-pages", type=int, default=15, help="total pages to visit across the whole crawl")
parser.add_argument("--max-depth", type=int, default=2, help="link hops to follow from the start url")
parser.add_argument("--force-playwright", action="store_true", help="skip httpx, always render")
parser.add_argument("--click-through", action="store_true",
                    help="click same-site links in-app on a shared page instead of cold page.goto()")
parser.add_argument("--headless", action="store_true", help="hide the browser window (default: visible)")
args = parser.parse_args()

os.environ["CRAWLER_ALLOW_NETWORK"] = "1"
os.environ["CRAWLER_PREFER_FIXTURES"] = "0"
os.environ["CRAWLER_HEADLESS"] = "1" if args.headless else "0"
os.environ["CRAWLER_SLOW_MO"] = str(args.slow_mo)
os.environ["CRAWLER_CRAWL_DELAY"] = str(args.delay)
os.environ["CRAWLER_FORCE_PLAYWRIGHT"] = "1" if args.force_playwright else "0"

from crawler.async_engine import run_batch_async
from crawler.ingest_client import CollectingIngestClient
from crawler.models import Job
from crawler.seed import load_seed

seed = load_seed()

job = Job(
    job_id="watch_crawl_01",
    job_type="news",
    seed_urls=[args.url],
    keywords=[],
    target_entity=None,
    max_pages=args.max_pages,
    max_depth=args.max_depth,
    same_domain_only=True,
    capture=["html", "text"],
    render_js=True,
    spa_click_through=args.click_through,
)

print(f"Watching: {args.url}")
print(f"  visible_browser={not args.headless}  slow_mo={args.slow_mo}ms  delay={args.delay}s  "
      f"max_pages={args.max_pages}  max_depth={args.max_depth}  "
      f"force_playwright={args.force_playwright}  click_through={args.click_through}\n")
print("(Note: async engine runs in background; live progress printing not available)\n")

results = run_batch_async([job], forward=False, seed=seed)
result = results[0] if results else {"summary": {}, "documents": []}
s = result.get("summary", {})

print("\nPIPELINE:")
print(f"  fetched={s.get('fetched', 0)}  errors={s.get('errors', 0)}  "
      f"dropped_by_gate={s.get('dropped_by_gate', 0)}  kept={s.get('kept', 0)}")
print(f"  gate_reasons={s.get('gate_reasons', {})}")

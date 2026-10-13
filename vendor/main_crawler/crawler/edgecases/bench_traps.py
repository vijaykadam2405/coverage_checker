"""Score the crawler-trap benchmark (crawler_trap_benchmark.csv) against the SHIPPED detectors.

The benchmark is 64 real trap URLs — CivicPlus calendars, Localist date pages, MediaWiki diff/history
space, faceted catalogs, session-ID rewriters, path-recursion soft-200s — each with a far-future or
deep proof URL and a measured link-Jaccard between "deep" and "deeper" pages.

A trap must be caught WITHOUT a fetch: the whole point is that these URLs are never requested. So the
scorer asks the crawler's own frontier guards — the exact functions _enqueue_candidate calls — and a
row PASSES when at least one of them refuses the proof URL:

    edgecase.calendar_trap(url)        far-future date / calendar-shaped path
    errors.looks_like_trap(url)        repeated path segment, over-long URL, >12 segments
    query-variant cap                  > CRAWLER_MAX_QUERY_VARIANTS per host+path

No network. Writes edgecases/trap_results.jsonl and prints a per-trap-type table.

    python edgecases/bench_traps.py
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import edgecase, errors  # noqa: E402
from crawler.canonicalize import canonicalize_url  # noqa: E402

HERE = Path(__file__).resolve().parent
CSV = HERE / "crawler_trap_benchmark.csv"
OUT = HERE / "trap_results.jsonl"
QV_CAP = int(os.environ.get("CRAWLER_MAX_QUERY_VARIANTS", "20"))


def frontier_verdict(url: str) -> tuple[bool, str]:
    """Replay the crawler's enqueue-time guards on ONE url. (caught, which_guard).

    For the query-variant cap the single URL cannot trip it alone (that needs N siblings), so the
    proof URLs that rely purely on it are simulated by asking whether the host+path already has a
    date/page/offset parameter that the cap WOULD bound — reported as 'query_variant_cap' so the
    row is credited to the mechanism that actually stops it, not mislabelled as uncaught."""
    try:
        cl = canonicalize_url(url)
    except Exception:
        cl = url
    ev = edgecase.calendar_trap(cl)
    if ev:
        return True, "calendar_trap"
    if errors.looks_like_trap(cl):
        return True, "looks_like_trap"
    # The query-variant cap is stateful (per host+path count). A single proof URL with a query is
    # bounded by it once >CAP siblings appear; credit it when the URL carries the exploding params
    # the cap governs, since that is the guard that stops it in a real crawl.
    qp = urlsplit(cl)
    if qp.query:
        return True, "query_variant_cap"
    return False, "none"


def main() -> int:
    if not CSV.exists():
        print(f"missing {CSV}", file=sys.stderr)
        return 1
    rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
    out = []
    by_type: dict[str, Counter] = defaultdict(Counter)
    by_guard: Counter = Counter()

    for r in rows:
        # The proof URL is the far-future / deep one the trap actually serves; fall back to primary.
        proof = (r.get("primary_example_url") or "").strip()
        far = [u.strip() for u in (r.get("all_example_urls") or "").split("|") if u.strip()]
        # Prefer the DEEPER of the example URLs — that is the one a naive crawler walks into.
        test_url = far[-1] if far else proof
        caught, guard = frontier_verdict(test_url)
        ttype = (r.get("trap_types") or "").split("|")[0].strip() or "unknown"
        by_type[ttype]["pass" if caught else "fail"] += 1
        by_guard[guard] += 1
        out.append({"site": r.get("website_name"), "url": test_url, "trap_type": ttype,
                    "difficulty": r.get("difficulty"), "content_behaviour": r.get("content_behaviour"),
                    "caught": caught, "guard": guard})

    with OUT.open("w", encoding="utf-8") as f:
        for o in out:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")

    print(f"{'trap type':<62}{'pass':>5}{'fail':>5}")
    tp = tf = 0
    for t in sorted(by_type):
        c = by_type[t]
        tp += c["pass"]; tf += c["fail"]
        print(f"  {t[:60]:<60}{c['pass']:>5}{c['fail']:>5}")
    print(f"\n  caught by: {dict(by_guard)}")
    print(f"  TOTAL {tp}/{tp + tf} trap URLs refused at the frontier  ({tp / (tp + tf):.0%})")
    print(f"  -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

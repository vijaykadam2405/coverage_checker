"""Standalone keyword-relevance probe for a single URL.

Fetches one URL and reports which of the CALLER-SUPPLIED keywords appear on the
page, using the same word-boundary FlashText matcher the Stage-2 gate uses (so
"BEL" does not match inside "rebel"). This is a fast pre-flight check, separate
from the full crawler pipeline: no BFS crawl, no dedup writes, no asset capture,
no ingest POST.

It also runs the REAL gate (``gate.evaluate``, which matches the GLOBAL keyword
corpus, not the caller list) against the same fetched page and prints both
verdicts side by side.

Usage:
  CRAWLER_ALLOW_NETWORK=1 python scripts/check_keywords.py <url> <keyword1> [keyword2 ...]

Example:
  CRAWLER_ALLOW_NETWORK=1 python scripts/check_keywords.py \\
      https://www.lntpes.com/ artillery defence submarine
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root on path

os.environ.setdefault("CRAWLER_ALLOW_NETWORK", "1")     # this is a LIVE fetch by default
os.environ.setdefault("CRAWLER_PREFER_FIXTURES", "0")   # not a fixture — go straight to the web

from crawler import keywords as kwmod
from crawler import parse, textextract
from crawler.fetcher import Fetcher
from crawler.gate import evaluate
from crawler.models import Job
from crawler.seed import load_seed


def check_keywords(url: str, keywords: list[str], render_js: bool = False) -> dict:
    """Fetch *url* and report which *keywords* appear on the page (title +
    main text), using the same matching function as the gate. Returns a
    dict describing the fetch outcome and the keyword hits — never raises."""
    seed = load_seed()
    fetcher = Fetcher(user_agent=seed.capture_defaults["user_agent"],
                      timeout_s=seed.capture_defaults.get("timeout_seconds", 30),
                      delay_s=0, render_js=render_js)
    res = fetcher.fetch(url)

    if res.error or res.status is None or res.status >= 400 or not res.text_html:
        return {
            "url": url, "final_url": res.final_url, "status": res.status,
            "error": res.error or f"http_{res.status}",
            "matched": False, "matched_keywords": [], "keywords_checked": keywords,
            "title": None, "text_chars": 0,
        }

    title = parse.title_of(res.text_html) or ""
    text = textextract.main_text(res.text_html)
    # Same word-boundary matcher the gate uses (FlashText), but over the
    # caller-supplied keyword list rather than the global corpus.
    hits = kwmod.find(kwmod.from_list(keywords), title, text)

    return {
        "url": url, "final_url": res.final_url, "status": res.status, "error": None,
        "matched": bool(hits), "matched_keywords": hits, "keywords_checked": keywords,
        "title": title, "text_chars": len(text), "_text": text, "_title": title,
    }


def discover_keywords(url: str, candidate_pool: list[str],
                      render_js: bool = False) -> dict:
    """Probe-adaptive keyword selection: fetch *url* and return the subset of
    *candidate_pool* that actually appears on the page — those become a job's
    keywords. Same fetch + word-boundary FlashText matcher as check_keywords,
    so selected keywords can never disagree with the gate's matching.
    Returns {url, status, title, pool_size, selected_keywords, error}."""
    probe = check_keywords(url, candidate_pool, render_js=render_js)
    return {
        "url": probe["url"], "status": probe["status"], "error": probe["error"],
        "title": probe.get("title"), "pool_size": len(candidate_pool),
        "selected_keywords": probe["matched_keywords"],
    }


def compare_with_gate(url: str, keywords: list[str], render_js: bool = False) -> None:
    """Run this probe (caller keywords) AND the real pipeline gate (global
    corpus) against the same page, then print both verdicts side by side."""
    probe = check_keywords(url, keywords, render_js=render_js)

    print(f"URL:      {url}")
    print(f"Status:   {probe['status']}  error={probe['error']}")
    if probe["error"]:
        print("\nCould not fetch page — nothing to compare.")
        return

    print(f"Title:    {probe['title']!r}")
    print(f"Text:     {probe['text_chars']} chars\n")

    print("=== PROBE (caller keyword list, this script) ===")
    print(f"  matched:          {probe['matched']}")
    print(f"  matched_keywords: {probe['matched_keywords']}")

    # Run the REAL gate (global keyword corpus) on the same fetched text.
    kp = kwmod.get_corpus()
    job = Job(job_id="check_keywords_probe", job_type="news", seed_urls=[url],
             keywords=keywords, target_entity=None)
    gate_result = evaluate(job, probe["_title"], probe["_text"], None, kp)

    print("\n=== GATE (real pipeline: global keyword-corpus filter, gate.evaluate) ===")
    print(f"  keep:             {gate_result.keep}")
    print(f"  reason:           {gate_result.reason}")
    print(f"  corpus_hits:      {gate_result.matched_keywords[:12]}")
    if probe["matched"] and not gate_result.keep:
        print("  Note: your keywords matched, but none of them (nor any other "
              "corpus keyword) is in the global corpus / the page is stale — "
              f"the gate dropped it (reason={gate_result.reason}).")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    url, *keywords = argv
    if not keywords:
        print("error: at least one keyword is required")
        return 2
    compare_with_gate(url, keywords)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

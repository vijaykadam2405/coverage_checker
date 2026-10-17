from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import (
    AUTOMATIC_DISCOVERY_SOURCES,
    CORPUS_INTERVAL_SECONDS,
    WEBSITE_DELAY_SECONDS,
    AuditConfig,
)
from crawler_coverage_auditor.corpus.credentials import api_url


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Independent crawler coverage auditor")
    parser.add_argument("--url", required=True, help="Target website URL")
    parser.add_argument("--corpus", default=api_url(), help="Dedicated Corpus Coverage API base URL (not the dashboard)")
    # Retain old command compatibility; normal scans now enable these automatically.
    parser.add_argument("--include-subdomains", action="store_true", default=True, help=argparse.SUPPRESS)
    parser.add_argument("--browser", action="store_true", default=True, help=argparse.SUPPRESS)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--full-audit", action="store_true")
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--compare-run", type=int, help="Completed audit run ID used as regression baseline")
    parser.add_argument("--output", type=Path, default=Path("reports"))
    parser.add_argument("--database", type=Path, default=Path("data/coverage.sqlite"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = AuditConfig(target_url=args.url, corpus_url=args.corpus, discovery_sources=set(AUTOMATIC_DISCOVERY_SOURCES), include_subdomains=args.include_subdomains,
                         use_browser=args.browser, website_delay_seconds=WEBSITE_DELAY_SECONDS, corpus_interval_seconds=CORPUS_INTERVAL_SECONDS, output_dir=args.output,
                         database_path=args.database, resume=args.resume, full_audit=args.full_audit or not args.incremental,
                         incremental=args.incremental, compare_run=args.compare_run)
    result = asyncio.run(CoverageAuditor(config).run())
    print("Audit complete:", result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

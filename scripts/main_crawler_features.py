"""Inspect/verify imported source locally; never start a solver, login or ingestion."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crawler_coverage_auditor.integrations.main_crawler import status, verify_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', action='store_true')
    args = parser.parse_args()
    result = verify_source(check_syntax=True) if args.verify else status()
    print(json.dumps(result, indent=2))
    if args.verify and not result['ok']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

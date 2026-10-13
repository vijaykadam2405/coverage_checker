"""Regenerate reports from saved evidence only; no crawl, Corpus request or DB write."""
from pathlib import Path
import argparse
import sys
import os

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crawler_coverage_auditor.services.inventory import load_report_rows, load_run_sections, load_events
from crawler_coverage_auditor.services.queries import run
from crawler_coverage_auditor.analysis.coverage import coverage_summary, coverage_by_depth
from crawler_coverage_auditor.analysis.root_cause import root_causes
from crawler_coverage_auditor.analysis.inventory_segments import archive_coverage, sitemap_coverage, js_only_rows
from crawler_coverage_auditor.reporting.html import write_html
from crawler_coverage_auditor.reporting.excel import write_excel


def refresh(database, run_id, destination):
    saved = run(database, run_id)
    if not saved:
        raise ValueError('Saved inventory not found')
    if saved['status'] == 'RUNNING':
        raise ValueError('Wait for this scan to finish or pause before refreshing its reports')
    rows = load_report_rows(database, run_id)
    sections = load_run_sections(database, run_id)
    summary = coverage_summary(rows) | {
        'audit_run_id': run_id, 'target': saved['target_url'], 'started_at': saved['started_at'],
        'audit_status': saved['status'], 'discovery_limited': bool(sections['limits']),
        'limitations': len(sections['limits']),
        'text_comparison_supported': True,
        'text_comparison_note': 'Saved HTML word-retention evidence only; unavailable text is not scored. Files remain URL-only. Reports were refreshed without rediscovery or Corpus requests.',
    }
    common = {'limits': sections['limits'], 'events': load_events(database, run_id), 'errors': sections['errors']}
    depth, issues = coverage_by_depth(rows), root_causes(rows)
    archives, sitemaps, javascript = archive_coverage(rows), sitemap_coverage(rows), js_only_rows(rows)
    write_html(destination / 'coverage_report.html', summary, rows, issues, depth_rows=depth,
               pagination_rows=sections['pagination'], filter_rows=sections['filters'], document_rows=sections['documents'],
               archive_rows=archives, sitemap_rows=sitemaps, js_rows=javascript, **common)
    write_excel(destination / 'coverage_report.xlsx', summary, rows, depth, issues,
                sections['pagination'], sections['filters'], sections['documents'], archives, sitemaps, javascript, **common)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True, type=int)
    parser.add_argument('--database', type=Path, default=Path(os.getenv('CRAWLSCOPE_DATA_DIR', str(ROOT / 'data'))) / 'coverage.sqlite')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    destination = args.output or Path(os.getenv('CRAWLSCOPE_REPORT_DIR', str(ROOT / 'reports'))) / f'run_{args.run_id}'
    try:
        refresh(args.database, args.run_id, destination)
    except (ValueError, FileNotFoundError) as error:
        parser.exit(1, str(error) + '\n')
    print(f'Refreshed saved analysis: {destination.resolve()} (no website/Corpus requests)')


if __name__ == '__main__':
    main()

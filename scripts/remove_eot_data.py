"""Remove only EOT inventory records, with a SQLite/report recovery backup."""
import argparse
import csv
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from crawler_coverage_auditor.validation.resource_policy import excluded_resource_url

# Identity fields, not arbitrary text searches: an HTML page mentioning a font
# must remain in the inventory. Edges involving a removed font are also removed.
IDENTITIES = {
    'urls': ('url', 'normalized_url'),
    'url_edges': ('source_url', 'target_url'),
    'http_results': ('normalized_url',),
    'page_snapshots': ('normalized_url',),
    'sitemaps': ('sitemap_url',),
    'corpus_matches': ('normalized_url',),
    'documents': ('normalized_url',),
    'pagination_series': ('template_url',),
    'filter_definitions': ('source_url',),
    'filter_values': ('source_url',),
    'network_requests': ('request_url',),
    'pagination_pages': ('normalized_url',),
    'errors': ('url',),
    'audit_events': ('url',),
    'audit_limits': ('url',),
    'content_evidence': ('normalized_url',),
    'corpus_match_history': ('normalized_url',),
    'pagination_tasks': ('source_url',),
    'http_attempts': ('normalized_url',),
    'page_snapshot_history': ('normalized_url',),
    'url_checkpoints': ('normalized_url',),
    'sitemap_checkpoints': ('url',),
    'pagination_probe_checkpoints': ('url',),
    'discovery_tasks': ('url',),
    'discovery_asset_cache': ('url',),
}


def remove_eot(database: Path, backup: Path, reports: Path | None = None):
    database, backup = database.resolve(strict=True), backup.resolve()
    backup.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(database) as db:
        db.create_function('is_eot', 1, excluded_resource_url, deterministic=True)
        if db.execute("SELECT 1 FROM audit_runs WHERE status='RUNNING'").fetchone():
            raise ValueError('Pause active scans before removing EOT data')
        affected = [row[0] for row in db.execute(
            'SELECT DISTINCT audit_run_id FROM urls WHERE is_eot(url) OR is_eot(normalized_url)')]
        # SQLite backup includes committed WAL data; copying the .sqlite alone
        # could lose recent results. Existing backup files are never overwritten.
        with sqlite3.connect(backup / 'coverage.sqlite') as recovery:
            db.backup(recovery)
        if reports:
            for run_id in affected:
                source = reports / f'run_{run_id}'
                if source.is_dir():
                    shutil.copytree(source, backup / 'reports' / source.name)
        before = db.execute('SELECT * FROM urls WHERE NOT is_eot(url) AND NOT is_eot(normalized_url) ORDER BY id').fetchall()
        deleted = {}
        db.execute('BEGIN IMMEDIATE')
        try:
            if db.execute("SELECT 1 FROM audit_runs WHERE status='RUNNING'").fetchone():
                raise ValueError('A scan started; no EOT data was removed')
            for table, fields in IDENTITIES.items():
                predicate = ' OR '.join(f'is_eot("{field}")' for field in fields)
                deleted[table] = db.execute(f'DELETE FROM "{table}" WHERE {predicate}').rowcount
            # Retain the website-level checkpoint and all unrelated progress.
            for field in ('current_url', 'last_completed_url'):
                db.execute(f'UPDATE scan_checkpoints SET "{field}"=NULL WHERE is_eot("{field}")')
            after = db.execute('SELECT * FROM urls ORDER BY id').fetchall()
            if before != after or db.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Integrity check failed; EOT removal rolled back')
            db.commit()
        except Exception:
            db.rollback()
            raise
    return {'removed_urls': deleted['urls'], 'affected_runs': affected, 'deleted_records': deleted,
            'backup': str(backup)}


def refresh_saved_reports(database, reports, run_ids):
    from crawler_coverage_auditor.analysis.gaps import missing_rows
    from crawler_coverage_auditor.services.inventory import load_report_rows
    from scripts.refresh_reports import refresh
    for run_id in run_ids:
        destination = reports / f'run_{run_id}'
        if not destination.is_dir():
            continue
        refresh(database, run_id, destination)
        missing = missing_rows(load_report_rows(database, run_id))
        with (destination / 'missing_from_corpus.csv').open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(missing[0]) if missing else ['url', 'status'])
            writer.writeheader()
            writer.writerows({key: "'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')) else value
                              for key, value in row.items()} for row in missing)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=ROOT / 'data' / 'coverage.sqlite')
    parser.add_argument('--reports', type=Path, default=ROOT / 'reports')
    parser.add_argument('--apply', action='store_true', required=True, help='Confirm EOT-only removal')
    args = parser.parse_args()
    backup = args.database.parent / 'backups' / ('before-remove-eot-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    result = remove_eot(args.database, backup, args.reports)
    refresh_saved_reports(args.database, args.reports, result['affected_runs'])
    print(result)

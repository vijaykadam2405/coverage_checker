"""Read-only inventory service for the API and report consumers, not UI rendering."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from crawler_coverage_auditor.validation.publication import enrich_archive_rows


def _connect_readonly(database_path: Path) -> sqlite3.Connection:
    """Open an existing audit database without allowing dashboard-side writes."""
    path = database_path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Audit database not found: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('BEGIN')  # Coherent read snapshot across related counters.
    return connection


def list_runs(database_path: Path) -> list[dict[str, Any]]:
    """Return run cards for the dashboard selector."""
    with closing(_connect_readonly(database_path)) as connection:
        capture = ' AND u.capture_enabled=1' if 'capture_enabled' in {r[1] for r in connection.execute('PRAGMA table_info(urls)')} else ''
        rows = connection.execute(f"""
            SELECT r.*,
                   COUNT(u.id) AS discovered_urls,
                   SUM(CASE WHEN (h.validity = 'VALID_PAGE' OR h.validity='URL_ONLY' AND u.page_type IN ('DOCUMENT','RESOURCE')) AND u.page_type != 'IMAGE' THEN 1 ELSE 0 END) AS valid_urls,
                   SUM(CASE WHEN (h.validity = 'VALID_PAGE' OR h.validity='URL_ONLY' AND u.page_type IN ('DOCUMENT','RESOURCE')) AND u.page_type != 'IMAGE' AND c.match_status IN
                       ('EXACT_MATCH','NORMALIZED_MATCH','CANONICAL_MATCH','REDIRECT_MATCH','FOUND_EXACT','FOUND_NORMALIZED','FOUND_CANONICAL','FOUND_REDIRECT')
                       THEN 1 ELSE 0 END) AS found_urls,
                   SUM(CASE WHEN (h.validity = 'VALID_PAGE' OR h.validity='URL_ONLY' AND u.page_type IN ('DOCUMENT','RESOURCE')) AND u.page_type != 'IMAGE' AND c.match_status='NOT_FOUND' THEN 1 ELSE 0 END) AS missing_urls
            FROM audit_runs r
            LEFT JOIN urls u ON u.audit_run_id = r.id {capture}
            LEFT JOIN http_results h ON h.audit_run_id = u.audit_run_id AND h.normalized_url = u.normalized_url
            LEFT JOIN corpus_matches c ON c.audit_run_id = u.audit_run_id AND c.normalized_url = u.normalized_url
            GROUP BY r.id ORDER BY r.id DESC
        """).fetchall()
    runs: list[dict[str, Any]] = []
    for row in rows:
        run = dict(row)
        for key in ("discovered_urls", "valid_urls", "found_urls", "missing_urls"):
            run[key] = int(run[key] or 0)
        run["config"] = json.loads(run.pop("config_json") or "{}")
        runs.append(run)
    return runs


def load_report_rows(database_path: Path, run_id: int) -> list[dict[str, Any]]:
    """Load URL-level audit evidence for a selected run."""
    with closing(_connect_readonly(database_path)) as connection:
        navigation = "u.navigation_depth" if "navigation_depth" in {row[1] for row in connection.execute("PRAGMA table_info(urls)")} else "NULL AS navigation_depth"
        capture = ' AND u.capture_enabled=1' if 'capture_enabled' in {r[1] for r in connection.execute('PRAGMA table_info(urls)')} else ''
        columns = {row[1] for row in connection.execute('PRAGMA table_info(corpus_matches)')}
        provider = 'c.provider, c.evidence_json' if 'provider' in columns and 'evidence_json' in columns else 'NULL AS provider, NULL AS evidence_json'
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        content = 'e.evidence_json AS content_evidence_json' if 'content_evidence' in tables else 'NULL AS content_evidence_json'
        content_join = 'LEFT JOIN content_evidence e ON e.audit_run_id=u.audit_run_id AND e.normalized_url=u.normalized_url' if 'content_evidence' in tables else ''
        rows = connection.execute(f"""
            SELECT u.url, u.normalized_url, u.canonical_url, u.page_type, u.min_depth,
                   {navigation}, u.discovery_methods, u.parent_url, u.anchor_text, u.state,
                   h.status_code, h.final_url, h.content_type, h.validity, h.title,
                   h.h1, h.text_length, h.fingerprint, h.error AS http_error, h.fetched_at,
                   c.match_status, c.corpus_url, c.document_id, c.similarity,
                   c.text_status, c.corpus_text_length, c.checked_at, {provider}, {content}
            FROM urls u
            LEFT JOIN http_results h ON h.audit_run_id = u.audit_run_id AND h.normalized_url = u.normalized_url
            LEFT JOIN corpus_matches c ON c.audit_run_id = u.audit_run_id AND c.normalized_url = u.normalized_url
            {content_join}
            WHERE u.audit_run_id = ? {capture} ORDER BY u.min_depth, u.url
        """, (run_id,)).fetchall()
        def snapshot(url):
            if 'page_snapshots' not in tables:
                return None
            saved = connection.execute('SELECT cleaned_text FROM page_snapshots WHERE audit_run_id=? AND normalized_url=?', (run_id, url)).fetchone()
            return saved[0] if saved else None
        return enrich_archive_rows(rows, snapshot)


def load_run_sections(database_path: Path, run_id: int) -> dict[str, list[dict[str, Any]]]:
    """Load compact discovery and diagnostics tables for dashboard tabs."""
    queries = {
        "pagination": "SELECT series_key,template_url,parameter_name,detected_from,observed_max,max_page,status,evidence FROM pagination_series WHERE audit_run_id = ? ORDER BY template_url",
        "filters": "SELECT d.source_url,d.control_name,d.control_type,d.form_action,d.form_method,v.display_value,v.request_value FROM filter_definitions d LEFT JOIN filter_values v ON v.audit_run_id=d.audit_run_id AND v.source_url=d.source_url AND v.control_name=d.control_name WHERE d.audit_run_id=? ORDER BY d.source_url,d.control_name,v.display_value",
        "documents": "SELECT normalized_url, extension, mime_type, size_bytes, parent_url FROM documents WHERE audit_run_id = ? ORDER BY normalized_url",
        "errors": "SELECT stage, url, message, created_at FROM errors WHERE audit_run_id = ? ORDER BY id DESC",
        "sitemaps": "SELECT sitemap_url, parent_sitemap, sitemap_type, url_count FROM sitemaps WHERE audit_run_id = ? ORDER BY sitemap_url",
        "network": "SELECT source_url, request_url, method, resource_type, status_code FROM network_requests WHERE audit_run_id = ? ORDER BY source_url, request_url",
        "limits": "SELECT stage,url,reason,last_successful_url,created_at FROM audit_limits WHERE audit_run_id=? ORDER BY id DESC",
        "content": "SELECT normalized_url,evidence_json FROM content_evidence WHERE audit_run_id=?",
    }
    with closing(_connect_readonly(database_path)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: [dict(row) for row in connection.execute(query, (run_id,)).fetchall()] if
                (name != "limits" or "audit_limits" in tables) and (name != "content" or "content_evidence" in tables) else [] for name, query in queries.items()}


def load_events(database_path: Path, run_id: int, limit: int = 200) -> list[dict[str, Any]]:
    with closing(_connect_readonly(database_path)) as connection:
        if not connection.execute("SELECT name FROM sqlite_master WHERE name='audit_events'").fetchone():
            return []
        rows = connection.execute("SELECT * FROM audit_events WHERE audit_run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit)).fetchall()
    return [dict(row) for row in rows]

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.validation.publication import enrich_archive_rows
from crawler_coverage_auditor.services.checkpoints import SCHEMA as CHECKPOINT_SCHEMA


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS audit_runs (
 id INTEGER PRIMARY KEY, target_url TEXT NOT NULL, corpus_url TEXT NOT NULL,
 started_at TEXT NOT NULL, completed_at TEXT, config_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'RUNNING', phase TEXT NOT NULL DEFAULT 'Starting', failure TEXT
);
CREATE TABLE IF NOT EXISTS urls (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
 url TEXT NOT NULL, normalized_url TEXT NOT NULL, final_url TEXT, canonical_url TEXT,
 page_type TEXT NOT NULL DEFAULT 'HTML', min_depth INTEGER NOT NULL, navigation_depth INTEGER,
 discovery_methods TEXT NOT NULL, parent_url TEXT, anchor_text TEXT,
 state TEXT NOT NULL DEFAULT 'PENDING', UNIQUE(audit_run_id, normalized_url)
);
CREATE TABLE IF NOT EXISTS url_edges (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
 source_url TEXT NOT NULL, target_url TEXT NOT NULL, anchor_text TEXT, method TEXT NOT NULL,
 UNIQUE(audit_run_id, source_url, target_url, method)
);
CREATE TABLE IF NOT EXISTS http_results (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
 normalized_url TEXT NOT NULL, status_code INTEGER, final_url TEXT, content_type TEXT,
 title TEXT, h1 TEXT, text_length INTEGER, fingerprint TEXT, validity TEXT, error TEXT,
 fetched_at TEXT NOT NULL, UNIQUE(audit_run_id, normalized_url)
);
CREATE TABLE IF NOT EXISTS page_snapshots (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
 normalized_url TEXT NOT NULL, cleaned_text TEXT NOT NULL, word_count INTEGER NOT NULL,
 UNIQUE(audit_run_id, normalized_url)
);
CREATE TABLE IF NOT EXISTS sitemaps (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
 sitemap_url TEXT NOT NULL, parent_sitemap TEXT, sitemap_type TEXT, url_count INTEGER DEFAULT 0,
 UNIQUE(audit_run_id, sitemap_url)
);
CREATE TABLE IF NOT EXISTS corpus_matches (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
 normalized_url TEXT NOT NULL, match_status TEXT NOT NULL, corpus_url TEXT,
 document_id TEXT, corpus_text_length INTEGER, similarity REAL, text_status TEXT, checked_at TEXT NOT NULL,
 UNIQUE(audit_run_id, normalized_url)
);
CREATE TABLE IF NOT EXISTS documents (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
 normalized_url TEXT NOT NULL, parent_url TEXT, extension TEXT, mime_type TEXT, size_bytes INTEGER,
 UNIQUE(audit_run_id, normalized_url)
);
CREATE TABLE IF NOT EXISTS pagination_series (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
 series_key TEXT NOT NULL, template_url TEXT NOT NULL, parameter_name TEXT NOT NULL,
 detected_from TEXT NOT NULL, observed_max INTEGER NOT NULL DEFAULT 1, max_page INTEGER,
 status TEXT NOT NULL DEFAULT 'DETECTED', evidence TEXT, UNIQUE(audit_run_id, series_key)
);
CREATE TABLE IF NOT EXISTS filter_definitions (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id), source_url TEXT NOT NULL,
 control_name TEXT NOT NULL, control_type TEXT NOT NULL, form_action TEXT, form_method TEXT,
 selector_hint TEXT, UNIQUE(audit_run_id, source_url, control_name, control_type)
);
CREATE TABLE IF NOT EXISTS filter_values (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id), source_url TEXT NOT NULL,
 control_name TEXT NOT NULL, display_value TEXT, request_value TEXT NOT NULL,
 UNIQUE(audit_run_id, source_url, control_name, request_value)
);
CREATE TABLE IF NOT EXISTS network_requests (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id), source_url TEXT NOT NULL,
 request_url TEXT NOT NULL, method TEXT, resource_type TEXT, status_code INTEGER,
 UNIQUE(audit_run_id, source_url, request_url, method)
);
CREATE TABLE IF NOT EXISTS pagination_pages (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
 series_key TEXT NOT NULL, page_number INTEGER NOT NULL, normalized_url TEXT NOT NULL,
 validity TEXT, fingerprint TEXT, UNIQUE(audit_run_id, series_key, page_number)
);
CREATE TABLE IF NOT EXISTS errors (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
 stage TEXT NOT NULL, url TEXT, message TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
 created_at TEXT NOT NULL, stage TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
 url TEXT, depth INTEGER, message TEXT NOT NULL, details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_by_run ON audit_events(audit_run_id,id);
CREATE TABLE IF NOT EXISTS audit_limits (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
 stage TEXT NOT NULL, url TEXT, reason TEXT NOT NULL, last_successful_url TEXT,
 created_at TEXT NOT NULL, UNIQUE(audit_run_id, stage, url, reason)
);
CREATE TABLE IF NOT EXISTS content_evidence (
 audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id), normalized_url TEXT NOT NULL,
 evidence_json TEXT NOT NULL, PRIMARY KEY(audit_run_id,normalized_url)
);
CREATE TABLE IF NOT EXISTS pagination_tasks (
 audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id), source_url TEXT NOT NULL,
 depth INTEGER NOT NULL, hints_json TEXT NOT NULL, PRIMARY KEY(audit_run_id,source_url)
);
"""


class AuditDatabase:
    def __init__(self, path: Path) -> None:
        self._discovery_batch_depth = 0
        self._checkpoint_operations = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(SCHEMA)
        self.connection.executescript(CHECKPOINT_SCHEMA)
        self._ensure_column('scan_checkpoints', 'paused_at', 'TEXT')
        self._ensure_column('scan_checkpoints', 'pause_reason', 'TEXT')
        self._ensure_column('pagination_probe_checkpoints', 'captured_at', 'TEXT')
        self._ensure_column("corpus_matches", "text_status", "TEXT")
        self._ensure_column("corpus_matches", "provider", "TEXT")
        self._ensure_column("corpus_matches", "evidence_json", "TEXT")
        self.connection.execute('CREATE TABLE IF NOT EXISTS corpus_match_history AS SELECT * FROM corpus_matches WHERE 0')
        self._ensure_column("audit_runs", "status", "TEXT")
        self._ensure_column("audit_runs", "phase", "TEXT")
        self._ensure_column("audit_runs", "failure", "TEXT")
        self._ensure_column("audit_runs", "discovery_completed_at", "TEXT")
        self._ensure_column("audit_runs", "verification_started_at", "TEXT")
        self._ensure_column("audit_runs", "verification_completed_at", "TEXT")
        self._ensure_column("urls", "navigation_depth", "INTEGER")
        self._ensure_column("urls", "capture_enabled", "INTEGER NOT NULL DEFAULT 1")
        self._ensure_column("urls", "host", "TEXT")
        self._ensure_column("urls", "first_discovery_method", "TEXT")
        self._ensure_column("urls", "discovered_at", "TEXT")
        from urllib.parse import urlsplit
        self.connection.executemany('UPDATE urls SET host=? WHERE id=?',
            [((urlsplit(row['normalized_url']).hostname or '').lower(), row['id'])
             for row in self.connection.execute('SELECT id,normalized_url FROM urls WHERE host IS NULL')])
        # Historical method sets were sorted, so their first source is unknowable.
        for statement in (
            'CREATE INDEX IF NOT EXISTS urls_run_state ON urls(audit_run_id,state)',
            'CREATE INDEX IF NOT EXISTS urls_run_pending_depth ON urls(audit_run_id,state,min_depth,id)',
            'CREATE INDEX IF NOT EXISTS urls_run_host_type ON urls(audit_run_id,host,page_type)',
            'CREATE INDEX IF NOT EXISTS urls_run_depth ON urls(audit_run_id,navigation_depth)',
            'CREATE INDEX IF NOT EXISTS matches_run_status ON corpus_matches(audit_run_id,match_status)',
            'CREATE INDEX IF NOT EXISTS http_run_validity ON http_results(audit_run_id,validity)',
            'CREATE INDEX IF NOT EXISTS events_run_severity_id ON audit_events(audit_run_id,severity,id)',
            'CREATE INDEX IF NOT EXISTS runs_target_id ON audit_runs(target_url,id)'):
            self.connection.execute(statement)
        self.connection.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        """Keep resume databases from older auditor versions compatible."""
        columns = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self.connection.close()

    def _checkpoint(self) -> None:
        """Keep normal writes immediate; group synchronous discovery writes only."""
        self._checkpoint_operations += 1
        if not self._discovery_batch_depth or self._checkpoint_operations >= 100:
            self.connection.commit()
            self._checkpoint_operations = 0

    @contextmanager
    def discovery_batch(self):
        """Short local write checkpoints, never held across network/browser awaits.

        One hundred storage operations is a durability checkpoint, not a URL
        limit. Earlier checkpoints survive an error; the current chunk rolls
        back. The source stays FETCHING/RENDERING until discovery finishes so
        an interrupted page can be replayed safely on resume.
        """
        self._discovery_batch_depth += 1
        try:
            yield
        except BaseException:
            self.connection.rollback()
            self._checkpoint_operations = 0
            raise
        finally:
            self._discovery_batch_depth -= 1
            if not self._discovery_batch_depth:
                self.connection.commit()
                self._checkpoint_operations = 0

    def create_run(self, target_url: str, corpus_url: str, config: dict[str, Any]) -> int:
        now = datetime.now(UTC).isoformat()
        cursor = self.connection.execute(
            "INSERT INTO audit_runs(target_url, corpus_url, started_at, config_json) VALUES (?, ?, ?, ?)",
            (target_url, corpus_url, now, json.dumps(config, default=lambda value: sorted(value) if isinstance(value, set) else str(value))),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def latest_resumable_run(self, target_url: str) -> int | None:
        row = self.connection.execute(
            "SELECT id FROM audit_runs WHERE target_url=? AND (completed_at IS NULL OR status IN ('CORPUS_PAUSED','CANCELLED','FAILED','AWAITING_CORPUS')) ORDER BY id DESC LIMIT 1", (target_url,)
        ).fetchone()
        return int(row["id"]) if row else None

    def latest_completed_run(self, target_url: str) -> int | None:
        row = self.connection.execute(
            "SELECT id FROM audit_runs WHERE target_url=? AND completed_at IS NOT NULL ORDER BY id DESC LIMIT 1", (target_url,)
        ).fetchone()
        return int(row["id"]) if row else None

    def finish_run(self, run_id: int, status: str = "COMPLETED", failure: str | None = None) -> None:
        self.connection.execute("UPDATE audit_runs SET completed_at=?,status=?,phase=?,failure=? WHERE id=?",
                                (datetime.now(UTC).isoformat(), status, "Finished", failure, run_id))
        self.connection.commit()

    def set_phase(self, run_id: int, phase: str) -> None:
        self.connection.execute("UPDATE audit_runs SET phase=? WHERE id=?", (phase, run_id))
        self.connection.commit()

    def inventory_ready(self, run_id: int) -> None:
        self.connection.execute("UPDATE audit_runs SET discovery_completed_at=?,completed_at=NULL,status='AWAITING_CORPUS',phase='Inventory ready · waiting for corpus check',failure=NULL WHERE id=?",
                                (datetime.now(UTC).isoformat(), run_id))
        self.connection.commit()

    def start_verification(self, run_id: int) -> None:
        self.connection.execute("UPDATE audit_runs SET verification_started_at=COALESCE(verification_started_at,?),completed_at=NULL,status='RUNNING',phase='Checking corpus',failure=NULL WHERE id=?",
                                (datetime.now(UTC).isoformat(), run_id))
        self.connection.commit()

    def finish_verification(self, run_id: int, status: str) -> None:
        if status in {'COMPLETED', 'LIMITED'}:
            self.connection.execute("UPDATE audit_runs SET verification_completed_at=? WHERE id=?", (datetime.now(UTC).isoformat(), run_id))
        self.finish_run(run_id, status)

    def event(self, run_id: int, stage: str, kind: str, message: str, url: str | None = None,
              depth: int | None = None, severity: str = "INFO", **details: Any) -> None:
        self.connection.execute("""INSERT INTO audit_events
            (audit_run_id,created_at,stage,kind,severity,url,depth,message,details_json) VALUES(?,?,?,?,?,?,?,?,?)""",
            (run_id, datetime.now(UTC).isoformat(), stage, kind, severity, url, depth, message, json.dumps(details)))
        self._checkpoint()

    def limit(self, run_id: int, stage: str, url: str | None, reason: str, last_successful: str | None = None) -> None:
        self.connection.execute("""INSERT OR IGNORE INTO audit_limits
            (audit_run_id,stage,url,reason,last_successful_url,created_at) VALUES(?,?,?,?,?,?)""",
            (run_id, stage, url, reason, last_successful, datetime.now(UTC).isoformat()))
        self._checkpoint()

    def content_evidence(self, run_id: int, url: str, evidence: dict) -> None:
        previous = self.connection.execute('SELECT evidence_json FROM content_evidence WHERE audit_run_id=? AND normalized_url=?', (run_id, url)).fetchone()
        if previous:
            evidence = json.loads(previous[0]) | evidence
        self.connection.execute("INSERT OR REPLACE INTO content_evidence VALUES(?,?,?)", (run_id, url, json.dumps(evidence)))
        self.connection.commit()

    def set_canonical(self, run_id: int, url: str, canonical: str | None) -> None:
        self.connection.execute("UPDATE urls SET canonical_url=? WHERE audit_run_id=? AND normalized_url=?", (canonical, run_id, url))
        self.connection.commit()

    def set_page_type(self, run_id: int, url: str, page_type: str) -> None:
        self.connection.execute("UPDATE urls SET page_type=? WHERE audit_run_id=? AND normalized_url=?", (page_type, run_id, url))
        self.connection.commit()

    def set_capture_enabled(self, run_id: int, url: str, enabled: bool) -> None:
        self.connection.execute('UPDATE urls SET capture_enabled=? WHERE audit_run_id=? AND normalized_url=?', (int(enabled), run_id, url))
        self.connection.commit()

    def add_url(self, run_id: int, item: DiscoveredURL) -> bool:
        navigational = item.discovery_method in {"SEED", "STATIC", "JS_DISCOVERY", "API_DISCOVERY", "FILTER_GET", "PAGINATION", "STRUCTURED_DATA", "FEED", "HTTP_LINK"}
        row = self.connection.execute(
            "SELECT id, min_depth, navigation_depth, discovery_methods FROM urls WHERE audit_run_id=? AND normalized_url=?", (run_id, item.normalized_url)
        ).fetchone()
        if row:
            previous_methods = set(json.loads(row["discovery_methods"]))
            methods = previous_methods | {item.discovery_method}
            if item.depth < row['min_depth'] or methods != previous_methods:
                self.connection.execute(
                    "UPDATE urls SET min_depth=?, discovery_methods=? WHERE id=?",
                    (min(row["min_depth"], item.depth), json.dumps(sorted(methods)), row["id"]),
                )
            if navigational and (row['navigation_depth'] is None or item.depth < row['navigation_depth']):
                self.connection.execute("UPDATE urls SET navigation_depth=MIN(COALESCE(navigation_depth,?),?) WHERE id=?", (item.depth, item.depth, row["id"]))
                if item.parent_url and (row["navigation_depth"] is None or item.depth < row["navigation_depth"]):
                    self.connection.execute("UPDATE urls SET parent_url=? WHERE id=?", (item.parent_url, row["id"]))
            if item.parent_url:
                self.connection.execute("INSERT OR IGNORE INTO url_edges(audit_run_id,source_url,target_url,anchor_text,method) VALUES(?,?,?,?,?)",
                                        (run_id, item.parent_url, item.normalized_url, item.anchor_text, item.discovery_method))
            self._checkpoint()
            return False
        self.connection.execute(
            "INSERT INTO urls(audit_run_id,url,normalized_url,page_type,min_depth,navigation_depth,discovery_methods,parent_url,anchor_text,capture_enabled,host,first_discovery_method,discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, item.url, item.normalized_url, item.page_type, item.depth, item.depth if navigational else None, json.dumps([item.discovery_method]), item.parent_url, item.anchor_text, int(item.capture_enabled),
             __import__('urllib.parse', fromlist=['urlsplit']).urlsplit(item.normalized_url).hostname, item.discovery_method, datetime.now(UTC).isoformat()),
        )
        if item.parent_url:
            self.connection.execute(
                "INSERT OR IGNORE INTO url_edges(audit_run_id,source_url,target_url,anchor_text,method) VALUES(?,?,?,?,?)",
                (run_id, item.parent_url, item.normalized_url, item.anchor_text, item.discovery_method),
            )
        self._checkpoint()
        return True

    def reconcile_navigation_depth(self, run_id: int) -> None:
        """Shortest actual website path from seed edges, excluding sitemap membership edges."""
        from collections import defaultdict, deque
        graph = defaultdict(list)
        for source, target in self.connection.execute("SELECT source_url,target_url FROM url_edges WHERE audit_run_id=? AND method!='SITEMAP'", (run_id,)):
            graph[source].append(target)
        seeds = [row[0] for row in self.connection.execute("SELECT normalized_url FROM urls WHERE audit_run_id=? AND discovery_methods LIKE '%SEED%'", (run_id,))]
        if not seeds:
            return
        depths = {seed: 0 for seed in seeds}
        queue = deque(seeds)
        while queue:
            source = queue.popleft()
            for target in graph[source]:
                if target not in depths:
                    depths[target] = depths[source] + 1
                    queue.append(target)
        self.connection.execute("UPDATE urls SET navigation_depth=NULL WHERE audit_run_id=?", (run_id,))
        self.connection.executemany("UPDATE urls SET navigation_depth=? WHERE audit_run_id=? AND normalized_url=?", [(depth, run_id, url) for url, depth in depths.items()])
        self.connection.commit()

    def pending_urls(self, run_id: int, *, limit: int | None = None) -> list[sqlite3.Row]:
        statement = "SELECT * FROM urls WHERE audit_run_id=? AND state='PENDING' ORDER BY min_depth, id"
        return self.connection.execute(statement + (' LIMIT ?' if limit is not None else ''),
            (run_id, limit) if limit is not None else (run_id,)).fetchall()

    def set_url_state(self, run_id: int, normalized_url: str, state: str) -> None:
        self.connection.execute("UPDATE urls SET state=? WHERE audit_run_id=? AND normalized_url=?", (state, run_id, normalized_url))
        self.connection.commit()

    def add_http_result(self, run_id: int, normalized_url: str, **values: Any) -> None:
        fetched_at = values.pop('fetched_at', None) or datetime.now(UTC).isoformat()
        columns = ["audit_run_id", "normalized_url", *values.keys(), "fetched_at"]
        placeholders = ",".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT OR REPLACE INTO http_results({','.join(columns)}) VALUES({placeholders})",
            (run_id, normalized_url, *values.values(), fetched_at),
        )
        self.connection.commit()

    def add_page_snapshot(self, run_id: int, normalized_url: str, cleaned_text: str) -> None:
        self.connection.execute("INSERT OR REPLACE INTO page_snapshots(audit_run_id,normalized_url,cleaned_text,word_count) VALUES(?,?,?,?)",
                                (run_id, normalized_url, cleaned_text, len(cleaned_text.split())))
        self.connection.commit()

    def page_snapshot(self, run_id: int, normalized_url: str) -> str | None:
        row = self.connection.execute("SELECT cleaned_text FROM page_snapshots WHERE audit_run_id=? AND normalized_url=?",
                                      (run_id, normalized_url)).fetchone()
        return str(row["cleaned_text"]) if row else None

    def add_sitemap(self, run_id: int, url: str, parent: str | None, sitemap_type: str, count: int) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO sitemaps(audit_run_id,sitemap_url,parent_sitemap,sitemap_type,url_count) VALUES(?,?,?,?,?)",
            (run_id, url, parent, sitemap_type, count),
        )
        if not cursor.rowcount:
            # An unfinished sitemap can change between pause and resume. Keep
            # its latest parsed entry count accurate without adding another row.
            self.connection.execute('''UPDATE sitemaps SET sitemap_type=?,url_count=?
                WHERE audit_run_id=? AND sitemap_url=? AND (sitemap_type!=? OR url_count!=?)''',
                (sitemap_type, count, run_id, url, sitemap_type, count))
        self.connection.commit()
        return cursor.rowcount > 0

    def add_document(self, run_id: int, url: str, parent_url: str | None, extension: str) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO documents(audit_run_id,normalized_url,parent_url,extension) VALUES(?,?,?,?)",
            (run_id, url, parent_url, extension),
        )
        self._checkpoint()

    def add_filter_definition(self, run_id: int, source_url: str, control_name: str, control_type: str,
                              form_action: str | None, form_method: str | None, selector_hint: str | None) -> None:
        self.connection.execute("""INSERT OR IGNORE INTO filter_definitions
            (audit_run_id,source_url,control_name,control_type,form_action,form_method,selector_hint) VALUES(?,?,?,?,?,?,?)""",
            (run_id, source_url, control_name, control_type, form_action, form_method, selector_hint))
        self._checkpoint()

    def add_filter_value(self, run_id: int, source_url: str, control_name: str, display_value: str | None,
                         request_value: str) -> None:
        self.connection.execute("INSERT OR IGNORE INTO filter_values(audit_run_id,source_url,control_name,display_value,request_value) VALUES(?,?,?,?,?)",
                                (run_id, source_url, control_name, display_value, request_value))
        self._checkpoint()

    def add_network_request(self, run_id: int, source_url: str, request_url: str, method: str | None,
                            resource_type: str | None, status_code: int | None) -> None:
        self.connection.execute("INSERT OR IGNORE INTO network_requests(audit_run_id,source_url,request_url,method,resource_type,status_code) VALUES(?,?,?,?,?,?)",
                                (run_id, source_url, request_url, method, resource_type, status_code))
        self._checkpoint()

    def upsert_pagination_series(self, run_id: int, series_key: str, template_url: str, parameter_name: str,
                                 detected_from: str, observed_max: int, max_page: int | None = None,
                                 status: str = "DETECTED", evidence: str | None = None) -> None:
        self.connection.execute("""
            INSERT INTO pagination_series(audit_run_id,series_key,template_url,parameter_name,detected_from,observed_max,max_page,status,evidence)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(audit_run_id,series_key) DO UPDATE SET
              observed_max=MAX(pagination_series.observed_max, excluded.observed_max),
              max_page=CASE WHEN excluded.status IN ('LIMITED','UNVERIFIED') THEN NULL ELSE COALESCE(excluded.max_page,pagination_series.max_page) END,
              status=CASE
                WHEN pagination_series.status IN ('VERIFIED','LIMITED','UNVERIFIED') AND excluded.status='DETECTED' THEN pagination_series.status
                ELSE excluded.status
              END,
              evidence=COALESCE(excluded.evidence,pagination_series.evidence)
        """, (run_id, series_key, template_url, parameter_name, detected_from, observed_max, max_page, status, evidence))
        self.connection.commit()

    def add_pagination_page(self, run_id: int, series_key: str, page_number: int, normalized_url: str,
                            validity: str | None, fingerprint: str | None) -> None:
        self.connection.execute("""
            INSERT INTO pagination_pages(audit_run_id,series_key,page_number,normalized_url,validity,fingerprint)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(audit_run_id,series_key,page_number) DO UPDATE SET
              normalized_url=excluded.normalized_url, validity=excluded.validity, fingerprint=excluded.fingerprint
        """, (run_id, series_key, page_number, normalized_url, validity, fingerprint))
        self.connection.commit()

    def pagination_rows(self, run_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM pagination_series WHERE audit_run_id=? ORDER BY template_url", (run_id,)).fetchall()

    def filter_rows(self, run_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("""
            SELECT d.source_url,d.control_name,d.control_type,d.form_action,d.form_method,d.selector_hint,
                   v.display_value,v.request_value
            FROM filter_definitions d LEFT JOIN filter_values v
              ON v.audit_run_id=d.audit_run_id AND v.source_url=d.source_url AND v.control_name=d.control_name
            WHERE d.audit_run_id=? ORDER BY d.source_url,d.control_name,v.display_value
        """, (run_id,)).fetchall()

    def document_rows(self, run_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM documents WHERE audit_run_id=? ORDER BY normalized_url", (run_id,)).fetchall()

    def add_error(self, run_id: int, stage: str, url: str | None, message: str) -> None:
        self.connection.execute("INSERT INTO errors(audit_run_id,stage,url,message,created_at) VALUES(?,?,?,?,?)",
                                (run_id, stage, url, message, datetime.now(UTC).isoformat()))
        self.connection.commit()

    def add_corpus_match(self, run_id: int, normalized_url: str, status: str, corpus_url: str | None,
                         document_id: str | None, corpus_text_length: int | None, similarity: float | None,
                         text_status: str | None = None, *, provider: str | None = None, evidence: dict | None = None) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO corpus_matches(audit_run_id,normalized_url,match_status,corpus_url,document_id,corpus_text_length,similarity,text_status,checked_at,provider,evidence_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, normalized_url, status, corpus_url, document_id, corpus_text_length, similarity, text_status, datetime.now(UTC).isoformat(), provider, json.dumps(evidence) if evidence else None),
        )
        self.connection.commit()

    def report_rows(self, run_id: int, *, include_traversal: bool = False, only_url: str | None = None, enrich_archives: bool = True) -> list[dict]:
        capture_clause = '' if include_traversal else ' AND u.capture_enabled=1'
        url_clause = ' AND u.normalized_url=?' if only_url is not None else ''
        rows = self.connection.execute(f"""
            SELECT u.url,u.normalized_url,u.canonical_url,u.page_type,u.min_depth,u.navigation_depth,u.discovery_methods,u.parent_url,u.anchor_text,
                   h.status_code,h.final_url,h.validity,h.title,h.h1,h.text_length,h.content_type,h.error,h.fetched_at,u.state,
                   c.match_status,c.corpus_url,c.document_id,c.similarity,c.text_status,c.corpus_text_length,c.provider,c.evidence_json,
                   e.evidence_json AS content_evidence_json
            FROM urls u LEFT JOIN http_results h ON h.audit_run_id=u.audit_run_id AND h.normalized_url=u.normalized_url
            LEFT JOIN corpus_matches c ON c.audit_run_id=u.audit_run_id AND c.normalized_url=u.normalized_url
            LEFT JOIN content_evidence e ON e.audit_run_id=u.audit_run_id AND e.normalized_url=u.normalized_url
            WHERE u.audit_run_id=? {capture_clause} {url_clause} ORDER BY u.min_depth,u.url
        """, (run_id, only_url) if only_url is not None else (run_id,)).fetchall()
        if not enrich_archives:
            return [dict(row) for row in rows]
        return enrich_archive_rows(rows, lambda url: self.page_snapshot(run_id, url))

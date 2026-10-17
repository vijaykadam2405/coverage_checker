"""Durable stage checkpoints; no credentials, browser storage or Corpus bodies."""
import json
from datetime import UTC, datetime

from crawler_coverage_auditor.validation.http import FetchResult

SCHEMA = '''
CREATE TABLE IF NOT EXISTS scan_checkpoints (
 audit_run_id INTEGER PRIMARY KEY REFERENCES audit_runs(id),
 current_url TEXT, stage TEXT NOT NULL DEFAULT 'QUEUE', last_completed_url TEXT,
 sitemap_initialized INTEGER NOT NULL DEFAULT 0, sitemap_completed INTEGER NOT NULL DEFAULT 0,
 updated_at TEXT NOT NULL, paused_at TEXT, pause_reason TEXT
);
CREATE TABLE IF NOT EXISTS url_checkpoints (
 audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id), normalized_url TEXT NOT NULL,
 website_completed INTEGER NOT NULL DEFAULT 0, browser_completed INTEGER NOT NULL DEFAULT 0,
 completed INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
 PRIMARY KEY(audit_run_id,normalized_url)
);
CREATE TABLE IF NOT EXISTS sitemap_checkpoints (
 audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id), url TEXT NOT NULL, parent_url TEXT,
 completed INTEGER NOT NULL DEFAULT 0, body_hash TEXT, next_entry INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(audit_run_id,url)
);
CREATE TABLE IF NOT EXISTS pagination_probe_checkpoints (
 audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id), url TEXT NOT NULL,
 final_url TEXT NOT NULL, status_code INTEGER NOT NULL, headers_json TEXT NOT NULL,
 body BLOB NOT NULL, captured_at TEXT, PRIMARY KEY(audit_run_id,url)
);
'''


class ScanProgress:
    def __init__(self, db, run_id):
        self.db, self.run_id = db, run_id
        created = db.connection.execute('INSERT OR IGNORE INTO scan_checkpoints(audit_run_id,updated_at) VALUES(?,?)',
                              (run_id, datetime.now(UTC).isoformat()))
        if created.rowcount and db.connection.execute("SELECT 1 FROM audit_events WHERE audit_run_id=? AND kind='PHASE' AND stage='Discovering website URLs' LIMIT 1", (run_id,)).fetchone():
            # Older scans have no explicit sitemap checkpoint. Their saved phase
            # transition proves setup finished; do not repeat it on upgrade.
            db.connection.execute('UPDATE scan_checkpoints SET sitemap_initialized=1,sitemap_completed=1 WHERE audit_run_id=?', (run_id,))
        db.connection.commit()

    def state(self):
        return dict(self.db.connection.execute('SELECT * FROM scan_checkpoints WHERE audit_run_id=?', (self.run_id,)).fetchone())

    def begin(self):
        self.db.connection.execute('UPDATE scan_checkpoints SET paused_at=NULL,pause_reason=NULL WHERE audit_run_id=?', (self.run_id,))
        self.db.connection.commit()

    def pause(self, reason):
        now = datetime.now(UTC).isoformat()
        self.db.connection.execute('UPDATE scan_checkpoints SET paused_at=?,pause_reason=?,updated_at=? WHERE audit_run_id=?',
                                   (now, reason, now, self.run_id))
        self.db.connection.commit()

    def url(self, url):
        row = self.db.connection.execute('SELECT * FROM url_checkpoints WHERE audit_run_id=? AND normalized_url=?',
                                        (self.run_id, url)).fetchone()
        return dict(row) if row else {'website_completed': 0, 'browser_completed': 0, 'completed': 0}

    def enter(self, url, stage):
        if stage.startswith('CORPUS'):
            self.db.connection.execute('UPDATE url_checkpoints SET completed=0 WHERE audit_run_id=? AND normalized_url=?', (self.run_id, url))
        self.db.connection.execute('UPDATE scan_checkpoints SET current_url=?,stage=?,updated_at=? WHERE audit_run_id=?',
            (url, stage, datetime.now(UTC).isoformat(), self.run_id))
        self.db.connection.commit()

    def mark(self, url, stage):
        if stage == 'COMPLETED' and self.url(url)['completed']:
            return
        columns = {'WEBSITE': 'website_completed', 'BROWSER': 'browser_completed', 'COMPLETED': 'completed'}
        column = columns[stage]
        now = datetime.now(UTC).isoformat()
        self.db.connection.execute(f'''INSERT INTO url_checkpoints(audit_run_id,normalized_url,{column},updated_at)
            VALUES(?,?,1,?) ON CONFLICT(audit_run_id,normalized_url) DO UPDATE SET {column}=1,updated_at=excluded.updated_at''',
            (self.run_id, url, now))
        if stage == 'COMPLETED':
            self.db.connection.execute("UPDATE urls SET state='DONE' WHERE audit_run_id=? AND normalized_url=?", (self.run_id, url))
            self.db.connection.execute("""UPDATE scan_checkpoints SET
                stage=CASE WHEN current_url=? THEN 'QUEUE' ELSE stage END,
                current_url=CASE WHEN current_url=? THEN NULL ELSE current_url END,
                last_completed_url=?,updated_at=? WHERE audit_run_id=?""",
                                       (url, url, url, now, self.run_id))
        else:
            next_stage = 'BROWSER' if stage == 'WEBSITE' else 'CORPUS_URL'
            self.db.connection.execute('UPDATE scan_checkpoints SET current_url=?,stage=?,updated_at=? WHERE audit_run_id=?',
                                       (url, next_stage, now, self.run_id))
        self.db.connection.commit()

    def finish_pause_history(self):
        if self.db.connection.execute("SELECT 1 FROM urls WHERE audit_run_id=? AND state IN ('PENDING','FETCHING','RENDERING','CHECKING_CORPUS') LIMIT 1", (self.run_id,)).fetchone():
            return []
        if self.db.connection.execute('SELECT 1 FROM pagination_tasks WHERE audit_run_id=? LIMIT 1', (self.run_id,)).fetchone():
            return []
        rows = [dict(row) for row in self.db.connection.execute("SELECT * FROM audit_limits WHERE audit_run_id=? AND stage='Audit' AND reason='Stopped by user; pending inventory and corpus checks remain'", (self.run_id,))]
        for row in rows:
            self.db.connection.execute("UPDATE audit_limits SET stage='Historical pause',reason=? WHERE id=?",
                (row['reason'] + f" (resolved checkpoint #{row['id']})", row['id']))
        self.db.connection.commit()
        return rows

    def preferred_pending(self):
        return self.db.connection.execute('''SELECT u.* FROM urls u JOIN scan_checkpoints s
            ON s.audit_run_id=u.audit_run_id AND s.current_url=u.normalized_url
            WHERE u.audit_run_id=? AND u.state='PENDING' ''', (self.run_id,)).fetchone()

    def restore(self):
        # Legacy DONE rows are left alone. New checkpoints distinguish a finished
        # HTTP stage from unfinished rendering, even if the process died between stages.
        self.db.connection.execute('''UPDATE urls SET state='PENDING' WHERE audit_run_id=? AND
            (state IN ('FETCHING','RENDERING') OR normalized_url IN
             (SELECT normalized_url FROM url_checkpoints WHERE audit_run_id=? AND completed=0 AND browser_completed=0)
             OR normalized_url IN (SELECT current_url FROM scan_checkpoints WHERE audit_run_id=? AND stage IN ('WEBSITE','BROWSER')))''',
             (self.run_id, self.run_id, self.run_id))
        self.db.connection.commit()

    def initialize_sitemaps(self, urls):
        with self.db.discovery_batch():
            for url in urls:
                self.enqueue_sitemap(url, None)
        self.db.connection.execute('UPDATE scan_checkpoints SET sitemap_initialized=1 WHERE audit_run_id=?', (self.run_id,))
        self.db.connection.commit()

    def enqueue_sitemap(self, url, parent):
        self.db.connection.execute('INSERT OR IGNORE INTO sitemap_checkpoints(audit_run_id,url,parent_url) VALUES(?,?,?)',
                                   (self.run_id, url, parent))
        self.db._checkpoint()

    def pending_sitemap(self):
        row = self.db.connection.execute('SELECT * FROM sitemap_checkpoints WHERE audit_run_id=? AND completed=0 ORDER BY rowid LIMIT 1',
                                        (self.run_id,)).fetchone()
        return dict(row) if row else None

    def sitemap_position(self, url, body_hash):
        row = self.db.connection.execute('SELECT body_hash,next_entry FROM sitemap_checkpoints WHERE audit_run_id=? AND url=?',
                                        (self.run_id, url)).fetchone()
        return row['next_entry'] if row and row['body_hash'] == body_hash else 0

    def sitemap_entry(self, url, body_hash, next_entry):
        self.db.connection.execute('UPDATE sitemap_checkpoints SET body_hash=?,next_entry=? WHERE audit_run_id=? AND url=?',
                                   (body_hash, next_entry, self.run_id, url))
        self.db._checkpoint()

    def finish_sitemap(self, url):
        self.db.connection.execute('UPDATE sitemap_checkpoints SET completed=1 WHERE audit_run_id=? AND url=?', (self.run_id, url))
        self.db.connection.commit()

    def finish_sitemaps(self):
        self.db.connection.execute('UPDATE scan_checkpoints SET sitemap_completed=1,current_url=NULL,stage=\'QUEUE\' WHERE audit_run_id=?', (self.run_id,))
        self.db.connection.commit()

    def save_probe(self, response):
        # Successful public HTML / terminal 404 responses only. A block/rate limit
        # must be retried, never cached as a final boundary. Linked files stay URL-only.
        if not isinstance(response, FetchResult) or response.error or response.url_only or response.status_code not in {200, 404, 410}:
            return
        mime = response.headers.get('content-type', '').split(';')[0].lower().strip()
        if mime not in {'text/html', 'application/xhtml+xml'}:
            return
        headers = {name: value for name, value in response.headers.items()
                   if name.lower() in {'content-type', 'server', 'link', 'cf-mitigated', 'x-amzn-waf-action'}}
        self.db.connection.execute('''INSERT OR REPLACE INTO pagination_probe_checkpoints
            (audit_run_id,url,final_url,status_code,headers_json,body,captured_at) VALUES(?,?,?,?,?,?,?)''',
            (self.run_id, response.requested_url, response.final_url, response.status_code, json.dumps(headers), response.content,
             response.captured_at or datetime.now(UTC).isoformat()))
        self.db.connection.commit()

    def probe(self, url):
        row = self.db.connection.execute('SELECT * FROM pagination_probe_checkpoints WHERE audit_run_id=? AND url=?',
                                        (self.run_id, url)).fetchone()
        return FetchResult(url, row['final_url'], row['status_code'], json.loads(row['headers_json']), row['body'], captured_at=row['captured_at']) if row else None


def checkpoint_snapshot(connection, run_id):
    """Bounded read-only API projection. Never expose cached HTML or cookie headers."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='scan_checkpoints'").fetchone():
        return {'available': False}
    row = connection.execute('SELECT * FROM scan_checkpoints WHERE audit_run_id=?', (run_id,)).fetchone()
    if not row:
        return {'available': False}
    labels = {'WEBSITE': 'Website discovery and validation', 'BROWSER': 'Browser discovery',
              'CORPUS_URL': 'Corpus URL check', 'CORPUS_TEXT': 'HTML text comparison',
              'SITEMAP': 'Sitemap discovery', 'PAGINATION': 'Pagination discovery', 'QUEUE': 'Next queued URL'}
    return {'available': True, 'current_url': row['current_url'], 'stage': row['stage'],
            'stage_label': labels.get(row['stage'], row['stage']), 'last_completed_url': row['last_completed_url'],
            'updated_at': row['updated_at'], 'paused_at': row['paused_at'], 'pause_reason': row['pause_reason'],
            'sitemap_completed': bool(row['sitemap_completed'])}

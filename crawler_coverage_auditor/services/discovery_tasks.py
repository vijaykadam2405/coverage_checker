"""Durable, versioned metadata work. One controller, no independent network workers."""

import json
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_tasks (
 id INTEGER PRIMARY KEY, audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id),
 kind TEXT NOT NULL, url TEXT NOT NULL, parent_url TEXT, depth INTEGER NOT NULL,
 version INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
 cursor_json TEXT NOT NULL DEFAULT '{}', evidence_json TEXT NOT NULL DEFAULT '{}',
 updated_at TEXT NOT NULL, UNIQUE(audit_run_id,kind,url,version)
);
CREATE INDEX IF NOT EXISTS discovery_task_queue ON discovery_tasks(audit_run_id,state,id);
CREATE TABLE IF NOT EXISTS discovery_asset_cache (
 audit_run_id INTEGER NOT NULL REFERENCES audit_runs(id), kind TEXT NOT NULL, url TEXT NOT NULL,
 version INTEGER NOT NULL, body_hash TEXT NOT NULL, result_json TEXT NOT NULL,
 PRIMARY KEY(audit_run_id,kind,url,version)
);
"""


class DiscoveryTasks:
    VERSION = 1

    def __init__(self, db, run_id):
        self.db, self.run_id = db, run_id

    def enqueue(self, kind, url, parent, depth):
        result = self.db.connection.execute(
            """INSERT OR IGNORE INTO discovery_tasks
            (audit_run_id,kind,url,parent_url,depth,version,updated_at) VALUES(?,?,?,?,?,?,?)""",
            (
                self.run_id,
                kind,
                url,
                parent,
                depth,
                self.VERSION,
                datetime.now(UTC).isoformat(),
            ),
        )
        self.db._checkpoint()
        return bool(result.rowcount)

    def restore(self, *, retry_failed=False):
        self.db.connection.execute(
            "UPDATE discovery_tasks SET state='PENDING' WHERE audit_run_id=? AND state='RUNNING'",
            (self.run_id,),
        )
        if retry_failed:
            self.db.connection.execute(
                "DELETE FROM discovery_asset_cache WHERE audit_run_id=? AND url IN (SELECT url FROM discovery_tasks WHERE audit_run_id=? AND state='FAILED')",
                (self.run_id, self.run_id),
            )
            self.db.connection.execute(
                "UPDATE discovery_tasks SET state='PENDING',cursor_json='{}' WHERE audit_run_id=? AND state='FAILED'",
                (self.run_id,),
            )
        self.db.connection.commit()

    def pending(self):
        row = self.db.connection.execute(
            "SELECT * FROM discovery_tasks WHERE audit_run_id=? AND state='PENDING' AND version=? ORDER BY id LIMIT 1",
            (self.run_id, self.VERSION),
        ).fetchone()
        return dict(row) if row else None

    def save(self, task, state, *, cursor=None, evidence=None):
        self.db.connection.execute(
            "UPDATE discovery_tasks SET state=?,cursor_json=?,evidence_json=?,updated_at=? WHERE id=?",
            (
                state,
                json.dumps(cursor or {}),
                json.dumps(evidence or {}),
                datetime.now(UTC).isoformat(),
                task["id"],
            ),
        )
        self.db._checkpoint()

    def cached(self, task):
        row = self.db.connection.execute(
            "SELECT result_json FROM discovery_asset_cache WHERE audit_run_id=? AND kind=? AND url=? AND version=?",
            (self.run_id, task["kind"], task["url"], task["version"]),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, task, body_hash, result):
        # Store extracted public URL evidence, not cookies, headers or API bodies.
        self.db.connection.execute(
            "INSERT OR REPLACE INTO discovery_asset_cache VALUES(?,?,?,?,?,?)",
            (
                self.run_id,
                task["kind"],
                task["url"],
                task["version"],
                body_hash,
                json.dumps(result),
            ),
        )
        self.db._checkpoint()


def task_summary(connection, run_id):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='discovery_tasks'"
    ).fetchone():
        return []
    return [
        dict(row)
        for row in connection.execute(
            """SELECT kind,state,count(*) AS count
        FROM discovery_tasks WHERE audit_run_id=? GROUP BY kind,state ORDER BY kind,state""",
            (run_id,),
        )
    ]


def source_work_remaining(connection, run_id):
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name='discovery_tasks'"
    ).fetchone():
        return False
    return bool(
        connection.execute(
            "SELECT 1 FROM discovery_tasks WHERE audit_run_id=? AND state IN ('PENDING','RUNNING','FAILED') LIMIT 1",
            (run_id,),
        ).fetchone()
    )

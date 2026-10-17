"""Recover discovery suppressed by historical count cutoffs, without deleting evidence."""
import sqlite3
from contextlib import closing
from pathlib import Path

HISTORICAL_COUNT_STAGE = 'Historical count cutoff'
RECOVERY_STARTED = 'COUNT_LIMIT_RECOVERY'
RECOVERY_FINISHED = 'COUNT_LIMIT_RECOVERY_COMPLETED'


def is_count_cutoff(stage: str, reason: str) -> bool:
    if stage.lower() not in {'inventory', 'browser', 'filters', 'pagination', 'sitemap'}:
        return False
    text = reason.lower()
    return any(marker in text for marker in (
        'inventory safeguard', 'rendered page safeguard', 'browser action budget',
        'value safeguard reached', 'exploration values; remaining values',
        'pagination probe safeguard', 'sitemap safeguard',
    ))


def count_cutoffs(connection, run_id):
    return [dict(row) for row in connection.execute(
        'SELECT id,stage,url,reason FROM audit_limits WHERE audit_run_id=?', (run_id,))
        if is_count_cutoff(row['stage'], row['reason'])]


def needs_count_recovery(database: Path, run_id: int) -> bool:
    """Read-only routing check; never starts a job or touches Corpus."""
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro', uri=True, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row['name'] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'audit_events', 'audit_limits'} <= tables:
            return False
        started = connection.execute('SELECT 1 FROM audit_events WHERE audit_run_id=? AND kind=? LIMIT 1',
            (run_id, RECOVERY_STARTED)).fetchone()
        return not started and bool(count_cutoffs(connection, run_id))


def reopen_count_limited_discovery(db, run_id):
    """Queue sources once, atomically with a checkpoint. File URLs remain URL-only."""
    if db.connection.execute('SELECT 1 FROM audit_events WHERE audit_run_id=? AND kind=? LIMIT 1',
            (run_id, RECOVERY_STARTED)).fetchone():
        return None
    limits = count_cutoffs(db.connection, run_id)
    if not limits:
        return None
    try:
        # Recovery is an explicit one-time replay of sources truncated by old
        # ceilings, not a normal pause/resume. Invalidate only those stage markers.
        db.connection.execute('''DELETE FROM url_checkpoints WHERE audit_run_id=? AND normalized_url IN
            (SELECT u.normalized_url FROM urls u JOIN http_results h
             ON h.audit_run_id=u.audit_run_id AND h.normalized_url=u.normalized_url
             WHERE u.audit_run_id=? AND u.state='DONE' AND u.page_type IN ('HTML','API','FEED')
             AND h.validity IN ('VALID_PAGE','EMPTY_PAGE','RENDER_REQUIRED','BLOCKED','BOT_BLOCKED','CAPTCHA_REQUIRED'))''', (run_id, run_id))
        if any(item['stage'].lower() == 'sitemap' for item in limits):
            db.connection.execute('DELETE FROM sitemap_checkpoints WHERE audit_run_id=?', (run_id,))
            db.connection.execute('UPDATE scan_checkpoints SET sitemap_initialized=0,sitemap_completed=0 WHERE audit_run_id=?', (run_id,))
        cursor = db.connection.execute("""UPDATE urls SET state='PENDING' WHERE audit_run_id=?
            AND state='DONE' AND page_type IN ('HTML','API','FEED') AND normalized_url IN
            (SELECT normalized_url FROM http_results WHERE audit_run_id=? AND validity IN
            ('VALID_PAGE','EMPTY_PAGE','RENDER_REQUIRED','BLOCKED','BOT_BLOCKED','CAPTCHA_REQUIRED'))""", (run_id, run_id))
        queued = cursor.rowcount
        db.connection.execute("""UPDATE audit_runs SET discovery_completed_at=NULL,completed_at=NULL,
            verification_completed_at=NULL,status='RUNNING' WHERE id=?""", (run_id,))
        # event() commits the queue update and durable marker together. Existing
        # snapshots, matches, documents and already pending URLs are untouched.
        db.event(run_id, 'Discovery recovery', RECOVERY_STARTED,
            f'Reopening {queued:,} processed source pages after old count cutoffs; saved results retained',
            requeued_sources=queued, original_boundaries=limits)
    except Exception:
        db.connection.rollback()
        raise
    return queued


def finish_count_recovery(db, run_id):
    if db.connection.execute("SELECT 1 FROM urls WHERE audit_run_id=? AND state IN ('PENDING','FETCHING','RENDERING','CHECKING_CORPUS') LIMIT 1", (run_id,)).fetchone():
        return []
    if db.connection.execute('SELECT 1 FROM pagination_tasks WHERE audit_run_id=? LIMIT 1', (run_id,)).fetchone():
        return []
    started = db.connection.execute('SELECT 1 FROM audit_events WHERE audit_run_id=? AND kind=? LIMIT 1',
        (run_id, RECOVERY_STARTED)).fetchone()
    finished = db.connection.execute('SELECT 1 FROM audit_events WHERE audit_run_id=? AND kind=? LIMIT 1',
        (run_id, RECOVERY_FINISHED)).fetchone()
    if not started or finished:
        return []
    limits = count_cutoffs(db.connection, run_id)
    try:
        for item in limits:
            db.connection.execute('UPDATE audit_limits SET stage=?,reason=? WHERE id=?',
                (HISTORICAL_COUNT_STAGE, item['stage']+': '+item['reason'], item['id']))
        db.event(run_id, 'Discovery recovery', RECOVERY_FINISHED,
            'Uncapped rediscovery work exhausted; previous count cutoffs retained as historical notes. Other boundaries still apply.',
            original_boundaries=limits)
    except Exception:
        db.connection.rollback()
        raise
    return limits

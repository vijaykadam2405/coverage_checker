"""Self-dedup + conditional re-fetch against our OWN crawl history (§7A).

The crawler dedups against *itself* — same URL, unchanged content => idempotent
re-crawl (do not re-emit). It does NOT merge different URLs about the same event
(that is L2's cross-source clustering).

Backed by a SQLite ``crawl_pages`` table keyed by canonical URL. For each page
we remember the last ``content_hash`` + HTTP validators (ETag / Last-Modified)
so the next run can (a) send conditional GETs and (b) skip unchanged content.

The classifier follows the reference crawler's golden rule: **a false
"changed" only wastes a re-crawl; a false "unchanged" loses data** — so every
uncertainty resolves to ``changed``.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import config

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default

Verdict = str  # "new" | "unchanged" | "changed" | "gone"

# Dropping this many QUEUED urls is loud. Below it a clear is routine (a small job re-crawling from
# its seed); above it, it is either a deliberate re-crawl of a large site or the bug that deleted
# 389,000 dvidshub URLs without a single log line.
_BIG_CLEAR = _env_int("CRAWLER_BIG_FRONTIER_CLEAR", 1000)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS crawl_pages (
    canonical_url TEXT PRIMARY KEY,
    content_hash  TEXT,
    etag          TEXT,
    last_modified TEXT,
    js_heavy      INTEGER DEFAULT 0,
    last_status   INTEGER,
    last_seen     TEXT,
    times_seen    INTEGER DEFAULT 0,
    error_category TEXT,
    fail_count    INTEGER DEFAULT 0,
    failed_at     TEXT,
    crawl_run_id  TEXT
);
CREATE TABLE IF NOT EXISTS crawl_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT,
    host            TEXT,
    fetched_at      TEXT,
    ua              TEXT,
    robots_decision TEXT,
    status          INTEGER,
    reason          TEXT,
    careful         INTEGER DEFAULT 0
);
-- Per-HOST render learning (auto mode): a host escalated to always-render (httpx kept coming up
-- empty) is remembered here so the NEXT run skips the wasted httpx probe and renders straight away.
-- Keyed by host; one row per host. Written when _note_render escalates, seeded at batch start.
CREATE TABLE IF NOT EXISTS host_render (
    host   TEXT PRIMARY KEY,
    always INTEGER DEFAULT 0
);
-- Per-HOST weak-under-load learning: a host that refused/reset/timed-out at high concurrency is
-- remembered so the NEXT run starts it careful (concurrency 1). Same shape/lifecycle as host_render.
-- `at` makes the mark EXPIRE. Without it a host promoted by three transient timeouts stayed at
-- concurrency 1 forever, because make_careful returns early for a host already careful, so nothing
-- ever re-tested it. MEASURED: 365 hosts marked fleet-wide against only 76 hosts with pending work
-- — so essentially everything being crawled was capped at one connection, and 48 workers per
-- instance had nowhere to go. That is a livelock, not politeness: i9 and i10 were spinning at 157
-- and 231 host-deflections PER FETCH.
-- The trigger is three conn_refused/timeouts, which a load spike produces on healthy hosts: the
-- 40-minute database stall that preceded this measurement timed out crawler POSTs fleet-wide.
CREATE TABLE IF NOT EXISTS host_careful (
    host    TEXT PRIMARY KEY,
    careful INTEGER DEFAULT 0,
    at      TEXT
);
-- Durable frontier, for RESUME after an interrupted crawl (operator stop, container restart,
-- network drop, power cut).
--
-- Without it, restarting a half-finished crawl means re-walking the whole site from the seed.
-- Content-hash dedup keeps the duplicates out of the corpus, so the DATA stays correct — but the
-- crawl still pays the full fetch cost a second time, which on a large site is hours. That is the
-- cost this table removes.
--
-- One row per URL per job, holding BOTH states rather than deleting on completion:
--   pending — discovered, not yet processed  -> reloaded into the frontier on resume
--   done    — already processed              -> reloaded into `seen`, so it is never re-enqueued
-- Keeping the done rows is what makes `seen` exact on resume. Deriving it from crawl_pages instead
-- would need a host LIKE-scan over every page ever crawled, and would still miss URLs that were
-- skipped rather than stored (gone, robots-denied, gated).
CREATE TABLE IF NOT EXISTS crawl_frontier (
    job_id       TEXT NOT NULL,
    url          TEXT NOT NULL,
    depth        INTEGER NOT NULL DEFAULT 0,
    parent_url   TEXT,
    needs_render INTEGER NOT NULL DEFAULT 0,
    state        TEXT NOT NULL DEFAULT 'pending',
    queued_at    TEXT,
    PRIMARY KEY (job_id, url)
);
CREATE INDEX IF NOT EXISTS ix_frontier_job_state ON crawl_frontier(job_id, state);

-- Per-job resume bookkeeping. pages_done is what stops a resumed job getting a fresh max_pages
-- allowance and overshooting its budget.
CREATE TABLE IF NOT EXISTS crawl_job_state (
    job_id     TEXT PRIMARY KEY,
    pages_done INTEGER NOT NULL DEFAULT 0,
    status     TEXT,
    started_at TEXT,
    updated_at TEXT,
    -- The job's own definition, so Resume is ONE action. Without it the operator would have to
    -- re-supply seed_urls/max_depth/capture/... exactly as before, and any difference would make
    -- the "resume" a subtly different crawl.
    job_json   TEXT
);
"""

# Columns added after the original schema shipped — back-filled onto existing DBs
# (no migration framework; a crawl_history.sqlite may predate these).
_EXTRA_COLS = {"error_category": "TEXT", "fail_count": "INTEGER DEFAULT 0", "failed_at": "TEXT",
               "crawl_run_id": "TEXT",
               # The raw error text behind error_category. `render_crash` is a catch-all for every
               # browser exception, so without this a host with 378 of them is unfixable: you
               # cannot tell a navigation timeout from a crashed tab.
               "error_detail": "TEXT"}


@dataclass
class StoredPage:
    canonical_url: str
    content_hash: str | None
    etag: str | None
    last_modified: str | None
    js_heavy: bool
    last_status: int | None


class CrawlHistory:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or config.DB_PATH)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")   # concurrent readers; safer under load
            # Wait for a write lock instead of raising "database is locked" immediately. The file is
            # shared between containers (crawler-api and the SIPRI dashboard both mount crawlerdata),
            # so two writers can genuinely collide; without this the loser drops its record silently
            # and the page gets refetched on the next run.
            self._conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
        with closing(self._conn.cursor()) as cur:
            cur.executescript(_SCHEMA)
            existing = {r[1] for r in cur.execute("PRAGMA table_info(crawl_pages)")}
            for col, decl in _EXTRA_COLS.items():
                if col not in existing:
                    cur.execute(f"ALTER TABLE crawl_pages ADD COLUMN {col} {decl}")
            # host_careful.at, same reason: CREATE TABLE IF NOT EXISTS does not add a column to a
            # table that already exists, and every live crawl_history.sqlite predates it. Rows
            # written before this keep at=NULL, which get_careful_hosts treats as EXPIRED rather
            # than permanent — those are precisely the marks that need re-testing.
            if "at" not in {r[1] for r in cur.execute("PRAGMA table_info(host_careful)")}:
                cur.execute("ALTER TABLE host_careful ADD COLUMN at TEXT")
            # AFTER the ALTERs — crawl_run_id is an _EXTRA_COLS column, so on a fresh database it
            # does not exist yet while _SCHEMA runs.
            #
            # /v1/batch/report answers "what happened on each site in this run" with
            # `WHERE crawl_run_id IN (...)`, and crawl_pages had no index beyond its canonical_url
            # primary key — so every call full-scanned the table. On a 115k-row, 97 MB history that
            # is seconds per instance, and the dashboard's per-site drill-down fans out across the
            # whole fleet: measured at 23.6 SECONDS for one click. The column is only ever queried
            # by equality, so a plain index is the whole fix.
            cur.execute("CREATE INDEX IF NOT EXISTS ix_pages_run ON crawl_pages(crawl_run_id)")
        self._conn.commit()

    def get(self, canonical_url: str) -> StoredPage | None:
        row = self._conn.execute(
            "SELECT * FROM crawl_pages WHERE canonical_url = ?", (canonical_url,)
        ).fetchone()
        if not row:
            return None
        return StoredPage(
            canonical_url=row["canonical_url"], content_hash=row["content_hash"],
            etag=row["etag"], last_modified=row["last_modified"],
            js_heavy=bool(row["js_heavy"]), last_status=row["last_status"],
        )

    def conditional_headers(self, canonical_url: str) -> dict:
        """If we've seen this URL, return validators for a conditional GET."""
        sp = self.get(canonical_url)
        headers: dict[str, str] = {}
        if sp and sp.etag:
            headers["If-None-Match"] = sp.etag
        if sp and sp.last_modified:
            headers["If-Modified-Since"] = sp.last_modified
        return headers

    def get_render_hosts(self) -> set[str]:
        """Hosts learned (in a prior run) to always need the browser — seed host_render_always so a
        recrawl skips the wasted httpx probe. Best-effort: any error → empty set (probe as usual)."""
        try:
            rows = self._conn.execute("SELECT host FROM host_render WHERE always=1").fetchall()
            return {r[0] for r in rows}
        except Exception:
            return set()

    def set_render_host(self, host: str, always: bool = True) -> None:
        """Persist that a host is always-render (or not), so the decision survives across runs."""
        try:
            self._conn.execute(
                "INSERT INTO host_render (host, always) VALUES (?, ?) "
                "ON CONFLICT(host) DO UPDATE SET always=excluded.always",
                (host, 1 if always else 0))
            self._conn.commit()
        except Exception:
            pass

    def get_careful_hosts(self) -> set[str]:
        """Hosts learned (in a prior run) to be weak under load — seed force_careful so a recrawl
        starts them at concurrency 1. Best-effort: any error → empty set (treat as normal).

        Marks EXPIRE after CRAWLER_CAREFUL_TTL_H (default 24h). The promotion trigger is three
        conn_refused/timeouts, which a load spike produces on perfectly healthy hosts — and
        `make_careful` returns early for a host already careful, so nothing ever re-tested one.
        One bad minute, concurrency 1 forever. MEASURED: 365 hosts marked against 76 with pending
        work, i9 and i10 spinning at 157 and 231 host-deflections per fetch.

        Expiry is the whole re-test: a genuinely weak host earns the mark again within three
        failures, at the cost of one window of normal concurrency. A healthy host marked during an
        incident recovers on its own instead of needing an operator to notice.

        A NULL `at` is a row written before this column existed. Treated as expired rather than
        permanent — those are exactly the poisoned ones."""
        try:
            ttl_h = float(os.environ.get("CRAWLER_CAREFUL_TTL_H", "24") or 24)
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(hours=ttl_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
            rows = self._conn.execute(
                "SELECT host FROM host_careful WHERE careful=1 AND at IS NOT NULL AND at > ?",
                (cutoff,)).fetchall()
            return {r[0] for r in rows}
        except Exception:
            return set()

    def set_careful_host(self, host: str, careful: bool = True) -> None:
        """Persist that a host is weak-under-load careful, so the decision survives across runs.

        Stamped, so get_careful_hosts can expire it. An unstamped mark is a permanent one, and a
        permanent mark from three transient timeouts is what capped 365 hosts at one connection."""
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._conn.execute(
                "INSERT INTO host_careful (host, careful, at) VALUES (?, ?, ?) "
                "ON CONFLICT(host) DO UPDATE SET careful=excluded.careful, at=excluded.at",
                (host, 1 if careful else 0, now))
            self._conn.commit()
        except Exception:
            pass

    def upsert(self, canonical_url: str, *, content_hash: str | None,
               etag: str | None, last_modified: str | None,
               status: int | None, fetched_at: str, js_heavy: bool = False,
               crawl_run_id: str | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO crawl_pages
                (canonical_url, content_hash, etag, last_modified, js_heavy,
                 last_status, last_seen, times_seen, crawl_run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(canonical_url) DO UPDATE SET
                content_hash  = COALESCE(excluded.content_hash, crawl_pages.content_hash),
                etag          = COALESCE(excluded.etag,          crawl_pages.etag),
                last_modified = COALESCE(excluded.last_modified, crawl_pages.last_modified),
                js_heavy      = excluded.js_heavy,
                last_status   = excluded.last_status,
                last_seen     = excluded.last_seen,
                times_seen    = crawl_pages.times_seen + 1,
                error_category = NULL,
                -- Clear the DETAIL too. Clearing only the category left a recovered URL showing the
                -- error that no longer applies: zf.com/cars/select fetched fine at 200 while still
                -- reporting "net::ERR_SOCKET_NOT_CONNECTED" from its previous run.
                error_detail  = NULL,
                fail_count    = 0,
                crawl_run_id  = excluded.crawl_run_id
            """,
            (canonical_url, content_hash, etag, last_modified,
             int(js_heavy), status, fetched_at, crawl_run_id),
        )
        self._conn.commit()

    def record_failure(self, canonical_url: str, *, status: int | None,
                       category: str | None, failed_at: str,
                       crawl_run_id: str | None = None, detail: str | None = None) -> None:
        """Persist a failed fetch so 'gone' (404/410) and 'retry next run' survive across runs.
        Increments fail_count so is_gone can require two 404s before giving up on a URL.

        `detail` is the raw error text. Without it `render_crash` is a dead end: it is a catch-all
        for every exception the browser can raise — navigation timeout, crashed tab, closed target,
        TLS — and 2,616 URLs carried that label with no way to tell which. The category drives
        policy; the detail is what makes it fixable.
        """
        self._conn.execute(
            """
            INSERT INTO crawl_pages
                (canonical_url, last_status, error_category, error_detail, failed_at, fail_count,
                 last_seen, times_seen, crawl_run_id)
            VALUES (?, ?, ?, ?, ?, 1, ?, 0, ?)
            ON CONFLICT(canonical_url) DO UPDATE SET
                last_status    = excluded.last_status,
                error_category = excluded.error_category,
                error_detail   = excluded.error_detail,
                failed_at      = excluded.failed_at,
                fail_count     = COALESCE(crawl_pages.fail_count, 0) + 1,
                last_seen      = excluded.last_seen,
                crawl_run_id   = excluded.crawl_run_id
            """,
            (canonical_url, status, category, (detail or "")[:300] or None,
             failed_at, failed_at, crawl_run_id),
        )
        self._conn.commit()

    def mark_unsent(self, canonical_url: str) -> None:
        """Forget this URL's CONTENT identity so the next run re-fetches and re-sends it.

        Called when a page was fetched fine but never reached the store. By then `upsert` has
        already written content_hash/etag/last_modified — which is exactly what makes the next run
        classify it "unchanged" and skip it. Without this, a document lost to an ingest blip is lost
        permanently AND the crawler is thereafter confident it already has it.

        All three validators are cleared, not just the hash: a surviving ETag earns a 304 on the
        next fetch, which short-circuits the re-send just as effectively as a hash match.
        The row itself stays, so fail_count and audit history survive.
        """
        self._conn.execute(
            "UPDATE crawl_pages SET content_hash = NULL, etag = NULL, last_modified = NULL "
            "WHERE canonical_url = ?", (canonical_url,))
        self._conn.commit()

    def record_audit(self, *, url: str, host: str, fetched_at: str, ua: str,
                     robots_decision: str, status: int | None, reason: str | None,
                     careful: bool) -> None:
        """Append-only compliance record: what we crawled, when, as whom, under which robots
        decision. One row per visit (unlike crawl_pages which overwrites). Used for careful
        (gov/mil) hosts so polite crawling is provable."""
        self._conn.execute(
            "INSERT INTO crawl_audit (url, host, fetched_at, ua, robots_decision, status, "
            "reason, careful) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (url, host, fetched_at, ua, robots_decision, status, reason, int(bool(careful))))
        self._conn.commit()

    def is_gone(self, canonical_url: str) -> bool:
        """True if this URL is known permanently gone → skip it on future runs. Golden rule:
        a false 'gone' loses data, so 410 (the server's explicit word) counts immediately but
        404 needs TWO strikes (a transient 404 during a deploy shouldn't kill a URL forever).

        Poison quarantine is the third case, and it is what makes "retry until it works" terminate.
        A URL that has failed many times WITHOUT ever reaching the server (no HTTP status at all —
        it crashed the renderer, or the socket never opened) is not going to start working on the
        next run either, and every attempt costs a browser tab for up to the item timeout. The
        threshold is deliberately generous, and it requires last_status IS NULL: anything that
        actually answered, even with a 500, keeps being retried on its own policy.
        """
        row = self._conn.execute(
            "SELECT last_status, fail_count FROM crawl_pages WHERE canonical_url = ?",
            (canonical_url,)).fetchone()
        if not row:
            return False
        st, fc = row["last_status"], (row["fail_count"] or 0)
        if st == 410:
            return True
        if st == 404 and fc >= 2:
            return True
        return st is None and fc >= _env_int("CRAWLER_POISON_FAILS", 8)

    def iter_needs_network_path(self, since_iso: str, limit: int):
        """Lazily yield URLs that need a different network egress (US/residential) — a WAF/IP block
        even C3 couldn't clear this run. A sqlite cursor, so a 100k backlog never materializes in
        memory. Scoped by failed_at >= run start (crawl_pages is keyed by URL and overwrites, so a
        stale prior-run row isn't replayed). Any later success resets error_category → drops out."""
        try:
            cur = self._conn.execute(
                "SELECT canonical_url FROM crawl_pages WHERE error_category = 'needs_network_path' "
                "AND (failed_at IS NULL OR failed_at >= ?) LIMIT ?",
                (since_iso, limit))
            for row in cur:
                yield row["canonical_url"]
        except Exception:
            return

    # ── durable frontier (resume) ────────────────────────────────────────────
    # All four writers take LISTS, not single rows. A crawl enqueues thousands of URLs a minute and
    # one INSERT per URL would put a sqlite round trip on the frontier's hot path; the engine
    # buffers and flushes these in batches instead.

    def frontier_add(self, job_id: str, rows: list[tuple]) -> int:
        """rows = [(url, depth, parent_url, needs_render)]. INSERT OR IGNORE: a URL already
        recorded for this job keeps its existing state, so re-enqueuing cannot resurrect a done
        row as pending (which would make a resume re-fetch it)."""
        if not rows:
            return 0
        now = _now_iso()
        with self._lock_free():
            self._conn.executemany(
                "INSERT OR IGNORE INTO crawl_frontier "
                "(job_id, url, depth, parent_url, needs_render, state, queued_at) "
                "VALUES (?,?,?,?,?, 'pending', ?)",
                [(job_id, u, d, p, int(bool(nr)), now) for u, d, p, nr in rows])
            self._conn.commit()
        return len(rows)

    def frontier_relax(self, job_id: str, rows: list[tuple]) -> int:
        """rows = [(url, depth)]. Lower a PENDING row's depth when the URL turns out to be reachable
        by a shorter path than the one that first found it.

        `depth` is meant to be "hops from the seed", and every rule that reads it — max_depth, the
        incremental fresh-depth surface, resume's shallowest-first ordering — assumes that. Under a
        LIFO (DFS) frontier, first sight records the depth of a DESCENT instead, which is a fact
        about the walk and not about the site. MEASURED on al-monitor.com: 81,297 pages spread over
        1,820 distinct depths, max 1,826, the busiest single level d1766 — while that site's own
        /archives index sits at d1 and every monthly page under it at d2, so those articles are
        three hops from the seed and were recorded as seventeen hundred.

        Only `pending` rows move. A `done` row's depth is provenance — it records where the crawler
        actually was when it fetched the page — and rewriting history to match a later discovery
        would make the stored corpus disagree with itself.

        `AND depth > ?` keeps this monotone: depth only ever falls, so replaying a flush is a no-op
        and two workers relaxing the same URL cannot fight."""
        if not rows:
            return 0
        with self._lock_free():
            self._conn.executemany(
                "UPDATE crawl_frontier SET depth=? "
                "WHERE job_id=? AND url=? AND state='pending' AND depth > ?",
                [(int(d), job_id, u, int(d)) for u, d in rows])
            self._conn.commit()
        return len(rows)

    def frontier_done(self, job_id: str, urls: list[str]) -> int:
        """Mark URLs processed. They stay in the table: on resume they populate `seen`, which is
        what stops the crawl re-fetching everything it already has."""
        if not urls:
            return 0
        with self._lock_free():
            self._conn.executemany(
                "UPDATE crawl_frontier SET state='done' WHERE job_id=? AND url=?",
                [(job_id, u) for u in urls])
            self._conn.commit()
        return len(urls)

    def frontier_pending(self, job_id: str, limit: int = 200_000) -> list[tuple]:
        """The URLs to resume from: [(url, depth, parent_url, needs_render)], shallowest first so a
        resumed crawl keeps breadth-first order rather than diving down whatever happened to be
        queued last."""
        rows = self._conn.execute(
            "SELECT url, depth, parent_url, needs_render FROM crawl_frontier "
            "WHERE job_id=? AND state='pending' ORDER BY depth ASC, rowid ASC LIMIT ?",
            (job_id, limit)).fetchall()
        return [(r[0], int(r[1] or 0), r[2], bool(r[3])) for r in rows]

    def frontier_seen(self, job_id: str) -> set[str]:
        """Every URL this job has already enqueued — pending AND done. Loaded into ctx.seen on
        resume so re-discovered links are not queued a second time."""
        return {r[0] for r in self._conn.execute(
            "SELECT url FROM crawl_frontier WHERE job_id=?", (job_id,)).fetchall()}

    def frontier_counts(self, job_id: str) -> dict:
        rows = self._conn.execute(
            "SELECT state, COUNT(*) FROM crawl_frontier WHERE job_id=? GROUP BY state",
            (job_id,)).fetchall()
        d = {str(r[0]): int(r[1]) for r in rows}
        return {"pending": d.get("pending", 0), "done": d.get("done", 0),
                "total": sum(d.values())}

    def frontier_prune(self, is_junk, apply: bool = False, sample: int = 10) -> dict:
        """Drop PENDING frontier URLs that `is_junk(url)` rejects, and report what went.

        A tightened enqueue filter only ever affects links discovered from here on. The junk already
        queued keeps its place in line — and it is the queued junk that does the damage, because a
        crawl works its frontier before it discovers anything new. Measured when this was written:
        four instances held 128 of 128 render-lane slots with ZERO renders in flight and had stored
        two pages in an hour, the queue behind them being calendar exports that each burned the 240s
        item watchdog and then requeued themselves.

        `is_junk` is passed in rather than imported so this stays the storage layer and, more to the
        point, so the caller hands it the SAME predicate the enqueue path uses — a purge with its own
        idea of junk would delete URLs the crawler would then re-discover, for ever.

        Only `pending` rows are touched. A `done` row is the seen-set entry that stops a URL being
        re-queued; deleting those would invite every one of these straight back in.

        Dry by default. `apply=False` counts and samples without writing, because the first thing to
        want from a rule that deletes is to see what it would take before it takes it.
        """
        rows = self._conn.execute(
            "SELECT rowid, job_id, url FROM crawl_frontier WHERE state='pending'").fetchall()
        hits = [(r[0], str(r[1]), str(r[2])) for r in rows if is_junk(str(r[2]))]
        by_host: dict[str, int] = {}
        for _, _, u in hits:
            h = urlsplit(u).netloc.lower().removeprefix("www.")
            by_host[h] = by_host.get(h, 0) + 1
        out = {"pending": len(rows), "junk": len(hits), "applied": bool(apply),
               "by_host": dict(sorted(by_host.items(), key=lambda kv: -kv[1])[:20]),
               "sample": [u for _, _, u in hits[:sample]]}
        if apply and hits:
            with self._lock_free():
                # Chunked: SQLite's default host-parameter ceiling is 999 and a wedged instance can
                # be holding five figures of these.
                for i in range(0, len(hits), 500):
                    batch = [h[0] for h in hits[i:i + 500]]
                    self._conn.execute(
                        "DELETE FROM crawl_frontier WHERE rowid IN (%s)"
                        % ",".join("?" * len(batch)), batch)
                self._conn.commit()
        return out

    def frontier_clear(self, job_id: str) -> int:
        """Drop a job's frontier. Called when a job starts WITHOUT resume, so a fresh crawl is
        genuinely fresh and cannot inherit a stale half-finished frontier.

        LOGS WHAT IT DELETES. It used to be silent, and that silence cost a real investigation:
        ~389,000 queued dvidshub URLs disappeared and nothing anywhere recorded it — not the row
        count, not the job, not the caller. Hours went into ruling out a junk filter, a schedule
        change and database corruption, and the cause was only found by reading this function.
        A mass delete that leaves no trace is indistinguishable from data loss.

        Above _BIG_CLEAR it logs a WARNING with a stack, because dropping tens of thousands of
        queued URLs is either a deliberate re-crawl or a bug, and the stack is what tells them
        apart after the fact."""
        pending = 0
        try:
            pending = self._conn.execute(
                "SELECT count(*) FROM crawl_frontier WHERE job_id=? AND state='pending'",
                (job_id,)).fetchone()[0]
        except Exception:                                # noqa: BLE001 — never block the delete
            pass
        with self._lock_free():
            cur = self._conn.execute("DELETE FROM crawl_frontier WHERE job_id=?", (job_id,))
            self._conn.execute("DELETE FROM crawl_job_state WHERE job_id=?", (job_id,))
            self._conn.commit()
        rows = cur.rowcount or 0
        if pending >= _BIG_CLEAR:
            log.warning("frontier_clear DROPPED %d PENDING url(s) for job=%s (%d rows total) — "
                        "if this job was not meant to restart from its seed, this is data loss",
                        pending, job_id, rows, stack_info=True)
        elif rows:
            log.info("frontier_clear job=%s removed %d row(s), %d of them pending",
                     job_id, rows, pending)
        return rows

    def job_state_save(self, job_id: str, *, pages_done: int, status: str,
                       job_json: str | None = None) -> None:
        now = _now_iso()
        with self._lock_free():
            # COALESCE on job_json: a later save that does not carry the definition (a progress
            # tick) must not erase the one stored at start, or Resume loses the config.
            self._conn.execute(
                "INSERT INTO crawl_job_state (job_id, pages_done, status, started_at, updated_at, job_json) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
                "pages_done=excluded.pages_done, status=excluded.status, "
                "updated_at=excluded.updated_at, job_json=COALESCE(excluded.job_json, job_json)",
                (job_id, int(pages_done), status, now, now, job_json))
            self._conn.commit()

    def job_state(self, job_id: str) -> dict | None:
        r = self._conn.execute(
            "SELECT job_id, pages_done, status, started_at, updated_at, job_json "
            "FROM crawl_job_state WHERE job_id=?", (job_id,)).fetchone()
        return dict(r) if r else None

    def resumable_jobs(self, limit: int = 100) -> list[dict]:
        """Jobs with pending frontier rows — i.e. crawls that stopped before finishing and can be
        picked up where they left off. This is what the dashboard's Resume list is built from.

        A job whose status is 'completed' is excluded even when rows remain pending. That is the
        max_pages case: the crawl ended because it spent its budget, not because it was cut off, so
        resuming it would restore the spent budget and stop again immediately. The engine now drops
        such a frontier at the end of the run; this filter also hides the ones written before it did.

        Left in on purpose: status 'running' with pending rows. That is a crawl whose process died
        (container restart, OOM) and never got to write its end state — genuinely interrupted, and
        exactly what resume exists for."""
        # `done` and `host` ride along so a caller can draw per-job PROGRESS, not just a backlog:
        # pending alone says how much is left and nothing about how far it got. The pending filter
        # moved from WHERE to HAVING for exactly that reason — filtering rows before the GROUP BY
        # meant the done rows were never in scope to be counted.
        rows = self._conn.execute(
            "SELECT f.job_id, "
            "       SUM(CASE WHEN f.state='pending' THEN 1 ELSE 0 END) AS pending, "
            "       SUM(CASE WHEN f.state='done'    THEN 1 ELSE 0 END) AS done, "
            "       COALESCE(s.pages_done, 0) AS pages_done, s.status, s.updated_at, "
            "       (s.job_json IS NOT NULL) AS has_config, s.job_json AS _cfg "
            "FROM crawl_frontier f LEFT JOIN crawl_job_state s ON s.job_id = f.job_id "
            "GROUP BY f.job_id "
            "HAVING pending > 0 AND COALESCE(s.status, '') <> 'completed' "
            "ORDER BY s.updated_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # The seed host, so a dashboard can line a job up with the host the batch reports. Never
            # return job_json itself — it is a whole job definition and this endpoint polls often.
            cfg, d["host"] = d.pop("_cfg", None), ""
            try:
                seeds = (json.loads(cfg or "{}") or {}).get("seed_urls") or []
                if seeds:
                    d["host"] = (urlsplit(str(seeds[0])).hostname or "").lower()
            except (ValueError, TypeError):
                pass                      # a malformed config must not break the Resume list
            out.append(d)
        return out

    def forget_resumable(self, job_ids: list[str] | None = None) -> dict:
        """Drop saved frontiers — `job_ids` None means every job that has one.

        Returns {job_id: rows_deleted}. This is the operator's "I am never resuming these" button:
        the crawled pages themselves live in Postgres/MinIO and are untouched; what goes is the
        bookkeeping that would let a re-run skip re-fetching them."""
        ids = job_ids
        if ids is None:
            ids = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT job_id FROM crawl_frontier").fetchall()]
        return {jid: self.frontier_clear(jid) for jid in ids}

    @contextmanager
    def _lock_free(self):
        """Swallow a LOCK CONTENTION failure only.

        sqlite is shared between containers, so a write can genuinely lose the lock race;
        busy_timeout covers most of it and losing the rest costs only resume precision, never the
        crawl. But this used to catch every sqlite3.Error, and that hid a real bug: the flush ran
        the connection from a worker thread, sqlite raised "SQLite objects created in a thread can
        only be used in that same thread", and it was logged as a routine skip — so the durable
        frontier was never written and resume silently did nothing. A programming error must be
        loud; only contention is routine."""
        try:
            yield
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" in msg or "busy" in msg:
                log.warning("frontier write skipped (%s) — resume may re-fetch a few pages", exc)
                return
            raise

    def close(self) -> None:
        self._conn.close()


def is_strong_etag(etag: str | None) -> bool:
    """Weak validators (``W/"..."``) can't justify trusting a 304."""
    return bool(etag) and not etag.strip().startswith(("W/", "w/"))


def classify(stored: StoredPage | None, *, status: int | None,
             content_hash: str | None, error: bool = False) -> Verdict:
    """Verdict from a fetch result. Bias every ambiguity toward ``changed``."""
    if stored is None or not stored.content_hash:
        return "new"
    if error:
        return "changed"                      # couldn't verify -> re-crawl
    if status in (404, 410):
        return "gone"
    if status == 304:
        return "unchanged" if is_strong_etag(stored.etag) else "changed"
    if status is not None and status >= 400:
        return "changed"
    if not content_hash:
        return "changed"                      # no usable hash -> uncertain
    return "unchanged" if content_hash == stored.content_hash else "changed"

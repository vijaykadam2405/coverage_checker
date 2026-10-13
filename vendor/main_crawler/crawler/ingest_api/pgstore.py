"""Optional Postgres `documents` store for the L1 ingest landing.

Each captured document is written here — the structured dump L2 reads — IN ADDITION to the ndjson
audit file and the MinIO blobs. This is the ONLY Postgres in the crawler stack (a single `documents`
table); L2 runs separately with its own DB. Disabled (no-op) when DATABASE_URL is unset — tests and
ndjson-only runs are unaffected, and the crawl ENGINE itself never imports this.
"""
from __future__ import annotations

import logging
import os
import threading
import time as _time

# Store faults went to stdout via print(), which means they were invisible to any log level, filter
# or aggregator the deployment uses. A silent store is exactly what the dashboards then misreport.
log = logging.getLogger("pgstore")

_LOCK = threading.Lock()
_CONN = None
_NO_DB = False            # DATABASE_URL unset — legitimately off, never retry
_RETRY_AFTER = 0.0        # monotonic ts; skip reconnect attempts until then
_RETRY_COOLDOWN_S = 30    # after a failed connect, wait this long before retrying

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id  TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    content_hash TEXT,
    fetched_at   TEXT,
    archived_at  TEXT,
    served_by    TEXT,
    served_from  TEXT,
    source_id    TEXT,
    title        TEXT,
    published_at TEXT,
    language     TEXT,
    depth        INTEGER,
    parent_url   TEXT,
    main_text    TEXT,
    html         TEXT,
    attachments  JSONB,
    ingested_at  TEXT,
    html_len     INTEGER,
    crawl_run_id TEXT
);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS html_len INTEGER;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS crawl_run_id TEXT;
-- When this row was accepted by the data centre. NULL = still local-only, which is what makes the
-- push resumable: a run that dies halfway re-sends only what never landed, not all 97k documents.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS pushed_at TIMESTAMPTZ;
-- WHICH MACHINE captured this. The crawler runs on N machines that all push into one centre, and
-- without this the corpus cannot answer "where did this page come from" — every row looks the same
-- and a misbehaving node is invisible. Stamped at insert from crawler.fleet.node_id().
ALTER TABLE documents ADD COLUMN IF NOT EXISTS node_id TEXT;
-- length(main_text) computed on demand DETOASTS THE WHOLE CORPUS. The heap is ~170 MB; main_text and
-- html live in TOAST, which was 8.2 GB, so ranking or summing by that length read every row's body
-- off disk. Measured live: the dashboard's summary query took 15.0s and dropped to 0.64s once this
-- column existed. `html_len` has always been stored for exactly this reason; this is its twin.
--
-- A nullable column + trigger, NOT `GENERATED ALWAYS AS (length(main_text)) STORED`: a stored
-- generated column rewrites the entire table under ACCESS EXCLUSIVE — minutes of every insert
-- blocked, with the crawler's 30s ingest timeout firing. This form is a catalog-only change.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS text_len INTEGER;
CREATE INDEX IF NOT EXISTS documents_node_idx ON documents (node_id);
CREATE INDEX IF NOT EXISTS documents_unpushed_idx ON documents (pushed_at) WHERE pushed_at IS NULL;
CREATE INDEX IF NOT EXISTS documents_url_idx ON documents (url);
-- source_id had NO index, so every per-source query seq-scanned the whole table: the per-site
-- panel, the delete preview, the depth breakdown, and the site->depth->pages view all paid for it.
-- Measured on the live store (487k rows): by_source(800) 8.0s -> and the depth query's own scan
-- dropped out of the plan entirely. On an existing database this is created CONCURRENTLY by hand
-- (a plain CREATE INDEX takes ACCESS EXCLUSIVE and would stall every crawler mid-write); here it
-- is IF NOT EXISTS so a fresh deploy simply has it.
CREATE INDEX IF NOT EXISTS documents_source_idx ON documents (source_id);
-- "how many pages arrived recently" is the single most-asked question of this table — the live
-- summary, the rate tiles, every throughput check. Without this it is a full pass over the corpus
-- every time, and those scans are what had Postgres at 7.16 of 40 cores while the crawl halved.
-- Only useful alongside a TEXT comparison: casting the column per row defeats any index on it.
CREATE INDEX IF NOT EXISTS documents_ingested_at_idx ON documents (ingested_at);

-- Keeps text_len correct for every writer, including anything reaching Postgres without going
-- through insert_document(). PG16 has no IF NOT EXISTS for triggers, so drop-then-create is what
-- makes this safe to re-run at every startup.
CREATE OR REPLACE FUNCTION documents_set_text_len() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.text_len := length(NEW.main_text);
  RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS documents_text_len ON documents;
CREATE TRIGGER documents_text_len
  BEFORE INSERT OR UPDATE OF main_text ON documents
  FOR EACH ROW EXECUTE FUNCTION documents_set_text_len();

-- One row per URL: the BEST capture, not the newest. Revisions stay in `documents` as history;
-- everything that wants "the page" (dashboard, L2 export) reads this view.
-- Ranking is deliberate: on this corpus the newest copy was SHORTER than an older one for 772 of
-- 1,946 duplicated urls, and >2x shorter for 211 (hensoldt.net/events: newest 244 chars vs 33,039),
-- because a re-crawl can be truncated by a WAF wall or a partial render. So: longest main_text
-- first, newest as the tie-break.
CREATE OR REPLACE VIEW documents_current AS
SELECT DISTINCT ON (url) *
FROM documents
-- coalesce(text_len, length(main_text), 0) is behaviourally IDENTICAL to the old
-- coalesce(length(main_text), 0): the backfill set text_len = length(main_text) exactly, verified
-- 0 mismatches over 224,539 rows. COALESCE short-circuits, so a row with text_len never touches
-- TOAST and a row written before the trigger existed still ranks correctly.
ORDER BY url, coalesce(text_len, length(main_text), 0) DESC, fetched_at DESC, document_id DESC;
"""

# Scalar columns copied straight off the document dict (attachments + ingested_at handled separately).
_COLS = ["document_id", "url", "content_hash", "fetched_at", "archived_at", "served_by", "served_from",
         "source_id", "title", "published_at", "language", "depth", "parent_url", "main_text", "html",
         "crawl_run_id"]


def _node_id() -> str:
    """This machine's fleet identity. Imported lazily so ingest_api keeps working standalone."""
    try:
        from crawler.fleet import node_id
        return node_id()
    except Exception:
        return ""


def _norm_url(url: str) -> str:
    # accept a SQLAlchemy-style URL too (L2's compose uses postgresql+psycopg://)
    return (url.replace("postgresql+psycopg://", "postgresql://")
               .replace("postgresql+psycopg2://", "postgresql://"))


def _connect():
    """Open the connection + ensure the schema. Raises on failure (caller handles)."""
    global _CONN
    import psycopg
    conn = psycopg.connect(_norm_url(os.environ["DATABASE_URL"]), autocommit=True, connect_timeout=5)
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)
    _CONN = conn
    return conn


def _get():
    """Return a live connection, or None.

    A failed connect is treated as TRANSIENT: we retry after a short cooldown rather than
    disabling forever. (The old behaviour black-holed an entire crawl's writes when a startup
    DNS blip resolving `postgres` failed once — inserts silently no-op'd while ndjson kept
    growing and the API still returned 200/accepted.) DATABASE_URL being unset is the only
    permanent-off state.
    """
    global _CONN, _NO_DB, _RETRY_AFTER
    if _NO_DB:
        return None
    if _CONN is not None:
        return _CONN
    if not os.environ.get("DATABASE_URL"):
        _NO_DB = True
        return None
    if _time.monotonic() < _RETRY_AFTER:
        return None
    with _LOCK:
        if _CONN is None and not _NO_DB:
            try:
                _connect()
            except Exception as exc:  # noqa: BLE001
                _RETRY_AFTER = _time.monotonic() + _RETRY_COOLDOWN_S
                print(f"[pgstore] connect failed (retry in {_RETRY_COOLDOWN_S}s): {exc}", flush=True)
    return _CONN


def insert_document(doc: dict, ingested_at: str) -> None:
    """Best-effort insert of one captured document. No-op if DATABASE_URL unset / DB unreachable.

    Idempotent on document_id, which is now DETERMINISTIC on (url, content) — see
    crawler.models.version_id. So a re-crawl that found the page unchanged collides and is skipped
    (no duplicate), while a genuinely changed page gets a new id and is kept as a new version.
    Read one-row-per-url through the `documents_current` view rather than the table."""
    conn = _get()
    if conn is None:
        return
    try:
        from psycopg.types.json import Jsonb
        vals = [doc.get(c) for c in _COLS]
        vals.append(Jsonb(doc.get("attachments") or []))
        vals.append(ingested_at)
        vals.append(len(doc.get("html") or ""))          # html_len — cheap size stat for the dashboard
        # node_id defaulted HERE, not at the call sites: every producer of a document (engine,
        # feed fallback, API fallback, C3) would otherwise have to remember it, and the one that
        # forgot would silently write an unattributable row.
        vals.append(doc.get("node_id") or _node_id())
        placeholders = ", ".join(["%s"] * (len(_COLS) + 4))
        cols = ", ".join(_COLS + ["attachments", "ingested_at", "html_len", "node_id"])
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO documents ({cols}) VALUES ({placeholders}) "
                f"ON CONFLICT (document_id) DO NOTHING",
                vals)
    except Exception as exc:  # noqa: BLE001 — never let a DB hiccup drop the ingest
        global _CONN, _RETRY_AFTER
        if getattr(conn, "closed", False) or getattr(conn, "broken", False):
            _CONN = None                                    # drop a dead conn → _get() reconnects
            _RETRY_AFTER = _time.monotonic() + _RETRY_COOLDOWN_S
        print(f"[pgstore] insert failed: {exc}", flush=True)


def ensure_schema() -> None:
    """Create the documents table at startup if a DB is configured (else no-op)."""
    _get()


# ── Read side (the :9090 dashboard) ──────────────────────────────────────────
# Short-lived connection per call, isolated from the shared insert _CONN (psycopg3 conns aren't
# safe for concurrent use; the dashboard is low-QPS so a per-call connect is fine).
_HAS_DB = False


class QueryFailed(RuntimeError):
    """The database could not be read. Distinct from "the query returned no rows"."""


def query(sql: str, params=()) -> list:
    """Best-effort read. Returns rows (list of tuples) or [] on any error / no DB.

    Kept lenient because most callers are panels that should degrade quietly. Callers that put a
    NUMBER on a dashboard must use `query_strict` instead — see below.
    """
    try:
        # `or []` matters: query_strict returns None for "no DATABASE_URL", and callers here
        # iterate the result. Returning None broke every list-shaped facet on the page.
        return query_strict(sql, params) or []
    except QueryFailed:
        return []


def query_strict(sql: str, params=()):
    """Read, or raise. Returns None when there is no DATABASE_URL configured at all.

    Why this exists: the lenient version returns [] both for "the store is empty" and for
    "Postgres is unreachable", and every caller downstream turned [] into a confident zero. A
    failed connection therefore rendered as a fully-populated empty corpus — 0 documents,
    0 sources, 0 bytes — on two dashboards at once, with the reconciliation panel ticking it
    green. An operator cannot tell that apart from a store that really is empty.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    import psycopg
    try:
        # ponytail: 600s statement_timeout — a backstop against a genuinely runaway read (e.g. an
        # accidental detoast), NOT the pile-up fix. live._cached_pg already single-flights the heavy
        # rollups so only one runs at a time; the stampede came from the pre-single-flight image, not
        # from a missing cap. The cap MUST exceed the rollup cost or it kills it before it can warm the
        # cache (a 20s and even a 240s cap did exactly that — the full-corpus DISTINCT-ON rollup over
        # documents_current measures ~250s+ under active-crawl I/O). 600s clears that with headroom
        # while still killing the 11-55min zombie class from the tsvector-overload incident.
        with psycopg.connect(_norm_url(url), autocommit=True,
                             options="-c statement_timeout=600000") as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("query failed: %s", exc)
        raise QueryFailed(str(exc)[:200]) from exc


def has_db() -> bool:
    """True once a configured DB answers SELECT 1 (cached). False without DATABASE_URL or while the
    DB is still unreachable — so the dashboard falls back to the ndjson tail (and tests stay ndjson)."""
    global _HAS_DB
    if _HAS_DB:
        return True
    if os.environ.get("DATABASE_URL") and query("SELECT 1"):
        _HAS_DB = True
    return _HAS_DB


# Card fields for the dashboard — everything EXCEPT the heavy html (its length only, via html_len).
_CARD_SQL = """SELECT document_id, url, title, main_text, fetched_at, depth, parent_url,
                      served_by, served_from, attachments, coalesce(html_len, 0), archived_at
               FROM documents_current ORDER BY fetched_at DESC LIMIT %s"""


def fetch_documents(limit: int = 50000) -> list:
    """Newest-first card rows for the whole `documents` table (no html)."""
    return query(_CARD_SQL, (limit,))


# Header totals over the WHOLE table, independent of the card LIMIT — otherwise every stat freezes
# the moment the corpus passes that cap (it did, at 50k). Deliberately sums the stored html_len
# column instead of length(html): html is TOASTed, so length(html) detoasts ~10GB and takes ~32s,
# while sum(html_len) is an integer scan at ~0.25s.
#
# main_text was left summing live as "small enough". It is not: at 1.17M rows main_text is TOASTed
# too, and this sum detoasted the lot on every corpus tile. text_len exists for exactly this and is
# fully backfilled (verified: 0 NULLs of 1,168,313). The coalesce fallback short-circuits, so
# length() is never evaluated while the column is populated — it is correctness insurance for a row
# inserted before its trigger fires, not a cost.
_TOTALS_SQL = """SELECT count(*),
                        coalesce(sum(html_len), 0),
                        coalesce(sum(coalesce(text_len, length(main_text), 0)), 0),
                        coalesce(sum((SELECT count(*) FROM jsonb_array_elements(attachments) a
                                      WHERE a->>'storage_path' IS NOT NULL)), 0)
                 FROM documents_current"""


def totals() -> tuple[int, int, int, int]:
    """(pages, html_bytes, text_chars, pdf_count) across every stored document. ~1.2s."""
    r = query(_TOTALS_SQL)
    return tuple(r[0]) if r else (0, 0, 0, 0)


def fetch_html(document_id: str) -> str | None:
    """The full stored html for one document (the /raw-html view)."""
    r = query("SELECT html FROM documents WHERE document_id = %s LIMIT 1", (document_id,))
    return r[0][0] if r else None


def urls_for_source(source_id: str) -> list[str]:
    """Every crawled url for a site (coverage cross-verify)."""
    return [row[0] for row in query("SELECT url FROM documents WHERE source_id = %s", (source_id,))]


def iter_source_rows(source_id: str):
    """Yield each best-capture-per-url document (ALL columns — incl. full html + attachments) for one
    source, as a dict, via a SERVER-SIDE cursor so a 1000-page site streams at constant memory rather
    than materialising every page's html in RAM (the download builder holds one row at a time).
    Empty generator when DATABASE_URL is unset. Same DISTINCT-ON ranking as the documents_current
    view, inlined so it doesn't depend on the view existing."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return
    import psycopg
    with psycopg.connect(_norm_url(url)) as conn:          # txn-scoped conn keeps the server cursor valid
        with conn.cursor(name="mallory_export") as cur:
            cur.itersize = 100
            cur.execute(
                # text_len, not length(main_text) — the same detoast the view's own sort key was
                # fixed for. Picking the richest revision per URL must not decompress the corpus.
                "SELECT DISTINCT ON (url) * FROM documents WHERE source_id = %s "
                "ORDER BY url, coalesce(text_len, length(main_text), 0) DESC, "
                "fetched_at DESC, document_id DESC",
                (source_id,))
            cols = [d.name for d in cur.description]
            for rec in cur:
                yield dict(zip(cols, rec))


# ── Ground-truth aggregates for the Live Data dashboard ──────────────────────
# These read ONLY from the documents table. No in-memory counters (which reset on restart and
# showed 0 while the table held 72k rows), no orchestrator job log (a different store entirely).
# Every number the dashboard prints resolves to one of these queries.

def live_summary() -> dict:
    """The headline counts, straight from Postgres.

    On a read failure this returns {"error": …} and NO counts, rather than a tidy row of zeros.
    The zeros were indistinguishable from an empty store, so an unreachable Postgres rendered as
    "0 documents" in accent-green on the Live Data page and on the orchestrator's data tile — and
    the reconciliation check agreed with itself and printed a tick.
    """
    # ingested_at is TEXT (ISO-8601). COMPARE IT AS TEXT, do not cast it.
    #
    # `ingested_at::timestamptz > now() - interval '5 minutes'` has to cast every row before it can
    # compare, so no index can be used and it is always a full pass over the corpus. ISO-8601 sorts
    # lexicographically, so a string comparison against a pre-formatted bound answers the same
    # question and can use documents_ingested_at_idx.
    #
    # MEASURED at 927k rows: the cast form ran 16 MINUTES under load; the text form, 4.2s. The cast
    # was one of the queries that had Postgres at 7.16 of 40 cores while crawl throughput halved.
    #
    # to_char on a UTC-normalised now() so the bound has the same shape as the stored values — the
    # column has no timezone, and comparing it against a local-time string would silently shift the
    # window by the server's offset.
    try:
        r = query_strict("""SELECT count(*),
                        count(DISTINCT source_id),
                        coalesce(sum(html_len), 0),
                        coalesce(sum(text_len), 0),
                        max(ingested_at),
                        count(*) FILTER (
                            WHERE ingested_at > to_char(
                                (now() at time zone 'UTC') - interval '5 minutes',
                                'YYYY-MM-DD"T"HH24:MI:SS'))
                 FROM documents_current""")
    except QueryFailed as exc:
        return {"error": str(exc)}
    if r is None:                                    # no DATABASE_URL — ndjson-only deployment
        return {"error": "no DATABASE_URL configured"}
    if not r:
        return {"documents": 0, "sources": 0, "html_bytes": 0, "text_chars": 0,
                "newest_at": None, "last_5min": 0}
    n, srcs, html_b, text_c, newest, recent = r[0]
    return {"documents": n, "sources": srcs, "html_bytes": int(html_b),
            "text_chars": int(text_c), "newest_at": str(newest) if newest else None,
            "last_5min": recent}


def by_source(limit: int = 200, q: str = "") -> list[dict]:
    """Pages per source_id (the crawler stamps this = registered domain), newest first by activity.

    `q` is a case-insensitive substring filter typed into the dashboard's search box, so it arrives
    from a URL query string: it is BOUND, never formatted into the statement. Two branches rather
    than one composed string precisely so there is no code path where it could be.
    """
    # `pdfs` uses the same LATERAL + storage_path filter as _BY_TYPE_SQL, so a per-site count and
    # the corpus-wide tile reconcile: an attachment merely DISCOVERED but never downloaded is not a
    # stored pdf. Without this the live-sites panel could show a site with pages but no visible
    # documents, which is exactly the state a tender or gov portal ends in when its PDFs fail.
    # text_bytes as well as html_bytes, because for a ONE-PAGE site they answer different
    # questions and only the text one is useful. A JS shell or a WAF interstitial carries plenty of
    # HTML and almost no text: tisaaerospace.com is a real single-page company site (3,002 chars of
    # text) while kalashnikovgroup.ru stores 19. Callers deciding whether a site is finished or
    # merely stuck need to tell those apart — see api._pages_by_domain.
    # html_len, NOT `d.html <> ''`. `html` is TOASTed, so comparing it to the empty string
    # DECOMPRESSES EVERY ROW'S HTML — 72 GB of it — to answer a question html_len already holds as
    # an int. Measured 2026-08-12: this query ran for over 6 minutes, pegged 3.4 cores at 0 MB/s of
    # disk (pure decompression, everything already in cache), stacked up 28 deep behind the
    # dashboard's pollers, and the corpus panel showed "corpus unreachable" over a healthy database.
    # Exact same defect as length(main_text) in documents_current's sort key, same fix: the _len
    # columns exist precisely so nothing has to touch the TOAST tables to count or size a page.
    _cols = """SELECT d.source_id, count(*), max(d.ingested_at),
                      count(*) FILTER (WHERE coalesce(d.html_len, 0) > 0),
                      coalesce(sum(d.html_len), 0),
                      coalesce(sum(att.n), 0),
                      coalesce(sum(d.text_len), 0)
               FROM documents_current d
               CROSS JOIN LATERAL (SELECT count(*) AS n
                                   FROM jsonb_array_elements(d.attachments) a
                                   WHERE a->>'storage_path' IS NOT NULL) att"""
    if q:
        rows = query(_cols + """ WHERE d.source_id ILIKE %s GROUP BY d.source_id
                                 ORDER BY count(*) DESC LIMIT %s""", (f"%{q}%", limit))
    else:
        rows = query(_cols + """ GROUP BY d.source_id
                                 ORDER BY count(*) DESC LIMIT %s""", (limit,))
    return [{"source_id": r[0] or "(none)", "pages": r[1],
             "last_at": str(r[2]) if r[2] else None, "with_html": r[3],
             "html_bytes": int(r[4]), "pdfs": int(r[5]),
             "text_chars": int(r[6])} for r in rows]


# Data type is DERIVED — there is no category column on `documents`, and inventing one would mean a
# migration plus a crawler change for something the stored payload already answers. html_len is the
# stored length (length(html) detoasts ~10GB, see _TOTALS_SQL) and is nullable because it was added
# by ALTER after the first crawls, hence the coalesce.
# The LATERAL counts each document's attachments ONCE and both attachment stats read that count, so
# "documents holding a pdf" and "pdfs held" cost a single scan of the view instead of two. The
# storage_path filter matches _TOTALS_SQL: an attachment we merely discovered but never downloaded
# is not a stored pdf, and the tiles have to reconcile.
_BY_TYPE_SQL = """SELECT count(*),
                         count(*) FILTER (WHERE coalesce(html_len, 0) > 0),
                         count(*) FILTER (WHERE coalesce(text_len, 0) > 0),
                         count(*) FILTER (WHERE att.n > 0),
                         coalesce(sum(att.n), 0),
                         coalesce(sum(html_len), 0),
                         coalesce(sum(text_len), 0)
                  FROM documents_current d
                  CROSS JOIN LATERAL (SELECT count(*) AS n
                                      FROM jsonb_array_elements(d.attachments) a
                                      WHERE a->>'storage_path' IS NOT NULL) att"""


def by_type() -> dict:
    """Corpus breakdown by derived data type (html / text / pdf) over documents_current."""
    r = query(_BY_TYPE_SQL)
    if not r:
        # An aggregate always returns exactly one row, so an empty result means the query never ran:
        # no DB, or a dead one. Reporting that beats zeros — on this dashboard "the store is empty"
        # and "the store is unreachable" must never render the same.
        return {"error": "no-db"}
    n, html_n, text_n, pdf_n, atts, html_b, text_c = r[0]
    return {"documents": n, "with_html": html_n, "with_text": text_n, "with_pdf": pdf_n,
            "pdf_attachments": int(atts), "html_bytes": int(html_b), "text_chars": int(text_c)}


def quality() -> dict:
    """Is the ONE corpus clean and deduplicated?

    Replaces the old local-vs-centre comparison, which asked whether data had arrived somewhere
    else. The crawler now runs AT the data centre and writes into this database directly, so there
    is no second copy to reconcile and no push backlog — the only questions left are about this
    store: are there duplicate captures, are there empty rows, is anything unattributed.

    Read-only. Nothing here deletes, repairs or rewrites; it reports and the operator decides.

    Deliberately ONE query. Six separate scans of a 220k-row table with a TOASTed body column is
    how the dashboard pinned Postgres in the first place.
    """
    r = query_strict("""
        SELECT count(*)                                                       AS rows_total,
               count(DISTINCT url)                                            AS urls_distinct,
               count(DISTINCT content_hash)                                   AS hashes_distinct,
               count(*) FILTER (WHERE content_hash IS NULL OR content_hash='') AS no_hash,
               count(*) FILTER (WHERE coalesce(text_len,0) = 0)                 AS no_text,
               count(*) FILTER (WHERE coalesce(html_len,0) = 0)                AS no_html,
               count(*) FILTER (WHERE title IS NULL OR title='')               AS no_title,
               count(*) FILTER (WHERE source_id IS NULL OR source_id='')       AS no_source,
               count(*) FILTER (WHERE fetched_at IS NULL OR fetched_at='')     AS no_fetched_at,
               min(fetched_at)                                                 AS oldest,
               max(fetched_at)                                                 AS newest
        FROM documents""")
    if r is None:
        return {"error": "no DATABASE_URL configured"}
    if not r:
        return {"error": "no rows returned"}
    (rows_total, urls_distinct, hashes_distinct, no_hash, no_text, no_html,
     no_title, no_source, no_fetched_at, oldest, newest) = r[0]
    revisions = int(rows_total) - int(urls_distinct)
    return {
        "rows_total": int(rows_total),
        "urls_distinct": int(urls_distinct),
        # A re-crawl of a page that CHANGED is a wanted revision, not duplication — documents_current
        # picks the best one. So this is reported as history, never as an error to be cleaned up.
        "revisions": revisions,
        "hashes_distinct": int(hashes_distinct),
        # Same bytes stored under DIFFERENT urls of the SAME site: genuinely redundant, usually a
        # site serving one page on many paths, or a login form recursing through its own ?back=.
        # This is the number the dedupe pass acts on — and it is a separate query on purpose, see
        # duplicate_urls() for why the subtraction it replaced was wrong in both size and sign.
        "duplicate_content": duplicate_urls(),
        "empty": {"no_text": int(no_text), "no_html": int(no_html), "no_title": int(no_title),
                  "no_hash": int(no_hash), "no_source": int(no_source),
                  "no_fetched_at": int(no_fetched_at)},
        "oldest_fetch": str(oldest) if oldest else None,
        "newest_fetch": str(newest) if newest else None,
    }


# Same bytes under several URLs, keyed PER SOURCE. Ranked shortest-URL-first, which reliably picks
# the canonical one: /contact-us/manufacturing over /contact-us/products/?Part=2694N&QuestionTopic=…,
# and /login over the ?back=<escaped self> chain that generated 78 URLs for one login page. `url` is
# the tie-break so the ranking is total and a re-run is a no-op rather than a different answer.
#
# DELETES BY (source_id, content_hash, url), never by url alone. A URL whose content CHANGED has
# several revisions under different hashes; this drops only the revisions whose bytes are duplicated
# elsewhere and leaves any revision that was unique — so a page that was once distinct keeps its row.
#
# Touches four scalar columns and never `html`/`main_text`: reading those would detoast the whole
# corpus, which is exactly how a `length(main_text)` in a view's ORDER BY once pinned this database.
#
# `%(scope)s` narrows the whole pass to one site. It exists because this function is CORPUS-WIDE by
# default and the tests that exercise it run against the shared store: an `apply=True` in a test is
# otherwise a production delete wearing a unit test's name. It found its purpose immediately — five
# such tests deduplicated the live corpus on their first green run.
_DUP_RANK = """
    SELECT source_id, content_hash, url,
           row_number() OVER (PARTITION BY source_id, content_hash
                              ORDER BY length(url), url) AS rn
    FROM (SELECT DISTINCT source_id, content_hash, url FROM documents
          WHERE content_hash IS NOT NULL AND content_hash <> '' %(scope)s) d
"""


# THE ROWS THAT MUST SURVIVE ANYWAY. Identical HTML does not guarantee an identical attachment
# list: two captures of the same page can differ in which PDFs were reachable at the time, and
# measured on this corpus 264 stored PDFs hung off nothing BUT a row this pass would have deleted.
# MinIO is content-addressed and shared, so the blob would have stayed on disk — permanently
# unreferenced, which is worse than either keeping or deleting it: storage paid for, nothing able
# to find it. A row holding a storage_path no survivor holds is therefore kept, ugly URL and all.
_DUP_PROTECTED = """
    kept_paths AS (
      SELECT DISTINCT a->>'storage_path' AS p
      FROM documents t JOIN r USING (source_id, content_hash, url),
           LATERAL jsonb_array_elements(t.attachments) a
      WHERE r.rn = 1 AND a->>'storage_path' IS NOT NULL),
    protected AS (
      SELECT DISTINCT t.source_id, t.content_hash, t.url
      FROM documents t JOIN r USING (source_id, content_hash, url),
           LATERAL jsonb_array_elements(t.attachments) a
      WHERE r.rn > 1 AND a->>'storage_path' IS NOT NULL
        AND a->>'storage_path' NOT IN (SELECT p FROM kept_paths)),
    doomed AS (
      SELECT r.source_id, r.content_hash, r.url FROM r
      WHERE r.rn > 1 AND NOT EXISTS (
        SELECT 1 FROM protected p WHERE p.source_id = r.source_id
          AND p.content_hash = r.content_hash AND p.url = r.url))
"""


def dedupe(apply: bool = False, sample: int = 20, source_id: str = "") -> dict:
    """Collapse same-bytes-different-url pages down to one URL each.

    What this removes is a URL RECORD, never content: every deleted row's bytes stay in the corpus
    under the URL that is kept, so the page count falls and the page set does not. MinIO is left
    completely alone — blobs are content-addressed and shared between rows, so deleting one here
    would punch a hole in a row that survives.

    Two things are checked rather than assumed, both because the cheap version of this quietly loses
    data: that every group has a survivor, and that no PDF ends up referenced by nothing (see
    _DUP_PROTECTED — 264 of them did, on the first real run).

    Dry by default. `apply=False` reports the same counts and a sample of exactly which URLs would
    go, because a five-figure delete is worth seeing the shape of before acting on it.

    `source_id` narrows everything — counts, sample and delete — to one site. Pass it from anything
    that is not a deliberate whole-corpus sweep.
    """
    # The scope is a BOUND PARAMETER, not interpolated text: the two statements below are built with
    # `%` because the CTEs are code, and a source_id spliced into that string would be an injection
    # straight into a DELETE. The fragment carries a placeholder; the value travels separately, and
    # it appears in both statements so the parameter tuples repeat it once per occurrence.
    scope = " AND source_id = %s" if source_id else ""
    rank = _DUP_RANK % {"scope": scope}
    args = (source_id,) if source_id else ()

    counts = query_strict("""
        WITH r AS (%s), %s
        SELECT (SELECT count(*) FROM r WHERE rn > 1),
               (SELECT count(*) FROM doomed),
               (SELECT count(*) FROM protected),
               (SELECT count(DISTINCT (source_id, content_hash)) FROM doomed),
               (SELECT count(DISTINCT source_id) FROM doomed),
               (SELECT count(*) FROM (SELECT source_id, content_hash FROM r GROUP BY 1,2
                                      HAVING count(*) FILTER (WHERE rn = 1) = 0) x)
        """ % (rank, _DUP_PROTECTED), args)
    if counts is None:
        return {"error": "no DATABASE_URL configured"}
    redundant, doomed, protected, groups, sources, orphan_groups = (int(x or 0) for x in counts[0])

    shape = query("""
        WITH r AS (%s), %s, k AS (SELECT source_id, content_hash, url FROM r WHERE rn = 1)
        SELECT k.url, d.url, d.source_id
        FROM doomed d JOIN k USING (source_id, content_hash)
        ORDER BY length(d.url) DESC LIMIT %%s""" % (rank, _DUP_PROTECTED), args + (sample,))

    out = {"redundant_urls": doomed, "groups": groups, "sources": sources,
           "kept_for_attachments": protected, "duplicates_found": redundant,
           "applied": bool(apply),
           "sample": [{"keeping": k, "deleting": d, "source_id": s} for k, d, s in shape]}
    # A group whose only rows are all ranked >1 is arithmetically impossible, so this is a guard
    # against the ranking itself being wrong — the one failure mode that would delete real content.
    if orphan_groups:
        out["error"] = ("refusing: %d content group(s) would lose every copy" % orphan_groups)
        return out
    if not apply or not doomed:
        return out

    deleted = query_strict("""
        WITH r AS (%s), %s
        DELETE FROM documents t USING doomed d
        WHERE t.source_id = d.source_id AND t.content_hash = d.content_hash AND t.url = d.url
        RETURNING 1""" % (rank, _DUP_PROTECTED), args)
    out["rows_deleted"] = len(deleted or [])
    # Re-read rather than assume: the whole point of this pass is that the number on the dashboard
    # is measured, and "we deleted N so it must be zero now" is not a measurement.
    out["duplicate_content_after"] = duplicate_urls()
    return out


def duplicate_urls() -> int:
    """URL records whose exact bytes already exist under another URL OF THE SAME SITE.

    Replaces `count(DISTINCT url) - count(DISTINCT content_hash)`, which is not this number and on
    this corpus was not even the right SIGN: a URL re-crawled after it changed contributes several
    hashes, so hashes outgrew urls and the subtraction went to -22,298 — clamped to 0 by a max(),
    and therefore reading as a clean corpus by accident rather than by measurement.

    This counts what the dedupe pass would act on: per (site, bytes), every copy after the first.
    """
    r = query("""SELECT count(*) - count(DISTINCT (source_id, content_hash))
                 FROM (SELECT DISTINCT source_id, content_hash, url FROM documents
                       WHERE content_hash IS NOT NULL AND content_hash <> '') d""")
    return int(r[0][0] or 0) if r else 0


def by_served_by() -> list[dict]:
    """Pages per fetch tier — served_by is which lane won the page (c1_live/c2_wayback/c3_camofox/
    feed_api). No LIMIT: the cardinality is the fetcher list, a handful of rows."""
    rows = query("""SELECT served_by, count(*), coalesce(sum(html_len), 0)
                    FROM documents_current GROUP BY served_by
                    ORDER BY count(*) DESC""")
    # Pre-C2 rows predate the column, so NULL here means "we no longer know which lane fetched it" —
    # a real bucket the operator should see, not a row to drop.
    return [{"served_by": r[0] or "(unknown)", "pages": r[1], "html_bytes": int(r[2])} for r in rows]


# Data-type predicates, keyed by the name the dashboard sends. A LOOKUP, not string building: the
# caller's value selects a fixed fragment or nothing at all, so an arbitrary `dtype` string can
# never reach the statement text. Every *value* below is still bound separately.
_TYPE_PREDICATE = {
    "html": "coalesce(html_len, 0) > 0",
    # NOT `main_text <> ''` — that detoasts every row to ask a question text_len answers as an int.
    # `html` above was already converted; this one was missed, and it is the predicate behind the
    # dashboard's "text" filter, i.e. a click away at any time.
    "text": "coalesce(text_len, length(main_text), 0) > 0",
    "pdf":  "attachments IS NOT NULL AND jsonb_array_length(attachments) > 0",
}


# ── Pushing the local store up to the data centre ────────────────────────────
# The crawler normally writes straight to the centre. When it is unreachable the run lands locally
# instead of being lost, and these turn that local landing back into a sync.

def push_backlog() -> dict:
    """How much of the local store the centre has not accepted yet."""
    r = query("""SELECT count(*) FILTER (WHERE pushed_at IS NULL),
                        count(*) FILTER (WHERE pushed_at IS NOT NULL),
                        count(*), max(pushed_at)
                 FROM documents_current""")
    if not r:
        return {"error": "no-db"}
    pending, pushed, total, last = r[0]
    return {"pending": pending, "pushed": pushed, "documents": total,
            "last_push_at": str(last) if last else None}


def unpushed(limit: int = 200) -> list[dict]:
    """The next slice to send, oldest first so a long backlog drains in crawl order.

    Reads documents_current (one row per URL, the BEST capture) rather than `documents`, so a page
    captured three times is pushed once — sending every revision would triple the wire cost and
    hand the centre older copies to reconcile.
    """
    rows = query("""SELECT document_id, url, title, source_id, served_by, main_text, html,
                           attachments, ingested_at, html_len, crawl_run_id, node_id
                    FROM documents_current WHERE pushed_at IS NULL
                    ORDER BY ingested_at LIMIT %s""", (min(limit, 1000),))
    cols = ("document_id", "url", "title", "source_id", "served_by", "main_text", "html",
            "attachments", "ingested_at", "html_len", "crawl_run_id", "node_id")
    return [dict(zip(cols, r)) for r in rows]


def mark_pushed(document_ids: list[str]) -> int:
    """Stamp only what the centre actually accepted.

    A real connection, not `query`: that helper returns [] on any exception, so a failed stamp would
    look like a success and those documents would never be re-sent — silent, permanent divergence
    between the two stores.
    """
    if not document_ids:
        return 0
    url = os.environ.get("DATABASE_URL")
    if not url:
        return 0
    import psycopg
    with psycopg.connect(_norm_url(url)) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE documents SET pushed_at = now() WHERE document_id = ANY(%s)",
                        (list(document_ids),))
            n = cur.rowcount
        conn.commit()
    return n


def preview_source(source_id: str) -> dict:
    """What deleting this source would remove. Read-only — the operator sees it before confirming."""
    r = query("""SELECT count(*), coalesce(sum(html_len),0), min(ingested_at), max(ingested_at)
                 FROM documents WHERE source_id = %s""", (source_id,))
    if not r:
        return {"error": "no-db"}
    n, b, first, last = r[0]
    files = query("""SELECT count(*) FROM documents d,
                     LATERAL jsonb_array_elements(coalesce(d.attachments,'[]'::jsonb)) a
                     WHERE d.source_id = %s AND a->>'storage_path' IS NOT NULL""", (source_id,))
    return {"source_id": source_id, "documents": n, "html_bytes": int(b),
            "attachments": (files[0][0] if files else 0),
            "first_ingested": str(first) if first else None,
            "last_ingested": str(last) if last else None}


def document_text(document_id: str) -> dict | None:
    """The EXTRACTED text of one page (not the html). /raw-html serves the source and /artifact
    serves PDFs; the cleaned text — the thing this corpus actually exists to hold — had no route.
    """
    r = query("SELECT url, title, main_text, fetched_at, source_id, depth FROM documents "
              "WHERE document_id = %s LIMIT 1", (document_id,))
    if not r:
        return None
    url, title, text, fetched, src, depth = r[0]
    return {"document_id": document_id, "url": url, "title": title, "text": text or "",
            "fetched_at": str(fetched) if fetched else None, "source_id": src, "depth": depth}


def document_attachments(document_id: str) -> list[dict]:
    """Stored files (PDFs, images) for one page, as {name, storage_path, bytes}."""
    r = query("SELECT attachments FROM documents WHERE document_id = %s LIMIT 1", (document_id,))
    if not r or not r[0][0]:
        return []
    out = []
    for a in (r[0][0] or []):
        if not isinstance(a, dict):
            continue
        p = a.get("storage_path") or ""
        if p:
            out.append({"name": a.get("filename") or a.get("name") or p.rsplit("/", 1)[-1],
                        "storage_path": p, "bytes": a.get("bytes") or a.get("size")})
    return out


# Removed: a cap on how many depth levels get example URLs. It made small depths render "no
# examples" in the drill-down, and the seed at d0 is exactly the depth an operator opens first.
# The quadratic cost was the per-depth QUERY LOOP, not the number of sampled rows — see
# depth_breakdown.


def depth_breakdown(source_id: str, sample: int = 12) -> dict:
    """Pages per crawl DEPTH for one site, plus a few example URLs at each level.

    Depth is what makes a stored corpus legible: d0 is the seed, d1 the top nav, and the content
    that actually matters is usually d2+. A site with 14k pages all at d1 has been walked, not
    crawled — a shape that is invisible in a page COUNT and obvious the moment it is split by depth.

    USES text_len, NEVER length(main_text). That column exists for exactly this reason: main_text is
    TOASTed, so putting length() on it makes Postgres fetch and decompress every page's full text
    just to sort. Measured here on one 42k-page site: 203s with length(main_text), and the same
    answer in a fraction of that from the stored column. The identical mistake once made the
    documents_current view detoast 8 GB per read; writing it a second time is what these comments
    are for.
    """
    rows = query("""SELECT depth, count(*), coalesce(sum(text_len),0), coalesce(sum(html_len),0)
                    FROM (SELECT DISTINCT ON (url) url, depth, text_len, html_len
                          FROM documents WHERE source_id = %s
                          ORDER BY url, coalesce(text_len,0) DESC, fetched_at DESC) t
                    GROUP BY depth ORDER BY depth NULLS LAST""", (source_id,))
    if not rows:
        return {"source_id": source_id, "depths": [], "total_pages": 0}

    # ONE windowed query for every sample, not one query PER DEPTH.
    #
    # MEASURED on al-monitor.com: the rollup above is fast (1.5s for 80,738 pages) but returns
    # 1,820 DISTINCT DEPTHS — the crawler had followed a chain 1,826 links deep. The old loop then
    # issued a sample query for each one, so the drill-down fired 1,820 queries and the dialog
    # showed "could not read: timed out / no depth data / total 0 page(s)" over a site holding 80k
    # pages. The per-depth loop was fine when a site had 8 depths and quietly quadratic when it did
    # not, and nothing about a page COUNT hints at 1,820 levels.
    #
    # EVERY depth gets samples, not just the busiest ones.
    #
    # The first version of this fix sampled only the top _SAMPLE_DEPTHS by page count, on the
    # reasoning that a depth holding one page does not need three examples. That was wrong about
    # what the panel is FOR: the operator expands a depth to reach the page links — html, text and
    # the PDFs — so a depth with no examples is a dead end in the UI. Measured on the live
    # dashboard: d0 holds exactly 1 page, was therefore nowhere near the busiest 40, and rendered
    # "no examples" over a site with 80k pages. Small depths are often the MOST interesting ones,
    # because they are the seed and the top nav.
    #
    # Sampling every depth costs nothing extra in round trips — it is still ONE query — only rows:
    # 1,820 depths x a couple of dozen. That is the trade the per-depth LOOP could not make, and it
    # is the loop, not the row count, that caused the timeout.
    want = [r[0] for r in rows]
    real = [d for d in want if d is not None]
    samples: dict = {d: [] for d in want}
    if want:
        # NO DISTINCT ON. Deduplicating in SQL meant sorting every url at this depth before the
        # LIMIT could apply — on a 42k-page site that was 158 of the query's 169 seconds. Over-fetch
        # per depth with a window function and dedupe by url in Python: same speed, and the same
        # page no longer appears twice (mhi.com showed https://mhi.com/ twice at d0, once per
        # revision).
        cond = "depth = ANY(%s)" if not any(d is None for d in want) else \
               "(depth = ANY(%s) OR depth IS NULL)"
        ex = query(
            "SELECT depth, url, title, fetched_at, text_chars, document_id, pdfs FROM ("
            "  SELECT depth, url, title, fetched_at, coalesce(text_len,0) AS text_chars,"
            "         document_id,"
            "         coalesce(jsonb_array_length(coalesce(attachments,'[]'::jsonb)),0) AS pdfs,"
            "         row_number() OVER (PARTITION BY depth"
            "                            ORDER BY coalesce(text_len,0) DESC) AS rn"
            "  FROM documents WHERE source_id = %s AND " + cond +
            # Over-fetch enough to survive the Python dedupe below without pulling sample*4 for
            # every one of 1,820 depths — revisions of the same url are a handful, not a multiple.
            ") s WHERE rn <= %s", (source_id, real, sample + 8))
        for d, u, t, f, c, did, natt in ex:
            bucket = samples.get(d)
            if bucket is None or len(bucket) >= sample:
                continue
            if any(x["url"] == u for x in bucket):
                continue
            bucket.append({"url": u, "title": (t or "")[:120],
                           "fetched_at": str(f) if f else None, "text_chars": int(c),
                           "document_id": did, "pdfs": int(natt or 0)})

    out = [{"depth": depth, "pages": int(n), "text_chars": int(chars), "html_bytes": int(hb),
            "sample": samples.get(depth, [])}
           for depth, n, chars, hb in rows]
    return {"source_id": source_id, "depths": out,
            "total_pages": sum(d["pages"] for d in out),
            "sampled_depths": len(want), "distinct_depths": len(rows)}


def delete_source(source_id: str, *, delete_files: bool = True) -> dict:
    """Delete every document for one source. Irreversible.

    Uses a real transaction and a direct connection rather than `query`, which swallows exceptions
    and returns [] — on a destructive path a failed DELETE must never be indistinguishable from a
    successful one.

    Blobs are removed only when NO surviving document still references them. Attachment paths can be
    shared (the same PDF linked from two sites), and deleting a file another source still points at
    would leave that source with a dead storage_path — data loss in a place nobody would look.
    """
    if not source_id or not source_id.strip():
        raise ValueError("source_id is required — refusing to delete with an empty filter")
    url = os.environ.get("DATABASE_URL")
    if not url:
        return {"error": "no-db"}

    import psycopg
    orphans: list[str] = []
    with psycopg.connect(_norm_url(url)) as conn:
        with conn.cursor() as cur:
            if delete_files:
                cur.execute("""
                    WITH doomed AS (
                        SELECT DISTINCT a->>'storage_path' AS p FROM documents d,
                        LATERAL jsonb_array_elements(coalesce(d.attachments,'[]'::jsonb)) a
                        WHERE d.source_id = %s),
                         kept AS (
                        SELECT DISTINCT a->>'storage_path' AS p FROM documents d,
                        LATERAL jsonb_array_elements(coalesce(d.attachments,'[]'::jsonb)) a
                        WHERE d.source_id <> %s OR d.source_id IS NULL)
                    SELECT p FROM doomed
                    WHERE p IS NOT NULL AND p NOT IN (SELECT p FROM kept WHERE p IS NOT NULL)""",
                    (source_id, source_id))
                orphans = [r[0] for r in cur.fetchall()]
            cur.execute("DELETE FROM documents WHERE source_id = %s", (source_id,))
            deleted = cur.rowcount
        conn.commit()

    removed, failed = 0, []
    for path in orphans:
        try:
            _delete_blob(path)
            removed += 1
        except Exception as exc:                       # noqa: BLE001
            failed.append(f"{path}: {str(exc)[:80]}")
    return {"source_id": source_id, "documents_deleted": deleted,
            "files_deleted": removed, "files_orphaned": len(orphans),
            "file_errors": failed[:5]}


def delete_all_documents(*, delete_files: bool = True) -> dict:
    """Delete EVERY document and its blobs — the dashboard's "delete all" button. Irreversible.

    Deliberately NOT a loop over delete_source(): that would miss rows whose source_id is NULL or
    empty (delete_source refuses those by design), leaving a "delete all" that quietly left rows
    behind. One statement clears the table instead.

    There is also no keep-set to compute here — once the table is empty EVERY blob is orphaned, so
    the careful "is another source still pointing at this file?" check that delete_source needs is
    both unnecessary and wrong (it would spare files nothing references any more).
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        return {"error": "no-db"}

    import psycopg
    paths: list[str] = []
    with psycopg.connect(_norm_url(url)) as conn:
        with conn.cursor() as cur:
            if delete_files:
                cur.execute("""SELECT DISTINCT a->>'storage_path' FROM documents d,
                               LATERAL jsonb_array_elements(coalesce(d.attachments,'[]'::jsonb)) a
                               WHERE a->>'storage_path' IS NOT NULL""")
                paths = [r[0] for r in cur.fetchall()]
            cur.execute("DELETE FROM documents")
            deleted = cur.rowcount
        conn.commit()

    removed, failed = 0, []
    for path in paths:
        try:
            _delete_blob(path)
            removed += 1
        except Exception as exc:                       # noqa: BLE001
            failed.append(f"{path}: {str(exc)[:80]}")

    # Then SWEEP THE STORE, not just what the index remembered.
    #
    # Everything above is driven by storage_path values read out of `documents`. A blob whose row was
    # already gone — deleted by an earlier wipe, or written by a crawl whose document never landed —
    # is invisible to that query, so it survives every "delete all" for ever. Measured on the data
    # centre: the table went to 0 documents and the bucket still held 353 PDFs / 547 MB from a
    # previous corpus, and the dashboard showed "Documents 0" beside "MinIO 353 objects", which is a
    # contradiction an operator has no way to resolve.
    #
    # The docstring above already states the principle — once the table is empty EVERY blob is
    # orphaned — it just was not acted on. Scoped to the prefixes the crawler writes (img/, doc/,
    # shot/) rather than the whole bucket, because the bucket is not guaranteed to be ours alone and
    # a delete-all must never reach somebody else's objects.
    swept, sweep_failed = 0, []
    if delete_files:
        try:
            from crawler import config, storage
            client = storage._client()
            for sub in ("doc/", "img/", "shot/"):
                for obj in client.list_objects(config.MINIO_BUCKET, prefix=sub, recursive=True):
                    try:
                        client.remove_object(config.MINIO_BUCKET, obj.object_name)
                        swept += 1
                    except Exception as exc:           # noqa: BLE001
                        sweep_failed.append(f"{obj.object_name}: {str(exc)[:60]}")
        except Exception as exc:                       # noqa: BLE001 — report, never 500
            sweep_failed.append(f"sweep unavailable: {str(exc)[:120]}")

    return {"documents_deleted": deleted, "files_deleted": removed,
            "files_orphaned": len(paths),
            # Blobs the index had NO record of. Reported separately on purpose: a non-zero number
            # here means the store and the index had drifted, which is worth knowing even though
            # this call has just fixed it.
            "orphans_swept": swept,
            "file_errors": (failed + sweep_failed)[:5]}


def _delete_blob(storage_path: str) -> None:
    """Remove one object. storage_path is an s3:// URI written by the crawler's storage module."""
    from crawler import config, storage
    prefix = f"s3://{config.MINIO_BUCKET}/"
    if not storage_path.startswith(prefix):
        raise ValueError(f"refusing to delete outside {prefix}")
    storage._client().remove_object(config.MINIO_BUCKET, storage_path[len(prefix):])


def recent(limit: int = 100, source: str = "", sources: list[str] | None = None,
           served_by: str = "", dtype: str = "", since: str = "", until: str = "") -> list[dict]:
    """Most recently ingested documents, narrowed by source / fetch tier / data type.

    `sources` is a LIST because the dashboard filters by catalog CATEGORY, which `documents` has no
    column for — the orchestrator resolves the category to its domains and passes them here, so the
    filtering still happens in SQL rather than by fetching a page and discarding most of it (which
    would silently under-report: LIMIT applies before a client-side filter, not after).
    """
    where, params = [], []
    if source:
        where.append("source_id = %s")
        params.append(source)
    if sources:
        where.append("source_id = ANY(%s)")
        params.append(list(sources))
    if served_by:
        where.append("served_by = %s")
        params.append(served_by)
    pred = _TYPE_PREDICATE.get((dtype or "").lower())
    if pred:
        where.append(pred)
    # A TIME WINDOW, so "what did this re-crawl actually bring in" is answerable. `ingested_at` is
    # stored as TEXT in ISO-8601 (…Z), which sorts lexicographically in the same order as it does
    # chronologically, so a plain string comparison is correct here and needs no cast.
    if since:
        where.append("ingested_at >= %s")
        params.append(since)
    if until:
        where.append("ingested_at <= %s")
        params.append(until)
    sql = "SELECT url, title, source_id, html_len, served_by, ingested_at FROM documents"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ingested_at DESC LIMIT %s"
    params.append(min(limit, 500))
    rows = query(sql, tuple(params))
    return [{"url": r[0], "title": r[1], "source_id": r[2], "html_len": r[3],
             "served_by": r[4], "ingested_at": str(r[5]) if r[5] else None} for r in rows]


# ── Corpus search ────────────────────────────────────────────────────────────
# Two tiers: title/url (trigram GIN, always on) and page BODY text (tsvector GIN, on once the index
# is built). DETOAST-SAFE — projects only scalar columns; the body match is answered by the GIN index
# on to_tsvector('simple', main_text), so no row is decompressed at query time. The tiers run as
# SEPARATE index-served queries and are NEVER OR'd into one clause: an OR mixing a trigram ILIKE with
# the tsvector @@ can tip the planner into a seq scan that evaluates to_tsvector(main_text) per row —
# i.e. detoast the whole corpus, the exact outage class this module guards against.
_BODY_TSV_READY = False


def _body_search_ready() -> bool:
    """True once idx_docs_tsv (GIN on to_tsvector('simple', main_text)) exists AND is valid. Until
    then search stays on the metadata tier, so a corpus mid-index-build never breaks the box. Cached
    like has_db() — the index only ever goes absent→present, never back."""
    global _BODY_TSV_READY
    if _BODY_TSV_READY:
        return True
    if query("SELECT 1 FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
             "WHERE c.relname = 'idx_docs_tsv' AND i.indisvalid"):
        _BODY_TSV_READY = True
    return _BODY_TSV_READY


def _search_filters(source, sources, dtype, since, until):
    """Shared WHERE tail (clauses, bound params) for both search tiers. Scalar columns only."""
    where, params = [], []
    if source:
        where.append("source_id = %s"); params.append(source)
    if sources:
        where.append("source_id = ANY(%s)"); params.append(list(sources))
    pred = _TYPE_PREDICATE.get((dtype or "").lower())
    if pred:
        where.append(pred)
    if since:
        where.append("ingested_at >= %s"); params.append(since)
    if until:
        where.append("ingested_at <= %s"); params.append(until)
    return where, params


# NOTE: the tsvector expression below MUST stay character-identical to the CREATE INDEX expression
# (`to_tsvector('simple', main_text)`), or the planner won't match the functional index and will
# seq-scan → detoast. Do not "improve" 'simple' to 'english' etc. without rebuilding the index.
_SEARCH_COLS = ("SELECT document_id, url, title, source_id, coalesce(html_len, 0), depth, fetched_at "
                "FROM documents")
_SEARCH_BODY_MATCH = "to_tsvector('simple', main_text) @@ plainto_tsquery('simple', %s)"


def search(q: str, limit: int = 100, source: str = "", sources: list[str] | None = None,
           dtype: str = "", since: str = "", until: str = "", body: bool = True) -> dict:
    """Search the corpus by title/url (always) and body text (once idx_docs_tsv is built).

    `q` is BOUND, never formatted in. `sources` is the category→domains list (same as recent(): the
    corpus has no category column, the orchestrator resolves it). Returns
    {"body_ready": bool, "rows": [{url,title,source_id,html_len,depth,fetched_at}]}.

    ponytail: reads the base `documents` table (where the indexes live) so re-crawls can appear twice;
    dedupes by url in Python keeping the newest. Fine at page size; revisit if someone pages deep.
    """
    q = (q or "").strip()
    if not q:
        return {"body_ready": _body_search_ready(), "rows": []}
    lim = min(max(limit, 1), 200)
    fwhere, fparams = _search_filters(source, sources, dtype, since, until)
    seen, out = set(), []

    def run(match_sql, match_params):
        sql = (_SEARCH_COLS + " WHERE " + " AND ".join([match_sql] + fwhere)
               + " ORDER BY ingested_at DESC LIMIT %s")
        for r in query(sql, tuple(match_params + fparams + [lim * 3])):
            if r[1] in seen:                       # dedupe re-crawl rows by url (r[0] is document_id)
                continue
            seen.add(r[1])
            out.append({"document_id": r[0], "url": r[1], "title": r[2], "source_id": r[3],
                        "html_len": r[4], "depth": r[5], "fetched_at": str(r[6]) if r[6] else None})

    like = f"%{q}%"
    run("(title ILIKE %s OR url ILIKE %s)", [like, like])   # metadata tier — trigram indexes
    used_body = bool(body) and _body_search_ready()
    if used_body:
        run(_SEARCH_BODY_MATCH, [q])                        # body tier — tsvector GIN index
    return {"body_ready": used_body, "rows": out[:lim]}

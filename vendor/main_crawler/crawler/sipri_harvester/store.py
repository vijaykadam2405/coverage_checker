"""Storage for the SIPRI harvester — its own Postgres schema and its own MinIO bucket.

Deliberately separate from the crawler's `documents` table and `mallory-raw` bucket: SIPRI is
reference data on an annual refresh cadence, not crawled pages. Both names come from
config.PG_SCHEMA / config.MINIO_BUCKET and are read at CALL time, never baked in here.

Degrades like ingest_api.pgstore when there is no DATABASE_URL / MinIO — a harvest keeps going
rather than dying on a storage blip — with one deliberate difference: every degraded path returns a
FALSY result and bumps a counter that stats() reports. A silent no-op is exactly how documents
vanished before (ingest returned 200 while the Postgres insert failed on a NOT NULL primary key),
so here the caller can always count what did not land.
"""
from __future__ import annotations

import io
import os
import re
import sys
import threading
import time as _time

from . import config

# ── connection state (mirrors ingest_api.pgstore: lazy, cooldown-retried, never fatal) ──
_LOCK = threading.Lock()
_CONN = None
_RETRY_AFTER = 0.0            # monotonic ts; skip reconnect attempts until then
_RETRY_COOLDOWN_S = 30
_NO_DB_WARNED = False

_ERRORS = 0                   # anything that did not land, counted for the end-of-run report
_LAST_ERROR: str | None = None


def _fail(msg: str) -> None:
    """Every degraded path funnels here: count it and SHOUT. The counter is what turns a lost
    write into something the caller can report instead of something nobody notices."""
    global _ERRORS, _LAST_ERROR
    _ERRORS += 1
    _LAST_ERROR = msg
    print(f"[sipri.store] {msg}", file=sys.stderr, flush=True)


def _norm_url(url: str) -> str:
    # accept a SQLAlchemy-style URL too (L2's compose uses postgresql+psycopg://)
    return (url.replace("postgresql+psycopg://", "postgresql://")
               .replace("postgresql+psycopg2://", "postgresql://"))


def _connect():
    """Open the connection and ensure the schema. Raises on failure (caller handles)."""
    global _CONN
    import psycopg
    conn = psycopg.connect(_norm_url(os.environ["DATABASE_URL"]), autocommit=True, connect_timeout=5)
    with conn.cursor() as cur:
        cur.execute(_ddl())   # idempotent, and re-run on reconnect so a restored DB comes back ready
    _CONN = conn
    return conn


def _get():
    """A live connection, or None. A failed connect is TRANSIENT — retried after a cooldown rather
    than latching off forever, which is what black-holed a whole crawl's writes once."""
    global _CONN, _RETRY_AFTER, _NO_DB_WARNED
    if _CONN is not None:
        return _CONN
    if not os.environ.get("DATABASE_URL"):
        if not _NO_DB_WARNED:                    # a config choice, not a fault: say it once
            _NO_DB_WARNED = True
            print("[sipri.store] DATABASE_URL unset - nothing will be persisted to Postgres",
                  file=sys.stderr, flush=True)
        return None
    if _time.monotonic() < _RETRY_AFTER:
        return None
    with _LOCK:
        if _CONN is None:
            try:
                _connect()
            except Exception as exc:  # noqa: BLE001
                _RETRY_AFTER = _time.monotonic() + _RETRY_COOLDOWN_S
                _fail(f"connect failed (retry in {_RETRY_COOLDOWN_S}s): {exc}")
    return _CONN


def _conn_for(op: str):
    """Connection or None, counting the loss against `op` so the caller sees WHAT was dropped."""
    conn = _get()
    if conn is None:
        _fail(f"{op}: no database - nothing written")
    return conn


def _drop_if_dead(conn) -> None:
    """Discard a broken connection so the next _get() reconnects; a dead one fails forever."""
    global _CONN, _RETRY_AFTER
    if getattr(conn, "closed", False) or getattr(conn, "broken", False):
        _CONN = None
        _RETRY_AFTER = _time.monotonic() + _RETRY_COOLDOWN_S


# ── schema ───────────────────────────────────────────────────────────────────
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TABLES = ("arms_transfers", "datasets", "documents")


def _schema() -> str:
    """The configured schema name, validated. An identifier can never be a bound parameter, so it
    reaches SQL by interpolation — refuse anything that is not a plain identifier."""
    name = config.PG_SCHEMA
    if not _IDENT_RE.match(name or ""):
        raise ValueError(f"invalid PG_SCHEMA {name!r} — must be a plain SQL identifier")
    return name


def _table(name: str) -> str:
    return f"{_schema()}.{name}"


def _ddl() -> str:
    """Every DDL statement, built from config at call time so the schema name is never hardcoded.
    Create-if-not-exists throughout: there is no Alembic in this project and this runs every harvest.
    """
    s = _schema()
    return f"""
CREATE SCHEMA IF NOT EXISTS {s};

CREATE TABLE IF NOT EXISTS {s}.arms_transfers (
    id           BIGINT PRIMARY KEY,
    trade_id     INTEGER,
    buyer        TEXT,
    seller       TEXT,
    order_yr     INTEGER,
    order_yr_est BOOLEAN,
    units        INTEGER,
    units_est    BOOLEAN,
    delivery_yr  INTEGER,
    status       TEXT,
    status_est   BOOLEAN,
    desg         TEXT,
    descr        TEXT,
    category     TEXT,
    raw          JSONB,
    harvested_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arms_transfers_buyer_idx  ON {s}.arms_transfers (buyer);
CREATE INDEX IF NOT EXISTS arms_transfers_seller_idx ON {s}.arms_transfers (seller);

CREATE TABLE IF NOT EXISTS {s}.datasets (
    key          TEXT PRIMARY KEY,
    title        TEXT,
    source_url   TEXT,
    landing_url  TEXT,
    kind         TEXT,
    sha256       TEXT,
    bytes        BIGINT,
    minio_key    TEXT,
    harvested_at TIMESTAMPTZ DEFAULT now(),
    row_count    INTEGER
);

CREATE TABLE IF NOT EXISTS {s}.documents (
    url          TEXT PRIMARY KEY,
    title        TEXT,
    html_len     INTEGER,
    sha256       TEXT,
    minio_key    TEXT,
    harvested_at TIMESTAMPTZ DEFAULT now()
);

-- Attachments (PDFs) get their OWN table rather than joining documents, because `documents` is
-- what the dashboard counts as "pages". Filing 800 PDFs there would inflate the page count and
-- put a byte length in a column named html_len — two numbers made wrong to save one table.
-- `found_on` keeps the page each file was linked from, which is the only way to tell a Wassenaar
-- guideline reached via the ATT library from one reached anywhere else.
CREATE TABLE IF NOT EXISTS {s}.attachments (
    url          TEXT PRIMARY KEY,
    found_on     TEXT,
    title        TEXT,
    content_type TEXT,
    bytes        BIGINT,
    sha256       TEXT,
    minio_key    TEXT,
    status       TEXT,          -- stored | skipped_not_pdf | too_large | http_<n> | error
    detail       TEXT,
    harvested_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS attachments_status_idx   ON {s}.attachments (status);
CREATE INDEX IF NOT EXISTS attachments_found_on_idx ON {s}.attachments (found_on);
"""


def ensure_schema() -> bool:
    """Create the schema and tables if missing. Idempotent — safe to call every run.
    False means no database, i.e. this harvest will persist nothing: check it before harvesting."""
    if _get() is None:
        _fail("ensure_schema: no database - this harvest will not be persisted")
        return False
    return True


# ── arms transfers ───────────────────────────────────────────────────────────
# API field -> column, in INSERT order. `desc` is a reserved SQL word, so the column is `descr`.
# The *_est flags mark a figure as SIPRI's ESTIMATE rather than a reported fact and are stored as
# given: a missing flag is NULL, never False — defaulting it would promote a guess to reported data.
_TRANSFER_MAP = {
    "id": "id",
    "tradeId": "trade_id",
    "buyer": "buyer",
    "seller": "seller",
    "orderYr": "order_yr",
    "orderYrEst": "order_yr_est",
    "units": "units",
    "unitsEst": "units_est",
    "deliveryYr": "delivery_yr",
    "status": "status",
    "statusEst": "status_est",
    "desg": "desg",
    "desc": "descr",
    "category": "category",
}
_TRANSFER_KEYS = list(_TRANSFER_MAP)                    # API keys, in column order
_TRANSFER_COLS = list(_TRANSFER_MAP.values())
_UPDATE_SET = ", ".join(f"{c} = EXCLUDED.{c}" for c in _TRANSFER_COLS if c != "id")
_UPDATE_SET += ", raw = EXCLUDED.raw, harvested_at = now()"

_BATCH = 1000     # 1000 rows x 15 bound params = 15k, well inside Postgres' 65535-per-statement cap


def upsert_transfers(rows: list[dict]) -> int:
    """Bulk upsert arms-transfer records; returns rows written.

    Compare the result against len(rows): a short count means something was dropped and
    stats()["errors"] says what. One statement per _BATCH rows keeps a 30k refresh at ~30 round
    trips instead of 30k.
    """
    if not rows:
        return 0
    conn = _conn_for("upsert_transfers")
    if conn is None:
        return 0
    try:
        from psycopg.types.json import Jsonb
    except Exception as exc:  # noqa: BLE001 — a missing driver must not look like an empty harvest
        _fail(f"upsert_transfers: psycopg unavailable ({exc}) - {len(rows)} rows not written")
        return 0

    # The search API paginates with sorts={} (no ORDER BY), so one record can legitimately arrive on
    # two pages. Postgres refuses to let ON CONFLICT DO UPDATE touch the same row twice in ONE
    # statement, so collapse duplicates here (last wins) rather than lose an entire batch to it.
    by_id: dict = {}
    no_id = 0
    for r in rows:
        if r.get("id") is None:
            no_id += 1                  # no primary key = unstorable; count it, never invent one
            continue
        by_id[r["id"]] = r
    if no_id:
        _fail(f"upsert_transfers: {no_id} record(s) had no id and were skipped")

    cols = _TRANSFER_COLS + ["raw"]
    head = f"INSERT INTO {_table('arms_transfers')} ({', '.join(cols)}, harvested_at) VALUES "
    row_sql = "(" + ", ".join(["%s"] * len(cols)) + ", now())"
    tail = f" ON CONFLICT (id) DO UPDATE SET {_UPDATE_SET}"

    batch = list(by_id.values())
    written = 0
    try:
        with conn.cursor() as cur:
            for i in range(0, len(batch), _BATCH):
                chunk = batch[i:i + _BATCH]
                params: list = []
                for r in chunk:
                    params.extend(r.get(k) for k in _TRANSFER_KEYS)
                    params.append(Jsonb(r))    # the original object, so a new SIPRI field is never lost
                cur.execute(head + ", ".join([row_sql] * len(chunk)) + tail, params)
                written += len(chunk)          # autocommit: each chunk stands on its own
    except Exception as exc:  # noqa: BLE001
        _drop_if_dead(conn)
        _fail(f"upsert_transfers: {exc} - wrote {written} of {len(batch)}")
    return written


# ── dataset registry ─────────────────────────────────────────────────────────
_DATASET_COLS = ["key", "title", "source_url", "landing_url", "kind", "sha256", "bytes",
                 "minio_key", "row_count"]


def record_dataset(key: str, *, title: str = "", source_url: str = "", landing_url: str = "",
                   kind: str = "", sha256: str = "", size_bytes: int = 0, minio_key: str = "",
                   row_count: int | None = None) -> bool:
    """Register a harvested file dataset (size_bytes lands in the `bytes` column).

    Keyword-only because source_url and landing_url are both strings — transposing them positionally
    would be silent. False means the row did not land. Only call this once the blob is actually
    stored: the sha256 here is what a later run compares against to decide "unchanged, skip".
    """
    conn = _conn_for(f"record_dataset({key})")
    if conn is None:
        return False
    vals = [key, title, source_url, landing_url, kind, sha256, size_bytes, minio_key, row_count]
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in _DATASET_COLS if c != "key")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {_table('datasets')} ({', '.join(_DATASET_COLS)}, harvested_at) "
                f"VALUES ({', '.join(['%s'] * len(_DATASET_COLS))}, now()) "
                f"ON CONFLICT (key) DO UPDATE SET {sets}, harvested_at = now()",
                vals)
        return True
    except Exception as exc:  # noqa: BLE001
        _drop_if_dead(conn)
        _fail(f"record_dataset({key}): {exc}")
        return False


def get_dataset(key: str) -> dict | None:
    """The registry row for `key`, or None if never harvested — OR if the DB is unreachable.

    Both answers push the caller toward re-harvesting, which is the safe direction to fail: the cost
    is re-downloading a file, versus skipping one that was never actually stored.
    """
    conn = _conn_for(f"get_dataset({key})")
    if conn is None:
        return None
    cols = _DATASET_COLS + ["harvested_at"]
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(cols)} FROM {_table('datasets')} WHERE key = %s", (key,))
            row = cur.fetchone()
        return dict(zip(cols, row)) if row else None
    except Exception as exc:  # noqa: BLE001
        _drop_if_dead(conn)
        _fail(f"get_dataset({key}): {exc}")
        return None


# ── browsable HTML databases ─────────────────────────────────────────────────
_DOC_COLS = ["url", "title", "html_len", "sha256", "minio_key"]


def record_document(url: str, *, title: str = "", html_len: int = 0, sha256: str = "",
                    minio_key: str = "") -> bool:
    """Record one harvested HTML page (embargoes, national reports, ...). False = not written."""
    conn = _conn_for(f"record_document({url})")
    if conn is None:
        return False
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in _DOC_COLS if c != "url")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {_table('documents')} ({', '.join(_DOC_COLS)}, harvested_at) "
                f"VALUES ({', '.join(['%s'] * len(_DOC_COLS))}, now()) "
                f"ON CONFLICT (url) DO UPDATE SET {sets}, harvested_at = now()",
                [url, title, html_len, sha256, minio_key])
        return True
    except Exception as exc:  # noqa: BLE001
        _drop_if_dead(conn)
        _fail(f"record_document({url}): {exc}")
        return False


# ── attachments (PDFs linked from harvested pages) ───────────────────────────
_ATT_COLS = ["url", "found_on", "title", "content_type", "bytes", "sha256", "minio_key",
             "status", "detail"]


def record_attachment(url: str, *, found_on: str = "", title: str = "", content_type: str = "",
                      size_bytes: int = 0, sha256: str = "", minio_key: str = "",
                      status: str = "stored", detail: str = "") -> bool:
    """Record one attachment attempt — including the ones that did NOT store.

    A refusal is written too, on purpose: without a row, every re-run would re-fetch the same
    404s and HTML-error-pages-pretending-to-be-PDFs forever, and nobody could see WHY a document
    the library lists is missing. `status` carries which of those happened.
    """
    conn = _conn_for(f"record_attachment({url})")
    if conn is None:
        return False
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in _ATT_COLS if c != "url")
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {_table('attachments')} ({', '.join(_ATT_COLS)}, harvested_at) "
                f"VALUES ({', '.join(['%s'] * len(_ATT_COLS))}, now()) "
                f"ON CONFLICT (url) DO UPDATE SET {sets}, harvested_at = now()",
                [url, found_on, title, content_type, size_bytes, sha256, minio_key, status, detail])
        return True
    except Exception as exc:  # noqa: BLE001
        _drop_if_dead(conn)
        _fail(f"record_attachment({url}): {exc}")
        return False


def attachment_urls_done() -> set:
    """URLs already attempted, so a re-run resumes instead of re-fetching. Empty set on no DB."""
    conn = _conn_for("attachment_urls_done")
    if conn is None:
        return set()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT url FROM {_table('attachments')}")
            return {r[0] for r in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001
        _drop_if_dead(conn)
        _fail(f"attachment_urls_done: {exc}")
        return set()


# ── object store ─────────────────────────────────────────────────────────────
# Same MinIO SERVER as the crawler (shared MINIO_* env, defaults mirroring crawler/config.py) but a
# different BUCKET — config.MINIO_BUCKET. Read at call time so tests and deployments can repoint it.
_minio_client = None
_no_minio_warned = False


def _minio():
    """The MinIO client, bucket ensured — or None. Rebuilt on every failure rather than latched off:
    a harvest stores a handful of blobs, so retrying costs little and losing them all costs a lot."""
    global _minio_client, _no_minio_warned
    if _minio_client is not None:
        return _minio_client
    endpoint = os.environ.get("MINIO_ENDPOINT", "")
    if not endpoint:
        if not _no_minio_warned:
            _no_minio_warned = True
            print("[sipri.store] MINIO_ENDPOINT unset - raw artifacts will not be stored",
                  file=sys.stderr, flush=True)
        return None
    try:
        from minio import Minio
        client = Minio(endpoint,
                       access_key=os.environ.get("MINIO_ACCESS_KEY", "mallory"),
                       secret_key=os.environ.get("MINIO_SECRET_KEY", "mallory123"),
                       secure=os.environ.get("MINIO_SECURE", "0") == "1")
        if not client.bucket_exists(config.MINIO_BUCKET):
            client.make_bucket(config.MINIO_BUCKET)
        _minio_client = client
    except Exception as exc:  # noqa: BLE001
        _fail(f"minio unavailable: {exc}")
    return _minio_client


def put_blob(key: str, data: bytes, content_type: str) -> str | None:
    """Store a raw artifact (xlsx / json / html) in the SIPRI bucket; returns the object key.

    None means it did NOT land — do not record_dataset() a blob that failed to store, or the sha256
    skip-check will treat the missing artifact as already harvested and never fetch it again.
    Deliberately no local-disk fallback: the crawler's silent one hid a missing package for a whole
    crawl, and unlike a crawl this harvest is cheap to repeat.
    """
    client = _minio()
    if client is None:
        _fail(f"put_blob({key}): no object store - {len(data)} bytes not stored")
        return None
    try:
        client.put_object(config.MINIO_BUCKET, key, io.BytesIO(data), length=len(data),
                          content_type=content_type)
        return key
    except Exception as exc:  # noqa: BLE001
        global _minio_client
        _minio_client = None                    # force a rebuild next call
        _fail(f"put_blob({key}): {exc} - {len(data)} bytes not stored")
        return None



def query(sql: str, params: tuple = ()) -> list[tuple]:
    """Read-only SELECT for the dashboard. Raises on failure so the caller can distinguish
    "no rows" from "no database" — silently returning [] for both is how an outage reads as an
    empty dataset."""
    conn = _get()
    if conn is None:
        raise RuntimeError("no database")
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    except Exception:
        _drop_if_dead(conn)
        raise


def stats() -> dict:
    """Row counts per table plus this process's error count — the end-of-run report.

    A count is None (not 0) when the DB cannot be read, so "we stored nothing" is never confused
    with "we could not tell".
    """
    counts: dict[str, int | None] = {t: None for t in _TABLES}
    conn = _get()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                for t in _TABLES:
                    cur.execute(f"SELECT count(*) FROM {_table(t)}")
                    counts[t] = cur.fetchone()[0]
        except Exception as exc:  # noqa: BLE001
            _drop_if_dead(conn)
            _fail(f"stats: {exc}")
    return {**counts, "errors": _ERRORS, "last_error": _LAST_ERROR}

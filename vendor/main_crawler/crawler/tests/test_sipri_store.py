"""SIPRI store: schema naming, field mapping, and failure visibility.

Runs with no Postgres and no MinIO — the connection layer is stubbed. What matters here is that
(a) the schema and bucket names always come from config rather than being hardcoded, (b) every
observed arms-transfer field survives the mapping INCLUDING the estimate flags, and (c) an outage
is reported to the caller instead of swallowed — a silent no-op is how documents vanished before.
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# store.py imports Jsonb lazily, so psycopg need not be installed to test the mapping. setdefault
# leaves a real psycopg alone if one is present; either way the wrapped object is on `.obj`.
_pg = types.ModuleType("psycopg")
_pg_types = types.ModuleType("psycopg.types")
_pg_json = types.ModuleType("psycopg.types.json")


class Jsonb:
    def __init__(self, obj):
        self.obj = obj


_pg_json.Jsonb = Jsonb
_pg_types.json = _pg_json
_pg.types = _pg_types
sys.modules.setdefault("psycopg", _pg)
sys.modules.setdefault("psycopg.types", _pg_types)
sys.modules.setdefault("psycopg.types.json", _pg_json)

from sipri_harvester import config, store  # noqa: E402

# A real record off /api/p/trades/search — the shape the columns must actually fit.
OBSERVED = {"id": 1239344, "tradeId": 2, "buyer": "United Arab Emirates", "seller": "France",
            "orderYr": 1976, "orderYrEst": False, "units": 100, "unitsEst": True,
            "deliveryYr": 1978, "status": "N", "statusEst": False, "desg": "R-440 Crotale",
            "desc": "surface-to-air missile", "category": "Missiles"}


class FakeCursor:
    def __init__(self, log, results):
        self.log, self.results = log, results

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))

    def fetchone(self):
        return self.results.pop(0) if self.results else None


class FakeConn:
    closed = False
    broken = False

    def __init__(self, results=None):
        self.log: list = []
        self.results = list(results or [])

    def cursor(self):
        return FakeCursor(self.log, self.results)


def use_conn(results=None) -> FakeConn:
    """Point the store at a fake connection and reset the error counter."""
    conn = FakeConn(results)
    store._get = lambda: conn
    store._ERRORS = 0
    store._LAST_ERROR = None
    return conn


def no_conn() -> None:
    store._get = lambda: None
    store._ERRORS = 0
    store._LAST_ERROR = None


def quiet():
    """Swallow the deliberate stderr shouting so the test output stays readable."""
    return contextlib.redirect_stderr(io.StringIO())


def row_of(params: list) -> dict:
    """column -> bound value for the first row of an INSERT."""
    return dict(zip(store._TRANSFER_COLS, params[:len(store._TRANSFER_COLS)]))


# ── schema naming ────────────────────────────────────────────────────────────
def t_ddl_uses_the_configured_schema() -> None:
    """The schema name must come from config; a hardcoded "sipri" ignores SIPRI_PG_SCHEMA."""
    old = config.PG_SCHEMA
    config.PG_SCHEMA = "sipri_alt"
    try:
        ddl = store._ddl()
        assert "CREATE SCHEMA IF NOT EXISTS sipri_alt;" in ddl, ddl
        for table in ("arms_transfers", "datasets", "documents"):
            assert f"sipri_alt.{table}" in ddl, f"{table} not created in the configured schema"
        assert "sipri." not in ddl, "the default schema name is hardcoded somewhere in the DDL"
    finally:
        config.PG_SCHEMA = old


def t_writes_use_the_configured_schema() -> None:
    old = config.PG_SCHEMA
    config.PG_SCHEMA = "sipri_alt"
    try:
        conn = use_conn(results=[None])
        store.upsert_transfers([OBSERVED])
        store.record_dataset("milex", sha256="a")
        store.record_document("https://www.sipri.org/databases/embargoes")
        store.get_dataset("milex")
        for sql, _params in conn.log:
            assert "sipri_alt." in sql, f"statement is not schema-qualified from config: {sql[:80]}"
    finally:
        config.PG_SCHEMA = old


def t_schema_name_is_validated() -> None:
    """PG_SCHEMA is an identifier, so it reaches SQL by interpolation and must be refused if odd."""
    old = config.PG_SCHEMA
    config.PG_SCHEMA = "x; DROP SCHEMA public CASCADE; --"
    try:
        raised = False
        try:
            store._ddl()
        except ValueError:
            raised = True
        assert raised, "an injected schema name reached the DDL unchallenged"
    finally:
        config.PG_SCHEMA = old


def t_ddl_is_idempotent() -> None:
    """No Alembic here: every run re-executes this, so every statement must be create-if-not-exists."""
    creates = [ln.strip() for ln in store._ddl().splitlines() if ln.strip().upper().startswith("CREATE")]
    # schema + 4 tables (arms_transfers, datasets, documents, attachments) + 4 indexes.
    # The count is pinned rather than checked loosely BECAUSE it catches exactly this: a table
    # added without its indexes, or an index added to a table nobody creates.
    assert len(creates) == 9, f"expected schema + 4 tables + 4 indexes, got {creates}"
    assert all("IF NOT EXISTS" in c.upper() for c in creates), creates
    for t in ("arms_transfers", "datasets", "documents", "attachments"):
        assert any(f".{t} (" in c for c in creates), f"{t} is not created: {creates}"


def t_storage_is_separate_from_the_crawler() -> None:
    """SIPRI must not land in the crawler's `documents` table or `mallory-raw` bucket."""
    ddl = store._ddl()
    assert "CREATE TABLE IF NOT EXISTS documents" not in ddl, "unqualified — that is the crawler's table"
    assert f"{config.PG_SCHEMA}.documents" in ddl
    assert config.MINIO_BUCKET != "mallory-raw", "SIPRI must not share the crawler's bucket"


def t_tables_cover_the_contract() -> None:
    ddl = store._ddl()
    for col in ("raw          JSONB", "harvested_at TIMESTAMPTZ"):
        assert col in ddl, f"arms_transfers is missing {col}"
    for col in ("key", "title", "source_url", "landing_url", "kind", "sha256", "bytes",
                "minio_key", "row_count"):
        assert col in ddl, f"datasets registry is missing {col}"


# ── arms transfers ───────────────────────────────────────────────────────────
def t_maps_every_observed_field() -> None:
    conn = use_conn()
    written = store.upsert_transfers([OBSERVED])
    assert written == 1
    sql, params = conn.log[0]
    row = row_of(params)
    assert row["id"] == 1239344 and row["trade_id"] == 2
    assert row["buyer"] == "United Arab Emirates" and row["seller"] == "France"
    assert row["order_yr"] == 1976 and row["units"] == 100 and row["delivery_yr"] == 1978
    assert row["status"] == "N" and row["category"] == "Missiles"
    assert row["desg"] == "R-440 Crotale"
    assert row["descr"] == "surface-to-air missile", "`desc` is a reserved word; column is `descr`"
    assert len(row) == len(OBSERVED), "an observed field has no column"
    assert "ON CONFLICT (id) DO UPDATE" in sql, "a re-harvest must update, not error or duplicate"


def t_estimate_flags_are_preserved() -> None:
    """A SIPRI estimate must never be readable as a reported figure."""
    conn = use_conn()
    store.upsert_transfers([OBSERVED])
    row = row_of(conn.log[0][1])
    assert row["units_est"] is True, "unitsEst=True was dropped — 100 units would read as fact"
    assert row["order_yr_est"] is False and row["status_est"] is False
    sql = conn.log[0][0]
    for col in ("order_yr_est", "units_est", "status_est"):
        assert f"{col} = EXCLUDED.{col}" in sql, f"{col} is not refreshed on re-harvest"


def t_missing_optional_keys_do_not_raise() -> None:
    conn = use_conn()
    written = store.upsert_transfers([{"id": 7}])
    assert written == 1
    row = row_of(conn.log[0][1])
    assert row["buyer"] is None and row["units"] is None
    assert row["units_est"] is None, \
        "a missing estimate flag must be NULL, not False — False asserts SIPRI reported it as fact"


def t_raw_preserves_the_original_object() -> None:
    """The point of `raw`: a field SIPRI adds later is still captured before anyone maps a column."""
    record = dict(OBSERVED, futureField="added next annual update")
    conn = use_conn()
    store.upsert_transfers([record])
    raw = conn.log[0][1][len(store._TRANSFER_COLS)]
    assert raw.obj == record, "raw is not the original object"
    assert raw.obj["futureField"] == "added next annual update"


def t_record_without_an_id_is_skipped_and_counted() -> None:
    """documents.document_id being NOT NULL is how rows vanished before — no id, no invented id."""
    conn = use_conn()
    with quiet():
        written = store.upsert_transfers([{"buyer": "no id here"}, dict(OBSERVED)])
    assert written == 1, "a record with no primary key must not be counted as written"
    assert store._ERRORS == 1, "the dropped record was not reported"
    assert len(conn.log) == 1


def t_duplicate_ids_collapse() -> None:
    """Pagination sends sorts={} (no ORDER BY), so a record can arrive twice; Postgres rejects a
    whole statement that touches one row twice via ON CONFLICT DO UPDATE."""
    conn = use_conn()
    written = store.upsert_transfers([OBSERVED, dict(OBSERVED, buyer="India")])
    assert written == 1, "a duplicate id in one INSERT would fail the entire batch"
    assert row_of(conn.log[0][1])["buyer"] == "India", "last occurrence should win"


def t_batches_stay_inside_the_parameter_cap() -> None:
    conn = use_conn()
    written = store.upsert_transfers([dict(OBSERVED, id=i) for i in range(2500)])
    assert written == 2500
    assert len(conn.log) == 3, f"expected 3 batched statements, got {len(conn.log)}"
    for _sql, params in conn.log:
        assert len(params) <= 65535, "Postgres allows 65535 bound parameters per statement"


def t_empty_input_is_not_an_error() -> None:
    use_conn()
    assert store.upsert_transfers([]) == 0
    assert store._ERRORS == 0, "an empty harvest is not a failure"


# ── registry + documents ─────────────────────────────────────────────────────
def t_dataset_registry_roundtrip() -> None:
    stored = ("milex", "SIPRI Military Expenditure Database", "https://x/f.xlsx",
              "https://www.sipri.org/databases/milex", "xlsx", "deadbeef", 4096,
              "milex/deadbeef.xlsx", 42, "2026-07-20")
    conn = use_conn(results=[stored])
    ok = store.record_dataset("milex", title="SIPRI Military Expenditure Database",
                              source_url="https://x/f.xlsx", kind="xlsx", sha256="deadbeef",
                              size_bytes=4096, minio_key="milex/deadbeef.xlsx", row_count=42)
    assert ok is True
    sql, params = conn.log[0]
    assert "ON CONFLICT (key) DO UPDATE" in sql, "re-harvest must update the registry row"
    assert "deadbeef" in params and 4096 in params, params

    got = store.get_dataset("milex")
    assert got["sha256"] == "deadbeef", "the sha256 skip-check reads this back"
    assert got["bytes"] == 4096 and got["row_count"] == 42 and got["harvested_at"] == "2026-07-20"


def t_unknown_dataset_is_none() -> None:
    use_conn(results=[None])
    assert store.get_dataset("never-harvested") is None


def t_record_document_upserts_on_url() -> None:
    conn = use_conn()
    ok = store.record_document("https://www.sipri.org/databases/embargoes", title="Embargoes",
                               html_len=51234, sha256="cafe", minio_key="embargoes/cafe.html")
    assert ok is True
    sql, params = conn.log[0]
    assert "ON CONFLICT (url) DO UPDATE" in sql
    assert 51234 in params and "cafe" in params


# ── failure is visible ───────────────────────────────────────────────────────
def t_database_outage_is_reported_to_the_caller() -> None:
    """The whole point: ingest once returned 200 while the insert failed and the rows went nowhere."""
    no_conn()
    with quiet():
        assert store.ensure_schema() is False
        assert store.upsert_transfers([OBSERVED]) == 0
        assert store.record_dataset("milex", sha256="a") is False
        assert store.record_document("https://www.sipri.org/databases/pko") is False
        assert store.get_dataset("milex") is None
        report = store.stats()
    assert store._ERRORS >= 5, f"lost writes were not counted: {store._ERRORS}"
    assert store._LAST_ERROR, "no error detail for the caller to log"
    assert report["errors"] >= 5, "stats() must surface the failures after a run"
    assert report["arms_transfers"] is None, "an unreadable count must not be reported as 0"


def t_a_broken_cursor_is_reported_not_swallowed() -> None:
    class Broken(FakeConn):
        def cursor(self):
            raise RuntimeError("server closed the connection unexpectedly")

    conn = Broken()
    store._get = lambda: conn
    store._ERRORS = 0
    with quiet():
        assert store.upsert_transfers([OBSERVED]) == 0, "reported a write that did not happen"
        assert store.record_dataset("milex", sha256="a") is False
    assert store._ERRORS == 2, "a mid-run DB failure was swallowed"
    assert "server closed" in (store._LAST_ERROR or "")


def t_partial_write_reports_what_landed() -> None:
    """A failure halfway through must not report the full count — autocommit already kept chunk 1."""
    class HalfBroken(FakeConn):
        def cursor(self):
            cur = FakeCursor(self.log, self.results)
            real = cur.execute

            def execute(sql, params=None):
                if len(self.log) >= 2:
                    raise RuntimeError("connection reset")
                real(sql, params)
            cur.execute = execute
            return cur

    conn = HalfBroken()
    store._get = lambda: conn
    store._ERRORS = 0
    with quiet():
        written = store.upsert_transfers([dict(OBSERVED, id=i) for i in range(2500)])
    assert written == 2000, f"reported {written} written, only 2000 statements succeeded"
    assert store._ERRORS == 1


def t_missing_driver_is_not_an_empty_harvest() -> None:
    saved = sys.modules.get("psycopg.types.json")
    sys.modules["psycopg.types.json"] = None      # import raises ImportError
    use_conn()
    try:
        with quiet():
            written = store.upsert_transfers([OBSERVED])
    finally:
        sys.modules["psycopg.types.json"] = saved
    assert written == 0 and store._ERRORS == 1, "a missing driver looked like a successful no-op"


def t_minio_outage_is_reported() -> None:
    saved_endpoint = os.environ.pop("MINIO_ENDPOINT", None)
    store._minio_client = None
    store._no_minio_warned = True                 # the once-only notice already fired
    store._ERRORS = 0
    try:
        with quiet():
            assert store.put_blob("milex/x.xlsx", b"payload", "application/vnd.ms-excel") is None
    finally:
        if saved_endpoint is not None:
            os.environ["MINIO_ENDPOINT"] = saved_endpoint
    assert store._ERRORS == 1, "bytes went nowhere and nobody was told"


def t_put_blob_uses_the_configured_bucket() -> None:
    class FakeMinio:
        made: list = []
        last = None

        def __init__(self, endpoint, access_key=None, secret_key=None, secure=False):
            self.endpoint, self.puts = endpoint, []
            FakeMinio.last = self

        def bucket_exists(self, bucket):
            return False                          # force the create path

        def make_bucket(self, bucket):
            FakeMinio.made.append(bucket)

        def put_object(self, bucket, key, stream, length=None, content_type=None):
            self.puts.append((bucket, key, stream.read(), content_type, length))

    saved_mod = sys.modules.get("minio")
    saved_endpoint = os.environ.get("MINIO_ENDPOINT")
    fake = types.ModuleType("minio")
    fake.Minio = FakeMinio
    sys.modules["minio"] = fake
    os.environ["MINIO_ENDPOINT"] = "minio:9000"
    store._minio_client = None
    store._ERRORS = 0
    try:
        key = store.put_blob("milex/deadbeef.xlsx", b"xlsx-bytes", "application/vnd.ms-excel")
    finally:
        store._minio_client = None
        if saved_mod is None:
            sys.modules.pop("minio", None)
        else:
            sys.modules["minio"] = saved_mod
        if saved_endpoint is None:
            os.environ.pop("MINIO_ENDPOINT", None)
        else:
            os.environ["MINIO_ENDPOINT"] = saved_endpoint

    assert key == "milex/deadbeef.xlsx", "the stored object key is what record_dataset persists"
    assert FakeMinio.made == [config.MINIO_BUCKET], \
        f"created {FakeMinio.made}, expected the configured SIPRI bucket"
    bucket, obj, data, ctype, length = FakeMinio.last.puts[0]
    assert bucket == config.MINIO_BUCKET and bucket != "mallory-raw"
    assert obj == "milex/deadbeef.xlsx" and data == b"xlsx-bytes"
    assert ctype == "application/vnd.ms-excel" and length == 10
    assert store._ERRORS == 0


def t_stats_counts_every_table() -> None:
    conn = use_conn(results=[(29916,), (4,), (3,)])
    report = store.stats()
    assert report == {"arms_transfers": 29916, "datasets": 4, "documents": 3,
                      "errors": 0, "last_error": None}, report
    assert [sql.split("FROM ")[1] for sql, _ in conn.log] == \
        [f"{config.PG_SCHEMA}.arms_transfers", f"{config.PG_SCHEMA}.datasets",
         f"{config.PG_SCHEMA}.documents"]


def main() -> None:
    for fn in (t_ddl_uses_the_configured_schema, t_writes_use_the_configured_schema,
               t_schema_name_is_validated, t_ddl_is_idempotent,
               t_storage_is_separate_from_the_crawler, t_tables_cover_the_contract,
               t_maps_every_observed_field, t_estimate_flags_are_preserved,
               t_missing_optional_keys_do_not_raise, t_raw_preserves_the_original_object,
               t_record_without_an_id_is_skipped_and_counted, t_duplicate_ids_collapse,
               t_batches_stay_inside_the_parameter_cap, t_empty_input_is_not_an_error,
               t_dataset_registry_roundtrip, t_unknown_dataset_is_none,
               t_record_document_upserts_on_url, t_database_outage_is_reported_to_the_caller,
               t_a_broken_cursor_is_reported_not_swallowed, t_partial_write_reports_what_landed,
               t_missing_driver_is_not_an_empty_harvest, t_minio_outage_is_reported,
               t_put_blob_uses_the_configured_bucket, t_stats_counts_every_table):
        fn()
        print(f"  ok  {fn.__name__}")
    print("ALL SIPRI STORE TESTS PASSED")



def test_sipri_store_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()

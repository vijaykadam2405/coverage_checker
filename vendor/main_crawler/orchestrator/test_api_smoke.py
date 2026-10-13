"""Smoke + invariant tests for the dashboard API.

The dashboard rests on ONE promise: a number is shown only if a service returned it, and "we did
not measure this" never renders as zero. That promise lives in the API, so it is asserted here —
previously nothing would have failed if `/api/logs/sites` had started reporting 0 for a host it had
no crawl report for, which is the exact bug this suite exists to catch.

Both downstream services are pointed at port 9 (discard) so they are deterministically UNREACHABLE.
That is not a limitation of the test, it IS the test: the unreachable path is the one that used to
render as a clean, empty, reassuring dashboard.

Standalone, matching test_scheduler.py / test_tiering.py:  python test_api_smoke.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp()
# Set BEFORE importing the app: db.py builds its engine at import, and crawler_client binds
# get_settings() to a module-level global, so a later change would not be seen.
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/smoke.db"
os.environ["CRAWLER_API"] = "http://127.0.0.1:9"      # discard port ⇒ instant connection refused
os.environ["INGEST_API_URL"] = "http://127.0.0.1:9"
os.environ["SCHEDULER_TICK"] = "3600"                 # the tick must not fire mid-test

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from fastapi.testclient import TestClient           # noqa: E402
from sqlalchemy import select                       # noqa: E402

from mallory_orchestrator.db import Base, SessionLocal, engine   # noqa: E402
from mallory_orchestrator.main import app           # noqa: E402
from mallory_orchestrator.models import (SiteMeasurement, Source,  # noqa: E402
                                         SourceRow)
from mallory_orchestrator import scheduler          # noqa: E402

# create_all explicitly rather than entering TestClient as a context manager: the lifespan also
# starts the scheduler background task, and a suite that races a live dispatcher is a flaky suite.
Base.metadata.create_all(engine)
client = TestClient(app)

# Every GET the dashboard actually calls. A 5xx here is a crash the operator would see as a blank
# panel with no explanation.
DASHBOARD_GETS = [
    "/health", "/api/host", "/api/tiers", "/api/control/state", "/api/control/categories",
    "/api/data/summary", "/api/data/types", "/api/data/categories", "/api/data/recent?limit=5",
    "/api/sources/catalog", "/api/schedule", "/api/schedule/policies",
    "/api/logs/sites?limit=10", "/api/logs/runs?limit=10", "/api/logs/unresolved",
    "/api/keywords",
]


def t_no_dashboard_route_5xxs_when_everything_downstream_is_dead() -> None:
    """A dead crawler and a dead ingest must degrade, never 500."""
    for path in DASHBOARD_GETS:
        r = client.get(path)
        assert r.status_code < 500, f"{path} -> {r.status_code}: {r.text[:200]}"


def t_unreachable_is_reported_not_zeroed() -> None:
    """The core promise. Each of these proxies a dead service; none may answer with a tidy zero."""
    for path, flag in (("/api/host", "reachable"), ("/api/data/summary", "reachable"),
                       ("/api/data/types", "reachable"), ("/api/data/categories", "reachable")):
        d = client.get(path).json()
        assert d.get(flag) is False, f"{path} claimed {flag}={d.get(flag)} with the service down"
        assert d.get("error"), f"{path} reported unreachable without saying why"
    # ...and the hardware numbers must be ABSENT, not 0 — a 0% CPU tile reads as a healthy idle box.
    host = client.get("/api/host").json()
    for k in ("cpu_percent", "ram", "disk"):
        assert host.get(k) is None, f"/api/host invented {k}={host.get(k)!r} with the crawler down"


def t_logs_report_unknown_as_null_never_zero() -> None:
    """A host with run history but NO crawler report: dedupe counters are known, per-URL outcomes
    are not. Reporting the unknown half as 0 is what made the old panel read 'crawled cleanly'."""
    now = scheduler.utcnow()
    with SessionLocal() as db:
        db.add(SiteMeasurement(domain="example-history.test", at=now, interval_days=1.0,
                               fetched=10, kept=3, skipped_unchanged=5, skipped_duplicate=1,
                               dropped_by_gate=2, errors=4, checked=9, duration_s=12.5))
        db.commit()
    rows = client.get("/api/logs/sites?limit=50").json()["rows"]
    row = next((r for r in rows if r["host"] == "example-history.test"), None)
    assert row is not None, "a site with measurement history is missing from the log"
    # known, because SiteMeasurement recorded them
    assert row["unchanged"] == 5 and row["duplicate"] == 1 and row["gate_dropped"] == 2
    assert row["errors"] == 4, "errors must be surfaced — it is one of the operator's tags"
    # unknown, because no crawler report covers this host
    for k in ("resolved", "retryable", "terminal", "unresolved"):
        assert row[k] is None, f"{k} was {row[k]!r}; unknown must stay null so the UI can say '?'"
    assert "history" in row["sources"] and "crawler" not in row["sources"]


def t_category_filter_never_widens_to_the_whole_store() -> None:
    """A category with no catalogued domains must return NOTHING. Falling through to an unfiltered
    query would answer a question the operator did not ask, with the entire store."""
    d = client.get("/api/data/recent?limit=5&category=definitely_not_a_category").json()
    assert d["rows"] == [] and d.get("note"), "empty category did not explain its empty result"
    # A hostile value is a category name like any other — it matches nothing and widens nothing.
    d = client.get("/api/data/recent?limit=5&category=' OR 1=1 --").json()
    assert d["rows"] == []


def t_spreadsheet_max_pages_of_zero_survives() -> None:
    """0 is falsy. `params.get(...) or default` silently turned "crawl nothing" into 100,000 pages."""
    now = scheduler.utcnow()
    with SessionLocal() as db:
        db.add(Source(domain="zero-budget.test", source_id="ZEROBUDGET", category="tender_portal",
                      tier=1, frequency="daily", source_known=True, tier_origin="human",
                      added_by="human", created_at=now))
        db.add(SourceRow(domain="zero-budget.test", raw={}, params={"max_pages": 0, "max_depth": 2},
                         imported_at=now, source_file="t.csv"))
        db.commit()
    jobs = client.post("/api/jobs/generate-by-category", json={"category": "tender"}).json()["jobs"]
    job = next(j["job"] for j in jobs if j["domain"] == "zero-budget.test")
    assert job["max_pages"] == 0, f"a spreadsheet 0 became {job['max_pages']}"
    assert job["max_depth"] == 2, "an explicit depth was overwritten by the -1 default"


def t_keyword_gate_follows_the_operator_rule() -> None:
    """Pure defence ⇒ gate OFF (capture everything); mixed publisher ⇒ gate ON. Every job must also
    carry the REASON, because an automatic decision nobody can inspect is the one that ruins a crawl."""
    now = scheduler.utcnow()
    with SessionLocal() as db:
        db.add(Source(domain="mixed-paper.test", source_id="MIXEDPAPER", category="aggregator",
                      tier=3, frequency="daily", source_known=True, tier_origin="human",
                      added_by="human", created_at=now))
        db.commit()
    tender = client.post("/api/jobs/generate-by-category", json={"category": "tender"}).json()
    news = client.post("/api/jobs/generate-by-category", json={"category": "news"}).json()
    t = next(j for j in tender["jobs"] if j["domain"] == "zero-budget.test")
    n = next(j for j in news["jobs"] if j["domain"] == "mixed-paper.test")
    assert t["job"]["keyword"] is False, "a tender portal must not be keyword-gated"
    assert n["job"]["keyword"] is True, "a general aggregator must be keyword-gated"
    assert t["_why"]["keyword"] and n["_why"]["keyword"], "the gate decision is unexplained"
    # tender listings must never have their RFP table truncated
    assert t["job"]["max_pdfs_per_page"] == 0


def t_capture_override_applies_and_bad_kinds_are_refused() -> None:
    """Manufacture maps to job_type `profile`, whose default capture is html+text only — no PDF. An
    operator crawling manufacturers wants brochures and spec sheets, so the override has to reach
    the job, and a typo in a capture kind must fail loudly rather than produce a job that silently
    collects nothing."""
    opts = client.get("/api/jobs/capture-options").json()
    assert "pdf" in opts["kinds"]
    assert opts["defaults"]["manufacture"] == ["html", "text"], \
        f"profile default changed: {opts['defaults']['manufacture']}"
    assert opts["defaults"]["all"] is None, "'all' must not claim a single default"

    body = {"category": "tender", "capture": ["html", "text", "pdf"]}
    jobs = client.post("/api/jobs/generate-by-category", json=body).json()["jobs"]
    assert jobs, "no jobs generated for the override check"
    for g in jobs:
        assert g["job"]["capture"] == ["html", "text", "pdf"], g["job"]["capture"]
        assert g["_why"]["capture"] == "operator override"

    # Omitted / empty ⇒ the category default, not an empty capture list.
    plain = client.post("/api/jobs/generate-by-category", json={"category": "tender"}).json()["jobs"]
    assert plain[0]["job"]["capture"] == ["html", "text", "pdf", "screenshot"]
    assert "default" in plain[0]["_why"]["capture"]

    r = client.post("/api/jobs/generate-by-category",
                    json={"category": "tender", "capture": ["html", "pdfs"]})
    assert r.status_code == 422, f"a misspelled capture kind was accepted ({r.status_code})"


def t_unknown_category_is_refused() -> None:
    r = client.post("/api/jobs/generate-by-category", json={"category": "nonsense"})
    assert r.status_code == 422, f"an unknown category was accepted ({r.status_code})"


def t_import_rejects_an_empty_upload_and_parses_a_real_csv() -> None:
    assert client.post("/api/sources/import?filename=x.csv", content=b"").status_code == 400
    csv = (b"Website;Type;Estimated Max Pages\n"
           b"https://navalnews.test/;News;~5,000 pages\n"
           b"no-url-here;News;10\n")
    d = client.post("/api/sources/import?filename=s.csv", content=csv).json()
    assert d["format"] == "csv"
    assert d["columns"] == ["Website", "Type", "Estimated Max Pages"], "operator columns not preserved"
    assert d["count"] == 1 and len(d["errors"]) == 1, "the unusable row was not reported separately"
    row = d["rows"][0]
    # 5050, not 5000: the preview shows what the CATALOG will hold, and the catalog budget rule
    # (importer.catalog_max_pages: under 100 -> 100, else +50) is applied at this boundary so it
    # lands exactly once — /commit stores the reviewed rows verbatim. Asserting the raw parse here
    # would mean the preview number and the stored number disagree, which is the bug the rule's
    # placement exists to avoid. normalise() itself stays pure and is pinned in importer's own
    # self-check ("~5,000 pages" -> 5000).
    assert row["max_pages"] == 5050, f'"~5,000 pages" + catalog headroom -> {row["max_pages"]}'
    assert row["category"] == "trade_press"
    assert row["raw"]["Website"] == "https://navalnews.test/", "the original row was not kept"


def t_derive_frequency_dry_run_saves_nothing() -> None:
    """`apply=false` is the operator's "show me first". It must not mutate a single row."""
    with SessionLocal() as db:
        before = {p.domain: p.tier for p in db.scalars(select(__import__(
            "mallory_orchestrator.models", fromlist=["SiteSchedule"]).SiteSchedule)).all()}
    d = client.post("/api/schedule/derive-frequency?apply=false").json()
    assert d["applied"] is False and d["jobs_updated"] == 0
    for r in d["rows"]:
        assert r["basis"], "a cadence was reported with no stated basis"
    with SessionLocal() as db:
        after = {p.domain: p.tier for p in db.scalars(select(__import__(
            "mallory_orchestrator.models", fromlist=["SiteSchedule"]).SiteSchedule)).all()}
    assert before == after, "the dry run changed stored cadences"


def main() -> None:
    for fn in (t_no_dashboard_route_5xxs_when_everything_downstream_is_dead,
               t_unreachable_is_reported_not_zeroed,
               t_logs_report_unknown_as_null_never_zero,
               t_category_filter_never_widens_to_the_whole_store,
               t_spreadsheet_max_pages_of_zero_survives,
               t_keyword_gate_follows_the_operator_rule,
               t_capture_override_applies_and_bad_kinds_are_refused,
               t_unknown_category_is_refused,
               t_import_rejects_an_empty_upload_and_parses_a_real_csv,
               t_derive_frequency_dry_run_saves_nothing):
        fn()
        print(f"  ok  {fn.__name__}")
    print("ALL DASHBOARD API TESTS PASSED")


if __name__ == "__main__":
    main()

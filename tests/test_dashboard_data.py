from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from crawler_coverage_auditor.services.inventory import (
    list_runs,
    load_report_rows,
    load_run_sections,
)
from crawler_coverage_auditor.storage.database import SCHEMA


def make_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO audit_runs(id,target_url,corpus_url,started_at,completed_at,config_json) VALUES(1,?,?,?,?,?)",
            ("https://source.example/", "https://corpus.example/", "2026-10-05T10:00:00+00:00",
             "2026-10-05T10:02:00+00:00", '{"output_dir":"reports"}'),
        )
        connection.execute(
            "INSERT INTO urls(audit_run_id,url,normalized_url,page_type,min_depth,discovery_methods,state) VALUES(1,?,?,?,?,?,?)",
            ("https://source.example/page", "https://source.example/page", "HTML", 1, '["SITEMAP"]', "DONE"),
        )
        connection.execute(
            "INSERT INTO http_results(audit_run_id,normalized_url,status_code,validity,fetched_at) VALUES(1,?,?,?,?)",
            ("https://source.example/page", 200, "VALID_PAGE", "2026-10-05T10:01:00+00:00"),
        )
        connection.execute(
            "INSERT INTO corpus_matches(audit_run_id,normalized_url,match_status,checked_at) VALUES(1,?,?,?)",
            ("https://source.example/page", "FOUND_EXACT", "2026-10-05T10:02:00+00:00"),
        )


def test_dashboard_reads_run_summary_and_url_evidence_read_only(tmp_path: Path) -> None:
    database_path = tmp_path / "coverage.sqlite"
    make_database(database_path)

    runs = list_runs(database_path)
    rows = load_report_rows(database_path, 1)
    sections = load_run_sections(database_path, 1)

    assert runs[0]["discovered_urls"] == 1
    assert runs[0]["valid_urls"] == 1
    assert runs[0]["found_urls"] == 1
    assert runs[0]["config"]["output_dir"] == "reports"
    assert rows[0]["validity"] == "VALID_PAGE"
    assert rows[0]["match_status"] == "FOUND_EXACT"
    assert sections["errors"] == []

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM audit_runs").fetchone()[0] == 1


def test_dashboard_does_not_create_a_missing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "missing.sqlite"

    with pytest.raises(FileNotFoundError):
        list_runs(database_path)

    assert not database_path.exists()

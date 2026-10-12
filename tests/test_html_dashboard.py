from pathlib import Path

from crawler_coverage_auditor.reporting.html import write_html


def test_html_dashboard_is_self_contained_and_interactive(tmp_path: Path) -> None:
    report = tmp_path / "coverage_report.html"
    write_html(report, {"audit_run_id": 7, "target": "https://example.test/", "expected_urls": 2}, [
        {"url": "https://example.test/missing", "validity": "VALID_PAGE", "match_status": "NOT_FOUND", "page_type": "HTML", "min_depth": 3}
    ], [{"severity": "HIGH", "kind": "PAGINATION", "evidence": "Missing page 101"}],
    depth_rows=[{"depth": 3, "expected": 2, "found": 1, "missing": 1}])
    content = report.read_text(encoding="utf-8")
    assert "CrawlScope" in content
    assert "URL inventory" in content
    assert "Download missing URLs CSV" in content
    assert "https://example.test/missing" in content
    assert "https://cdn" not in content

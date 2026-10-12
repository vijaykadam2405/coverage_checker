from crawler_coverage_auditor.analysis.regression import compare_runs


def test_regression_identifies_recovered_and_newly_missing_urls() -> None:
    old = [{"normalized_url": "https://x/a", "validity": "VALID_PAGE", "match_status": "NOT_FOUND"},
           {"normalized_url": "https://x/b", "validity": "VALID_PAGE", "match_status": "EXACT_MATCH"}]
    new = [{"normalized_url": "https://x/a", "validity": "VALID_PAGE", "match_status": "EXACT_MATCH"},
           {"normalized_url": "https://x/b", "validity": "VALID_PAGE", "match_status": "NOT_FOUND"}]
    summary, changes = compare_runs(old, new)
    assert summary == {"recovered_urls": 1, "newly_missing_urls": 1, "net_recovered_urls": 0}
    assert {item["change"] for item in changes} == {"RECOVERED", "NEWLY_MISSING"}

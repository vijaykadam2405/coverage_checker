from crawler_coverage_auditor.analysis.inventory_segments import archive_coverage, js_only_rows, sitemap_coverage


def test_segments_preserve_discovery_provenance() -> None:
    rows = [
        {"normalized_url": "https://x.test/news?year=2021", "validity": "VALID_PAGE", "match_status": "NOT_FOUND", "discovery_methods": '["SITEMAP"]'},
        {"normalized_url": "https://x.test/js-route", "validity": "VALID_PAGE", "match_status": "NOT_FOUND", "discovery_methods": '["JS_DISCOVERY"]'},
        {"normalized_url": "https://x.test/about", "validity": "VALID_PAGE", "match_status": "EXACT_MATCH", "discovery_methods": '["STATIC", "SITEMAP"]'},
    ]
    assert archive_coverage(rows)[0]["year"] == "2021"
    assert sitemap_coverage(rows)[0]["expected"] == 2
    assert [row["normalized_url"] for row in js_only_rows(rows)] == ["https://x.test/js-route"]

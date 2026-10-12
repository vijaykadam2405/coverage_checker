from crawler_coverage_auditor.validation.soft404 import assess_page_validity


def test_http_200_no_results_is_soft_404() -> None:
    assert assess_page_validity(200, "No results found").status == "SOFT_404"


def test_valid_page_is_not_limited_by_page_number() -> None:
    assert assess_page_validity(200, "A genuine archive page with results", item_count=5).status == "VALID_PAGE"

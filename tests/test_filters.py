from crawler_coverage_auditor.discovery.filters import detect_filters, get_filter_urls


def test_learns_get_filter_values_from_website_controls() -> None:
    html = """<form action='/news' method='get'><select name='year'>
    <option value=''>All years</option><option value='2024'>2024</option><option value='2023'>2023</option>
    </select></form>"""
    controls = detect_filters(html, "https://example.test/current")
    assert len(controls) == 1
    assert controls[0].name == "year"
    assert [value.request_value for value in controls[0].values] == ["2024", "2023"]
    assert get_filter_urls(controls[0], "https://example.test/current") == [
        "https://example.test/news?year=2024", "https://example.test/news?year=2023"
    ]

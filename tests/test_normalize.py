from crawler_coverage_auditor.validation.normalize import classify_parameter, normalize_url


def test_normalizes_fragment_case_and_tracking_parameter() -> None:
    assert normalize_url("HTTPS://EXAMPLE.COM/news//?utm_source=x&page=2#latest") == "https://example.com/news/?page=2"


def test_classifies_semantic_parameters() -> None:
    assert classify_parameter("page") == "PAGINATION"
    assert classify_parameter("year") == "FILTER"


def test_trims_href_whitespace_without_removing_encoded_spaces_or_queries():
    assert normalize_url(' https://example.test/about \n') == 'https://example.test/about'
    assert normalize_url(' /article?page=2&year=2021 ', 'https://example.test/') == 'https://example.test/article?page=2&year=2021'
    assert normalize_url('https://example.test/about%20') == 'https://example.test/about%20'


def test_html_links_trim_whitespace_before_resolution_and_dedup(tmp_path):
    from crawler_coverage_auditor.discovery.static_links import extract_links
    from crawler_coverage_auditor.auditor import CoverageAuditor
    from crawler_coverage_auditor.config import AuditConfig
    base = 'https://example.test/'
    links = extract_links('<a href=" /about ">About</a><a href="/about">Same page</a><a href=" javascript:alert(1) ">Ignore</a>', base)
    assert [link.url for link in links] == [base + 'about', base + 'about']
    auditor = CoverageAuditor(AuditConfig(target_url=base, database_path=tmp_path / 'audit.sqlite'))
    try:
        assert auditor._add_discovery(base + 'about ', 'STATIC', 1, base, 'About')
        assert not auditor._add_discovery(base + 'about', 'STATIC', 1, base, 'About')
        row = auditor.db.connection.execute('SELECT url,normalized_url FROM urls').fetchone()
        assert tuple(row) == (base + 'about', base + 'about')
    finally:
        auditor.db.close()

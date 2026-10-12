from crawler_coverage_auditor.analysis.inventory import inventory_breakdown, html_content_duplicates
from crawler_coverage_auditor.analysis.diagnostics import diagnostic_summary, split_boundaries


def test_resource_groups_are_disjoint_and_html_validity_is_explicit():
    rows = [
        {'url': 'https://site.test/page', 'page_type': 'HTML', 'validity': 'VALID_PAGE', 'match_status': 'EXACT_MATCH', 'fingerprint': 'same'},
        {'url': 'https://site.test/alias', 'page_type': 'HTML', 'validity': 'VALID_PAGE', 'fingerprint': 'same'},
        {'url': 'https://site.test/broken', 'page_type': 'HTML', 'validity': 'HTTP_ERROR'},
        {'url': 'https://site.test/pending', 'page_type': 'HTML'},
        {'url': 'https://site.test/report.PDF?download=1', 'page_type': 'DOCUMENT', 'validity': 'URL_ONLY'},
        {'url': 'https://site.test/download/123', 'page_type': 'DOCUMENT', 'content_type': 'application/pdf', 'validity': 'URL_ONLY'},
        {'url': 'https://site.test/file.docx', 'page_type': 'DOCUMENT', 'validity': 'URL_ONLY', 'match_status': 'NOT_FOUND'},
        {'url': 'https://site.test/photo.jpg', 'page_type': 'IMAGE', 'validity': 'URL_ONLY'},
        {'url': 'https://site.test/api', 'page_type': 'API', 'validity': 'VALID_PAGE'},
        {'url': 'https://site.test/video.mp4', 'page_type': 'RESOURCE', 'validity': 'URL_ONLY'},
    ]
    counts = {item['Type']: item for item in inventory_breakdown(rows)}
    assert sum(item['Collected URLs'] for item in counts.values()) == len(rows)
    assert counts['HTML pages']['Collected URLs'] == 4
    assert counts['HTML pages']['Ready for corpus'] == 2
    assert counts['HTML pages']['Failed / excluded'] == 1
    assert counts['HTML pages']['Website check pending'] == 1
    assert counts['PDFs']['Collected URLs'] == 2
    assert counts['Other documents']['Missing from corpus'] == 1
    assert counts['Images']['Corpus unverified'] == 1
    assert html_content_duplicates(rows)[0]['URL count'] == 2


def test_historical_diagnostics_are_not_erased_or_mislabeled_as_missing():
    url = 'https://site.test/page'
    rows = [{'url': url, 'page_type': 'HTML', 'validity': 'VALID_PAGE'}]
    items = [
        {'stage': 'Browser', 'url': url, 'reason': "JavaScript error: Cannot read properties of undefined (reading 'top')"},
        {'stage': 'Document text', 'url': 'https://site.test/a.pdf', 'reason': 'PDF has no extractable text'},
        {'stage': 'Corpus', 'url': url, 'reason': 'Corpus could not complete search: timed out'},
        {'stage': 'Website', 'url': 'https://site.test/broken', 'reason': 'HTTP_ERROR: HTTP 404'},
    ]
    before = [item.copy() for item in items]
    boundaries, notes = split_boundaries(items, rows)
    assert len(boundaries) == len(notes) == 2
    assert items == before
    summary = diagnostic_summary(items, rows)
    assert {item['Impact'] for item in summary} == {'Diagnostic', 'Historical', 'Verification', 'Website'}
    assert sum(item['Records'] for item in summary) == 4


def test_whitespace_404_is_explained_as_historical_url_handling_not_broken_page():
    summary = diagnostic_summary([{'stage': 'Website', 'url': 'https://site.test/about ', 'message': 'HTTP_ERROR: HTTP 404'}], [])
    assert summary[0]['Issue'] == 'Historical untrimmed URL / 404'
    assert summary[0]['Impact'] == 'Historical'

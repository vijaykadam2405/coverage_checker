"""Archive counts require evidence from the article itself, not incidental dates."""
import json

import pytest

from crawler_coverage_auditor.analysis.dashboard import build_analysis
from crawler_coverage_auditor.analysis.inventory_segments import row_archive_year
from crawler_coverage_auditor.services.inventory import load_report_rows
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.validation.publication import publication_evidence, snapshot_publication

URL = 'https://example.test/blog/remote-soc-a-challenge-for-security-professionals'
TEXT = 'Home 25/02/2022 09:26 AM Remote SOC: A Challenge for Security Professionals Article text. Copyright 2026.'


def test_saved_article_date_is_correlated_with_long_slug():
    evidence = snapshot_publication(TEXT, URL)
    assert evidence['date'] == '2022-02-25' and evidence['year'] == '2022'
    assert snapshot_publication(TEXT, 'https://example.test/blog/index.php') is None
    assert snapshot_publication(TEXT.replace('Remote SOC', 'Unrelated story'), URL) is None
    assert snapshot_publication(TEXT.replace('25/02/2022', '31/02/2022'), URL) is None
    assert snapshot_publication('Copyright 2022. ' + TEXT.split(' AM ')[1], URL) is None
    assert snapshot_publication(TEXT + ' 25/02/2021 09:26 AM Remote SOC: A Challenge for Security Professionals', URL) is None


@pytest.mark.parametrize('html', [
    '<meta property="article:published_time" content="2021-04-03T12:00:00Z">',
    '<time itemprop="datePublished" datetime="2021-04-03">3 April 2021</time>',
    '<script type="application/ld+json">' + json.dumps({'@graph': [{'@type': 'NewsArticle', 'url': URL, 'datePublished': '2021-04-03'}]}) + '</script>',
])
def test_explicit_live_article_metadata(html):
    assert publication_evidence(html, URL)['date'] == '2021-04-03'


def test_unrelated_json_article_and_modified_dates_do_not_date_listing():
    data = {'@type': 'NewsArticle', 'url': URL, 'datePublished': '2021-04-03'}
    html = '<script type="application/ld+json">' + json.dumps(data) + '</script>'
    assert publication_evidence(html, 'https://example.test/blog') is None
    assert publication_evidence(html, URL.replace('example.test', 'other.test')) is None
    assert publication_evidence('<meta property="article:modified_time" content="2021-04-03"><footer>Copyright 2021</footer>', URL) is None
    assert publication_evidence('<meta property="article:published_time" content="2021-04-03"><time itemprop="datePublished" datetime="2020-03-02">Date</time>', URL) is None
    assert publication_evidence('<h1>Remote SOC: A Challenge for Security Professionals</h1><article>' + TEXT + '</article>', URL)['year'] == '2022'


def test_existing_snapshot_enrichment_is_readonly_and_shared_with_completed_reports(tmp_path):
    path = tmp_path / 'audit.sqlite'
    db = AuditDatabase(path)
    run = db.create_run('https://example.test/', 'https://api.test/', {})
    db.add_url(run, DiscoveredURL(URL, URL, 'STATIC', 1))
    db.add_http_result(run, URL, status_code=200, validity='VALID_PAGE')
    db.add_page_snapshot(run, URL, TEXT)
    db.add_corpus_match(run, URL, 'EXACT_MATCH', URL, None, None, None)
    before = path.read_bytes()
    rows = load_report_rows(path, run)
    assert path.read_bytes() == before
    assert 'cleaned_text' not in rows[0]
    assert db.report_rows(run, only_url=URL)[0]['content_evidence_json'] == rows[0]['content_evidence_json']
    archive = next(item for item in build_analysis(rows, {})['segments'] if item['Area'] == 'Archives')
    assert archive == {'Area': 'Archives', 'Valid URLs': 1, 'Found': 1, 'Missing': 0, 'Unverified': 0, 'Coverage %': 100}
    assert build_analysis(rows, {})['years'][0]['Year'] == '2022'
    assert db.connection.execute('SELECT COUNT(*) FROM content_evidence').fetchone()[0] == 0
    assert row_archive_year({**rows[0], 'validity': 'HTTP_ERROR'}) is None
    assert row_archive_year({**rows[0], 'content_evidence_json': None, 'evidence_json': json.dumps({'published_at': '2021-04-03'})}) is None
    db.content_evidence(run, URL, {'other_evidence': True})
    db.content_evidence(run, URL, {'website_publication': snapshot_publication(TEXT, URL)})
    assert json.loads(db.report_rows(run)[0]['content_evidence_json'])['other_evidence']
    db.close()

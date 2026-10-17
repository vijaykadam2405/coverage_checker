"""Capped saved inventories can discover further URLs without losing their results."""
import asyncio
import json

import pytest

from crawler_coverage_auditor.analysis.diagnostics import issue_info
from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.services.jobs import AuditJobs
from crawler_coverage_auditor.services.recovery import (
    HISTORICAL_COUNT_STAGE,
    finish_count_recovery,
    is_count_cutoff,
    needs_count_recovery,
    reopen_count_limited_discovery,
)
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.validation.fingerprints import sha256_text
from crawler_coverage_auditor.validation.http import FetchResult
from crawler_coverage_auditor.validation.text_coverage import TEXT_API_VERSION


def saved_inventory(tmp_path):
    db = AuditDatabase(tmp_path/'audit.sqlite')
    run_id = db.create_run('https://example.test/', 'https://api.test/', {'max_urls': 20000})
    for path, kind in (('', 'HTML'), ('article', 'HTML'), ('api', 'API'), ('report.pdf', 'DOCUMENT'), ('later', 'HTML'), ('gone', 'HTML')):
        url = 'https://example.test/'+path
        db.add_url(run_id, DiscoveredURL(url, url, 'STATIC', 0 if not path else 1, page_type=kind))
        if path != 'later':
            db.add_http_result(run_id, url, validity='HTTP_ERROR' if path == 'gone' else 'URL_ONLY' if kind == 'DOCUMENT' else 'VALID_PAGE')
            db.set_url_state(run_id, url, 'DONE')
    db.add_page_snapshot(run_id, 'https://example.test/article', 'Saved meaningful article content')
    db.add_corpus_match(run_id, 'https://example.test/article', 'EXACT_MATCH', 'https://example.test/article', 'id', 32, 1, 'FOUND_FULL', provider='coverage_api:https://api.test')
    db.limit(run_id, 'Inventory', 'https://example.test/', 'Inventory safeguard (20000 URLs) reached; additional content may exist')
    db.limit(run_id, 'Browser', 'https://example.test/', 'Rendered page safeguard (300) reached; candidates may remain')
    db.limit(run_id, 'Website', 'https://example.test/blocked', 'HTTP 429; access remains restricted')
    db.inventory_ready(run_id)
    db.finish_run(run_id, 'LIMITED')
    return db, run_id


def test_recovery_is_once_durable_and_preserves_inventory_matches_and_files(tmp_path):
    db, run_id = saved_inventory(tmp_path)
    try:
        assert needs_count_recovery(tmp_path/'audit.sqlite', run_id)
        assert reopen_count_limited_discovery(db, run_id) == 3
        assert db.connection.execute('SELECT COUNT(*) FROM urls').fetchone()[0] == 6
        assert db.connection.execute('SELECT COUNT(*) FROM corpus_matches').fetchone()[0] == 1
        assert db.page_snapshot(run_id, 'https://example.test/article') == 'Saved meaningful article content'
        assert db.connection.execute('SELECT state FROM urls WHERE url LIKE ?', ('%report.pdf',)).fetchone()[0] == 'DONE'
        assert db.connection.execute('SELECT state FROM urls WHERE url LIKE ?', ('%gone',)).fetchone()[0] == 'DONE'
        assert db.connection.execute('SELECT state FROM urls WHERE url LIKE ?', ('%later',)).fetchone()[0] == 'PENDING'
        assert db.connection.execute('SELECT discovery_completed_at FROM audit_runs WHERE id=?', (run_id,)).fetchone()[0] is None
        assert finish_count_recovery(db, run_id) == []
        db.set_url_state(run_id, 'https://example.test/article', 'DONE')
        assert reopen_count_limited_discovery(db, run_id) is None
        assert not needs_count_recovery(tmp_path/'audit.sqlite', run_id)
        assert db.connection.execute('SELECT state FROM urls WHERE url LIKE ?', ('%article',)).fetchone()[0] == 'DONE'
        db.connection.execute("UPDATE urls SET state='DONE'")
        db.connection.commit()
        resolved = finish_count_recovery(db, run_id)
        assert len(resolved) == 2
        assert db.connection.execute('SELECT COUNT(*) FROM audit_limits').fetchone()[0] == 3
        assert db.connection.execute('SELECT COUNT(*) FROM audit_limits WHERE stage=?', (HISTORICAL_COUNT_STAGE,)).fetchone()[0] == 2
        assert db.connection.execute("SELECT reason FROM audit_limits WHERE stage='Website'").fetchone()[0].startswith('HTTP 429')
        assert issue_info(HISTORICAL_COUNT_STAGE, resolved[0]['reason'])[1] == 'Historical'
        assert finish_count_recovery(db, run_id) == []
    finally:
        db.close()


def test_resume_routes_completed_capped_inventory_back_to_discovery(tmp_path, monkeypatch):
    db, run_id = saved_inventory(tmp_path)
    db.close()
    jobs = AuditJobs()
    calls = []
    monkeypatch.setattr(jobs, '_launch', lambda config, reports, action, *args: calls.append((config, action)))
    jobs.resume(run_id, tmp_path/'audit.sqlite', tmp_path/'reports')
    config, action = calls[0]
    assert action == 'discovery' and config.resume
    assert config.max_urls is config.max_browser_pages is None


def test_actual_resumed_discovery_finds_more_links_and_never_downloads_pdf(tmp_path, monkeypatch):
    config = AuditConfig(target_url='https://example.test/', corpus_url='https://api.test/',
        database_path=tmp_path/'audit.sqlite', output_dir=tmp_path/'reports', respect_robots=False,
        discovery_sources={'STATIC'}, discovery_only=True, resume=True)
    original = CoverageAuditor(config)
    run_id = original.run_id
    original._add_discovery(config.target_url, 'SEED', 0, None, '')
    original.db.add_http_result(run_id, config.target_url, validity='VALID_PAGE')
    original.db.set_url_state(run_id, config.target_url, 'DONE')
    original.db.limit(run_id, 'Inventory', config.target_url, 'Inventory safeguard (20000 URLs) reached; more content may exist')
    original.db.inventory_ready(run_id)
    original.db.finish_run(run_id, 'LIMITED')
    original.db.close()
    fetched = []
    class Website:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def head(self, url, **kwargs):
            return FetchResult(url, url, 200, {'content-type':'application/pdf'}, b'', url_only=True)
        async def get(self, url, **kwargs):
            fetched.append(url)
            html = '<main><h1>Meaningful published content</h1>'
            if url == config.target_url:
                html += '<a href="/new-article">New article</a><a href="/new-report.pdf">New PDF</a>'
            return FetchResult(url, url, 200, {'content-type': 'text/html'}, (html+'</main>').encode())
    monkeypatch.setattr('crawler_coverage_auditor.auditor.PoliteHttpClient', Website)
    resumed = CoverageAuditor(config, run_id=run_id)
    result = asyncio.run(resumed.run())
    assert result['status'] == 'AWAITING_CORPUS'
    db = AuditDatabase(config.database_path)
    try:
        assert {row['url'] for row in db.report_rows(run_id)} == {config.target_url,
            config.target_url+'new-article', config.target_url+'new-report.pdf'}
        assert fetched == [config.target_url, config.target_url+'new-article']
        assert db.connection.execute('SELECT COUNT(*) FROM audit_limits WHERE stage=?', (HISTORICAL_COUNT_STAGE,)).fetchone()[0] == 1
    finally:
        db.close()


@pytest.mark.parametrize('message', ['TOO_LARGE: body exceeds safety limit', 'HTTP 429 — rate limit',
    'Corpus API unavailable', 'Multiple filters: exhaustive combinations not proven'])
def test_non_count_boundaries_are_not_suppressed(message):
    assert not is_count_cutoff('Website', message)


@pytest.mark.parametrize('changed', [False, True])
def test_only_changed_html_text_is_rechecked_without_presence_queries(tmp_path, changed):
    config = AuditConfig(target_url='https://example.test/', corpus_url='https://api.test/', database_path=tmp_path/'audit.sqlite')
    auditor = CoverageAuditor(config)
    url = config.target_url
    original = 'Original meaningful article'
    live = 'Updated meaningful article with new details' if changed else original
    calls = []
    class API:
        async def held(self, urls):
            raise AssertionError('Successful URL presence must be reused')
        async def text(self, urls):
            calls.append(urls)
            return {url: {'lookup_status': 'found', 'content_type': 'text/html', 'version_id': 'v2',
                'html_text': {'status': 'available', 'truncated': False, 'text': live}}}
    try:
        auditor._add_discovery(url, 'SEED', 0, None, '')
        auditor.db.add_http_result(auditor.run_id, url, validity='VALID_PAGE')
        auditor.db.add_page_snapshot(auditor.run_id, url, live)
        auditor.db.add_corpus_match(auditor.run_id, url, 'EXACT_MATCH', url, 'id', len(original), 1, 'FOUND_FULL',
            provider='coverage_api:https://api.test', evidence={'api_contract_version': TEXT_API_VERSION,
            'text_comparison': {'live_hash': sha256_text(original)}})
        assert asyncio.run(auditor._verify_all(api=API(), only_url=url)) == 'COMPLETED'
        assert calls == ([[url]] if changed else [])
        assert auditor.db.connection.execute('SELECT COUNT(*) FROM corpus_match_history').fetchone()[0] == int(changed)
        row = dict(auditor.db.report_rows(auditor.run_id)[0])
        assert row['text_status'] == 'FOUND_FULL' and row['similarity'] == 1
        if changed:
            assert json.loads(row['evidence_json'])['text_comparison']['live_hash'] == sha256_text(live)
    finally:
        auditor.db.close()

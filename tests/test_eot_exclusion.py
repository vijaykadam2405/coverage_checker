import asyncio
import sqlite3

import httpx
import pytest

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.validation.http import PoliteHttpClient
from crawler_coverage_auditor.validation.resource_policy import excluded_resource_url
from scripts.remove_eot_data import remove_eot


@pytest.mark.parametrize('url', [
    'https://example.com/font.eot', 'https://example.com/FONT.EOT?v=2#iefix',
    'https://example.com/font%2EeOt', '/fonts/font.eot?',
])
def test_eot_paths_excluded(url):
    assert excluded_resource_url(url)


@pytest.mark.parametrize('url', [None, '', 'https://example.com/font.woff2',
    'https://example.com/report.pdf', 'https://example.com/photo.png',
    'https://example.com/search?file=font.eot', 'https://example.com/font.eot.html'])
def test_other_urls_unchanged(url):
    assert not excluded_resource_url(url)


def test_discovery_and_storage_reject_eot_without_events(tmp_path):
    auditor = CoverageAuditor(AuditConfig(target_url='https://example.com/',
        database_path=tmp_path/'audit.sqlite', output_dir=tmp_path/'reports'))
    try:
        for method in ['STATIC', 'SITEMAP', 'JS_DISCOVERY', 'API_DISCOVERY', 'EXTERNAL_INDEX']:
            assert not auditor._add_discovery('https://example.com/font.eot?v=2', method, 1, None, '')
        assert not auditor.db.add_url(auditor.run_id, DiscoveredURL(
            'https://example.com/font.eot', 'https://example.com/font.eot', 'STATIC', 1))
        assert auditor.db.connection.execute('SELECT COUNT(*) FROM urls').fetchone()[0] == 0
        assert auditor.db.connection.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == 0
    finally:
        auditor.db.close()


def test_http_never_requests_eot_and_stops_redirect(tmp_path):
    calls = []
    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={'location': '/font.eot'})
    async def check():
        async with PoliteHttpClient(1, 2, 0, 'test') as client:
            client._client._transport = httpx.MockTransport(respond)
            assert (await client.get('https://example.com/font.eot')).failure_reason == 'RESOURCE_EXCLUDED'
            assert not calls
            assert (await client.get('https://example.com/download')).error
    asyncio.run(check())
    assert calls == ['https://example.com/download']


def test_purge_scoped_and_recoverable(tmp_path):
    path = tmp_path/'audit.sqlite'
    db = AuditDatabase(path)
    run_id = db.create_run('https://example.com/', '', {})
    font, html = 'https://example.com/font.EOT?v=2', 'https://example.com/article'
    db.connection.execute("UPDATE audit_runs SET status='LIMITED' WHERE id=?", (run_id,))
    # Simulate a legacy font entry, before the new insertion guard existed.
    for url in (font, html):
        db.connection.execute('INSERT INTO urls(audit_run_id,url,normalized_url,min_depth,discovery_methods) VALUES(?,?,?,?,?)',
                              (run_id, url, url, 1, '["STATIC"]'))
        db.connection.execute('INSERT INTO page_snapshots(audit_run_id,normalized_url,cleaned_text,word_count) VALUES(?,?,?,?)',
                              (run_id, url, 'Keep HTML mention of font.EOT unchanged', 7))
    db.connection.execute('INSERT INTO url_edges(audit_run_id,source_url,target_url,method) VALUES(?,?,?,?)',
                          (run_id, html, font, 'STATIC'))
    db.connection.commit()
    db.close()
    result = remove_eot(path, tmp_path/'backup')
    assert result['removed_urls'] == 1
    assert result['affected_runs'] == [run_id]
    with sqlite3.connect(path) as saved:
        assert saved.execute('SELECT normalized_url FROM urls').fetchall() == [(html,)]
        assert saved.execute('SELECT cleaned_text FROM page_snapshots').fetchone()[0] == 'Keep HTML mention of font.EOT unchanged'
        assert saved.execute('SELECT COUNT(*) FROM url_edges').fetchone()[0] == 0
    with sqlite3.connect(tmp_path/'backup'/'coverage.sqlite') as recovery:
        assert recovery.execute('SELECT COUNT(*) FROM urls').fetchone()[0] == 2


def test_purge_refuses_active_scan(tmp_path):
    path = tmp_path/'audit.sqlite'
    db = AuditDatabase(path)
    db.create_run('https://example.com/', '', {})
    db.close()
    with pytest.raises(ValueError, match='Pause active scans'):
        remove_eot(path, tmp_path/'backup')

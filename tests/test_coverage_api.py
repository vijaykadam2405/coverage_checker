from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from crawler_coverage_auditor.analysis.coverage import coverage_summary
from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.corpus import credentials
from crawler_coverage_auditor.corpus.coverage_api import CoverageAPIClient
from crawler_coverage_auditor.corpus.errors import CorpusUnavailable
from crawler_coverage_auditor.storage.models import DiscoveredURL


def response(url, held=True, **fields):
    return {'url': url, 'held': held, 'match': 'exact' if held else None,
            'matched_url': url if held else None, **fields}


def test_authenticated_batch_and_variant_mapping():
    calls = []
    urls = ['https://example.test/a', 'https://example.test/b', 'https://example.test/missing']
    def handler(request):
        calls.append(request)
        assert request.url.path == '/v1/held' and request.method == 'POST'
        assert request.headers['authorization'] == 'Bearer test-only-key'
        assert json.loads(request.content) == {'urls': urls}
        return httpx.Response(200, json={'results': [response(urls[0]),
            response(urls[1], match='variant', matched_url='http://www.example.test/b/'), response(urls[2], False)]})
    async def exercise():
        async with CoverageAPIClient('https://api.test', min_interval=0, transport=httpx.MockTransport(handler)) as api:
            result = await api.held(urls + [urls[0]])
            assert result[urls[1]]['match'] == 'variant' and not result[urls[2]]['held']
            with pytest.raises(ValueError):
                await api.held([f'https://example.test/{i}' for i in range(501)])
    asyncio.run(exercise())
    assert len(calls) == 1


@pytest.mark.parametrize('results', [None, [], [{'url': 'https://other.test/', 'held': False}],
    [{'url': 'https://example.test/', 'held': 'false'}], [response('https://example.test/', matched_url='https://other.test/')],
    [response('https://example.test/'), response('https://example.test/')]])
def test_malformed_or_missing_results_never_prove_absence(results):
    async def exercise():
        async with CoverageAPIClient('https://api.test', min_interval=0,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'results': results}))) as api:
            with pytest.raises(CorpusUnavailable):
                await api.held(['https://example.test/'])
    asyncio.run(exercise())


@pytest.mark.parametrize('status', [401, 403, 413, 302])
def test_terminal_errors_and_redirects_do_not_retry_or_leak_headers(status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'Location': 'https://outside.test/'}, text='secret response should not appear in errors')
    async def exercise():
        async with CoverageAPIClient('https://api.test', min_interval=0, transport=httpx.MockTransport(handler)) as api:
            with pytest.raises(CorpusUnavailable) as error:
                await api.held(['https://example.test/'])
            assert 'secret response' not in str(error.value) and 'test-only-key' not in str(error.value)
    asyncio.run(exercise())
    assert len(calls) == 1


@pytest.mark.parametrize('status', [429, 503])
def test_retry_after_and_recovery(status):
    calls, waits, events = [], [], []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={'Retry-After': '7'}) if len(calls) == 1 else httpx.Response(200, json={'results': [response('https://example.test/')]})
    async def exercise():
        async with CoverageAPIClient('https://api.test', retries=1, min_interval=0,
            on_event=lambda kind, *args, **kwargs: events.append(kind), transport=httpx.MockTransport(handler)) as api:
            async def wait(seconds): waits.append(seconds)
            api._wait = wait
            assert (await api.held(['https://example.test/']))['https://example.test/']['held']
    asyncio.run(exercise())
    assert len(calls) == 2 and 7 in waits
    assert events == ['CORPUS_BACKOFF', 'CORPUS_RECOVERED']


def test_long_cooldown_date_and_cancellation():
    assert CoverageAPIClient._retry_after(format_datetime(datetime.now(UTC) + timedelta(seconds=120))) > 118
    async def exercise():
        async with CoverageAPIClient('https://api.test', transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={'Retry-After': '120'}))) as api:
            with pytest.raises(CorpusUnavailable, match='resume later'):
                await api.held(['https://example.test/'])
        async with CoverageAPIClient('https://api.test', stopped=lambda: True) as api:
            with pytest.raises(CorpusUnavailable, match='stopped'):
                await api.held(['https://example.test/'])
    asyncio.run(exercise())


def inventory(tmp_path, count=3):
    config = AuditConfig(target_url='https://example.test/', corpus_url='https://api.test/',
        database_path=tmp_path/'audit.sqlite', output_dir=tmp_path/'reports', corpus_batch_size=2, corpus_interval_seconds=0, max_retries=0)
    auditor = CoverageAuditor(config)
    for i in range(count):
        url = f'https://example.test/{i}'
        auditor.db.add_url(auditor.run_id, DiscoveredURL(url, url, 'STATIC', 1))
        auditor.db.add_http_result(auditor.run_id, url, status_code=200, validity='VALID_PAGE')
    auditor.db.inventory_ready(auditor.run_id)
    return config, auditor


def mock_api(monkeypatch, handler):
    monkeypatch.setattr('crawler_coverage_auditor.auditor.CoverageAPIClient',
        lambda base, **kwargs: CoverageAPIClient(base, transport=httpx.MockTransport(handler), **kwargs))


def test_api_batches_resume_and_historical_evidence_preserved(tmp_path, monkeypatch):
    config, auditor = inventory(tmp_path, count=5)
    first = 'https://example.test/0'
    auditor.db.add_corpus_match(auditor.run_id, first, 'NOT_FOUND', None, None, None, None, 'TEXT_NOT_AVAILABLE')
    calls, fail = [], [True]
    def handler(request):
        urls = json.loads(request.content)['urls']
        calls.append(urls)
        if fail[0] and 'https://example.test/2' in urls:
            return httpx.Response(503)
        return httpx.Response(200, json={'results': [response(url, url.endswith('/0'), title='Stored page', versions=2) for url in urls]})
    mock_api(monkeypatch, handler)
    assert asyncio.run(auditor._verify_all()) == 'CORPUS_PAUSED'
    assert auditor.db.connection.execute('SELECT COUNT(*) FROM corpus_match_history').fetchone()[0] == 1
    saved = [dict(row) for row in auditor.db.report_rows(auditor.run_id)]
    assert coverage_summary(saved)['checked_urls'] == 2
    assert coverage_summary(saved)['text_checked_urls'] == 0
    fail[0] = False
    assert asyncio.run(auditor._verify_all()) == 'LIMITED'
    assert first not in [url for batch in calls[2:] for url in batch]
    assert coverage_summary([dict(row) for row in auditor.db.report_rows(auditor.run_id)])['checked_urls'] == 5
    assert auditor.db.connection.execute('SELECT COUNT(*) FROM content_evidence').fetchone()[0] == 0
    assert 'test-only-key' not in json.dumps(asdict(config), default=str)
    assert 'test-only-key' not in json.dumps(saved)
    auditor.db.close()


def test_redirect_canonical_and_presence_saved_before_alias_failure(tmp_path, monkeypatch):
    config, auditor = inventory(tmp_path, count=3)
    auditor.db.connection.execute("UPDATE http_results SET final_url='https://example.test/redirect' WHERE normalized_url='https://example.test/1'")
    auditor.db.connection.execute("UPDATE urls SET canonical_url='https://example.test/canonical' WHERE normalized_url='https://example.test/2'")
    auditor.db.connection.commit()
    failed = [True]
    def handler(request):
        urls = json.loads(request.content)['urls']
        if 'https://example.test/redirect' in urls and failed[0]:
            return httpx.Response(503)
        return httpx.Response(200, json={'results': [response(url, url.endswith(('/0', '/redirect', '/canonical'))) for url in urls]})
    mock_api(monkeypatch, handler)
    assert asyncio.run(auditor._verify_all()) == 'CORPUS_PAUSED'
    rows = [dict(row) for row in auditor.db.report_rows(auditor.run_id)]
    assert rows[0]['match_status'] == 'EXACT_MATCH' and rows[1]['match_status'] is None
    failed[0] = False
    asyncio.run(auditor._verify_all())
    assert [row['match_status'] for row in auditor.db.report_rows(auditor.run_id)] == ['EXACT_MATCH', 'REDIRECT_MATCH', 'CANONICAL_MATCH']
    auditor.db.close()


def test_credentials_missing_no_dashboard_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv('CRAWLSCOPE_CORPUS_API_KEY')
    _, auditor = inventory(tmp_path)
    assert asyncio.run(auditor._verify_all()) == 'CORPUS_PAUSED'
    assert auditor.db.connection.execute('SELECT COUNT(*) FROM corpus_matches').fetchone()[0] == 0
    auditor.db.close()


@pytest.mark.skipif(__import__('os').name != 'nt', reason='Windows DPAPI')
def test_key_is_encrypted_and_can_be_rotated_locally(monkeypatch):
    credentials.save_key('a-local-test-key')
    stored = credentials.credential_path().read_text()
    assert 'a-local-test-key' not in stored
    monkeypatch.delenv('CRAWLSCOPE_CORPUS_API_KEY')
    assert credentials.load_key() == 'a-local-test-key'
    credentials.save_key('a-rotated-test-key')
    assert credentials.load_key() == 'a-rotated-test-key'


def test_only_one_request_in_flight_and_request_spacing():
    active, peak, started = [0], [0], []
    async def handler(request):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        started.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.01)
        active[0] -= 1
        return httpx.Response(200, json={'results': [response(url) for url in json.loads(request.content)['urls']]})
    async def exercise():
        async with CoverageAPIClient('https://api.test', min_interval=0.04, transport=httpx.MockTransport(handler)) as api:
            await asyncio.gather(*(api.held([f'https://example.test/{i}']) for i in range(3)))
    asyncio.run(exercise())
    assert peak[0] == 1
    assert all(right - left >= 0.04 for left, right in zip(started, started[1:]))


def test_connection_failure_is_bounded_and_invalid_json_is_unverified():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError('remote secret message must not reach reports')
    async def exercise():
        async with CoverageAPIClient('https://api.test', retries=1, min_interval=0, transport=httpx.MockTransport(handler)) as api:
            async def wait(_): pass
            api._wait = wait
            with pytest.raises(CorpusUnavailable) as error:
                await api.held(['https://example.test/'])
            assert 'secret message' not in str(error.value)
        async with CoverageAPIClient('https://api.test', transport=httpx.MockTransport(lambda _: httpx.Response(200, text='not json'))) as api:
            with pytest.raises(CorpusUnavailable, match='invalid JSON'):
                await api.held(['https://example.test/'])
    asyncio.run(exercise())
    assert len(calls) == 2


def test_different_endpoint_results_are_rechecked_and_old_matches_archived(tmp_path, monkeypatch):
    _, auditor = inventory(tmp_path, count=1)
    url = 'https://example.test/0'
    auditor.db.add_corpus_match(auditor.run_id, url, 'EXACT_MATCH', url, None, None, None,
        'API_TEXT_NOT_SUPPORTED', provider='coverage_api:https://previous-api.test')
    mock_api(monkeypatch, lambda _: httpx.Response(200, json={'results': [response(url, False)]}))
    assert asyncio.run(auditor._verify_all()) == 'COMPLETED'
    assert auditor.db.report_rows(auditor.run_id)[0]['match_status'] == 'NOT_FOUND'
    assert auditor.db.connection.execute('SELECT provider FROM corpus_match_history').fetchone()[0] == 'coverage_api:https://previous-api.test'
    auditor.db.close()


def test_invalid_secret_fails_without_including_it_in_error(monkeypatch):
    monkeypatch.setenv('CRAWLSCOPE_CORPUS_API_KEY', 'secret header')
    with pytest.raises(CorpusUnavailable) as error:
        CoverageAPIClient('https://api.test')
    assert 'secret' not in str(error.value)


def test_semantic_failure_never_counts_as_missing():
    async def exercise():
        async with CoverageAPIClient('https://api.test', transport=httpx.MockTransport(lambda _: httpx.Response(200,
            json={'ok': False, 'error': 'database busy', 'results': [response('https://example.test/', False)]}))) as api:
            with pytest.raises(CorpusUnavailable, match='unsuccessful'):
                await api.held(['https://example.test/'])
    asyncio.run(exercise())


def test_corpus_compatibility_is_consistent_for_api_origins(monkeypatch):
    real_client=httpx.AsyncClient
    modes=[]
    def recording_client(**kwargs):
        modes.append(kwargs['verify'])
        kwargs['transport']=httpx.MockTransport(lambda _:httpx.Response(200))
        return real_client(**kwargs)
    monkeypatch.setattr('crawler_coverage_auditor.corpus.coverage_api.httpx.AsyncClient',recording_client)
    async def exercise():
        for origin in ['https://187.127.134.12:8445/','https://187.127.134.12:8443/','https://api.example.test:8445/']:
            async with CoverageAPIClient(origin): pass
    asyncio.run(exercise())
    assert modes==[False,False,False]

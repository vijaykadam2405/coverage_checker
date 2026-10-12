"""Updated contract regression tests; transport/credentials never reach production."""
import asyncio
import json

import httpx
import pytest

from crawler_coverage_auditor.analysis.coverage import coverage_summary, metrics
from crawler_coverage_auditor.analysis.states import text_comparison_state
from crawler_coverage_auditor.corpus.coverage_api import CoverageAPIClient
from crawler_coverage_auditor.corpus.errors import CorpusUnavailable
from crawler_coverage_auditor.services import queries
from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.validation.text_coverage import compare_html_text
from test_coverage_api import inventory, mock_api, response


def text_result(url, text='beginning middle ending', **fields):
    return {'requested_url': url, 'lookup_status': 'found', 'error': None,
            'matched_url': url, 'record_id': 'doc-test', 'content_type': 'text/html',
            'title': 'Test page', 'version_id': 'v1', 'last_fetched_at': '2026-10-10T00:00:00Z',
            'html_text': {'status': 'available', 'text': text, 'truncated': False, 'chars': len(text)}, **fields}


def snapshot(auditor, url, text):
    auditor.db.add_page_snapshot(auditor.run_id, url, text)
    auditor.db.connection.execute('UPDATE http_results SET text_length=? WHERE audit_run_id=? AND normalized_url=?',
                                  (len(text), auditor.run_id, url))
    auditor.db.connection.commit()


def test_text_items_contract_and_shared_held_text_lane():
    calls, active, peak, starts = [], [0], [0], []
    async def handler(request):
        calls.append(request.url.path)
        assert request.headers['Authorization'] == 'Bearer test-only-key'
        active[0] += 1; peak[0] = max(peak[0], active[0])
        starts.append(asyncio.get_running_loop().time())
        await asyncio.sleep(.005)
        active[0] -= 1
        payload = json.loads(request.content)
        if request.url.path == '/v1/text':
            assert payload == {'items': [{'url': 'https://example.test/'}]}
            results = [text_result('https://example.test/')]
        else:
            results = [response(url) for url in payload['urls']]
        return httpx.Response(200, json={'results': results})
    async def exercise():
        async with CoverageAPIClient('https://api.test', min_interval=.02, transport=httpx.MockTransport(handler)) as api:
            await asyncio.gather(api.held(['https://example.test/']), api.text(['https://example.test/']))
            with pytest.raises(ValueError):
                await api.text([f'https://example.test/{i}' for i in range(26)])
    asyncio.run(exercise())
    assert set(calls) == {'/v1/held', '/v1/text'} and peak[0] == 1 and starts[1]-starts[0] >= .02


@pytest.mark.parametrize('changes', [
    {'matched_url': 'https://wrong.test/'}, {'requested_url': 'https://wrong.test/'},
    {'lookup_status': []}, {'html_text': None},
    {'html_text': {'status': 'available', 'text': None, 'truncated': False}},
    {'html_text': {'status': 'available', 'text': 12, 'truncated': False}},
    {'html_text': {'status': 'available', 'text': 'body', 'truncated': 'false'}},
    {'html_text': {'status': 'empty', 'text': 'not empty', 'truncated': False}},
])
def test_malformed_text_never_becomes_completeness(changes):
    async def exercise():
        async with CoverageAPIClient('https://api.test', min_interval=0,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'results': [text_result('https://example.test/', **changes)]}))) as api:
            with pytest.raises(CorpusUnavailable):
                await api.text(['https://example.test/'])
    asyncio.run(exercise())


def test_text_deferred_or_omitted_is_not_empty_text():
    async def exercise():
        async with CoverageAPIClient('https://api.test', min_interval=0,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'deferred': 1, 'results': []}))) as api:
            with pytest.raises(CorpusUnavailable, match='omitted'):
                await api.text(['https://example.test/'])
    asyncio.run(exercise())


def test_retention_reuses_similarity_and_checks_ending_and_duplicates():
    result = compare_html_text('alpha beta gamma delta epsilon zeta', 'navigation alpha beta gamma delta footer')
    assert result['score'] == pytest.approx(4/6)
    assert result['evidence']['segments'] == {'beginning': 100, 'middle': 100, 'ending': 0}
    assert result['status'] == 'FOUND_PARTIAL_TEXT'
    assert compare_html_text('same same same end', 'same end')['score'] == 1
    assert compare_html_text('beginning middle ending '*10, 'beginning middle ending')['score'] == 1
    assert compare_html_text('first middle last', 'menu first middle last footer')['status'] == 'FOUND_FULL'
    assert compare_html_text('first middle last', '')['status'] == 'TEXT_MISSING'
    assert compare_html_text('', 'something')['score'] is None


def test_html_comparison_and_files_url_only_and_not_tracked(tmp_path, monkeypatch):
    config, auditor = inventory(tmp_path, count=1)
    url = 'https://example.test/0'
    snapshot(auditor, url, 'beginning middle ending')
    for address, kind in [('https://example.test/file.pdf', 'DOCUMENT'), ('https://example.test/image.png', 'IMAGE')]:
        auditor.db.add_url(auditor.run_id, DiscoveredURL(address, address, 'STATIC', 1, page_type=kind))
        auditor.db.add_http_result(auditor.run_id, address, validity='URL_ONLY')
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path == '/v1/text':
            assert json.loads(request.content) == {'items': [{'url': url}]}
            return httpx.Response(200, json={'results': [text_result(url)]})
        return httpx.Response(200, json={'results': [response(u, lookup_status='found') if not u.endswith('.png')
            else {'url': u, 'held': None, 'lookup_status': 'not_tracked', 'error': 'images_not_tracked'}
            for u in json.loads(request.content)['urls']]})
    mock_api(monkeypatch, handler)
    assert asyncio.run(auditor._verify_all()) == 'COMPLETED'
    rows = [dict(row) for row in auditor.db.report_rows(auditor.run_id)]
    html, image = next(r for r in rows if r['page_type']=='HTML'), next(r for r in rows if r['page_type']=='IMAGE')
    assert html['text_status'] == 'FOUND_FULL' and html['similarity'] == 1
    assert image['match_status'] == 'NOT_TRACKED'
    assert next(r for r in rows if r['page_type']=='DOCUMENT')['text_status'] == 'URL_ONLY'
    assert coverage_summary(rows, include_images=True)['missing_urls'] == 0
    assert coverage_summary(rows, include_images=True)['unverified_urls'] == 0
    assert text_comparison_state(html)['text_coverage_method'] == 'unique_word_coverage'
    summary = queries.summary(config.database_path, auditor.run_id)
    assert summary['metrics']['not_tracked'] == 1 and summary['metrics']['verification_completion'] == 100
    assert summary['types']['IMAGE']['verified_coverage'] is None
    assert calls.count('/v1/text') == 1
    assert 'beginning middle ending' not in html['evidence_json']
    detail = queries.url_detail(config.database_path, auditor.run_id, queries.url_page(config.database_path, auditor.run_id)['items'][0]['id'])
    assert detail is not None
    assert asyncio.run(auditor._verify_all()) == 'COMPLETED'
    assert calls.count('/v1/text') == 1  # Successful comparisons and not_tracked don't repeat.
    auditor.db.close()


def test_text_failure_resume_preserves_presence_and_compares_only_remaining(tmp_path, monkeypatch):
    _, auditor = inventory(tmp_path, count=3)
    for i in range(3):
        snapshot(auditor, f'https://example.test/{i}', 'beginning middle ending')
    failed, calls = [True], []
    def handler(request):
        if request.url.path == '/v1/held':
            return httpx.Response(200, json={'results': [response(url) for url in json.loads(request.content)['urls']]})
        url = json.loads(request.content)['items'][0]['url']; calls.append(url)
        if failed[0] and url.endswith('/1'):
            return httpx.Response(503)
        return httpx.Response(200, json={'results': [text_result(url)]})
    mock_api(monkeypatch, handler)
    assert asyncio.run(auditor._verify_all()) == 'CORPUS_PAUSED'
    rows = [dict(row) for row in auditor.db.report_rows(auditor.run_id)]
    assert [row['match_status'] for row in rows] == ['EXACT_MATCH', 'EXACT_MATCH', None]
    assert rows[1]['text_status'] == 'TEXT_API_ERROR' and rows[1]['similarity'] is None
    assert coverage_summary(rows)['text_checked_urls'] == 1
    failed[0] = False
    assert asyncio.run(auditor._verify_all()) == 'LIMITED'
    assert calls == ['https://example.test/0', 'https://example.test/1', 'https://example.test/1', 'https://example.test/2']
    assert coverage_summary([dict(r) for r in auditor.db.report_rows(auditor.run_id)])['text_checked_urls'] == 3
    auditor.db.close()


@pytest.mark.parametrize('fields,status,score', [
    ({'html_text': {'status': 'available', 'truncated': True, 'text': 'beginning'}}, 'TEXT_TRUNCATED', None),
    ({'content_type': 'application/pdf'}, 'TEXT_TYPE_MISMATCH', None),
    ({'html_text': {'status': 'empty', 'truncated': False, 'text': ''}}, 'TEXT_MISSING', 0),
    ({'html_text': {'status': 'unavailable', 'truncated': False, 'text': None}}, 'TEXT_NOT_AVAILABLE', None),
])
def test_unavailable_truncated_nonhtml_and_empty_are_distinct(tmp_path, monkeypatch, fields, status, score):
    _, auditor = inventory(tmp_path, count=1); url = 'https://example.test/0'
    snapshot(auditor, url, 'beginning middle ending')
    mock_api(monkeypatch, lambda request: httpx.Response(200, json={'results': [response(url)] if request.url.path=='/v1/held' else [text_result(url, **fields)]}))
    asyncio.run(auditor._verify_all())
    row = dict(auditor.db.report_rows(auditor.run_id)[0])
    assert row['match_status'] == 'EXACT_MATCH' and row['text_status'] == status and row['similarity'] == score
    assert coverage_summary([row])['text_checked_urls'] == (1 if status=='TEXT_MISSING' else 0)
    assert text_comparison_state(row)['text_coverage_percent'] == (0 if status=='TEXT_MISSING' else None)
    auditor.db.close()


def test_old_presence_only_inventory_upgrades_without_presence_recheck(tmp_path, monkeypatch):
    _, auditor = inventory(tmp_path, count=1); url = 'https://example.test/0'
    snapshot(auditor, url, 'beginning middle ending')
    auditor.db.add_corpus_match(auditor.run_id, url, 'EXACT_MATCH', url, None, None, None,
        'API_TEXT_NOT_SUPPORTED', provider='coverage_api:https://api.test')
    def handler(request):
        assert request.url.path == '/v1/text'
        return httpx.Response(200, json={'results': [text_result(url)]})
    mock_api(monkeypatch, handler)
    assert asyncio.run(auditor._verify_all()) == 'COMPLETED'
    assert auditor.db.report_rows(auditor.run_id)[0]['text_status'] == 'FOUND_FULL'
    auditor.db.close()


def test_per_url_lookup_error_never_proves_missing(tmp_path, monkeypatch):
    _, auditor = inventory(tmp_path, count=2)
    def handler(request):
        urls = json.loads(request.content)['urls']
        return httpx.Response(200, json={'results': [response(u) if u.endswith('/0') else
            {'url': u, 'lookup_status': 'error', 'held': None, 'error': 'busy'} for u in urls]})
    mock_api(monkeypatch, handler)
    assert asyncio.run(auditor._verify_all()) == 'CORPUS_PAUSED'
    assert [r['match_status'] for r in auditor.db.report_rows(auditor.run_id)] == ['EXACT_MATCH', 'API_ERROR']
    assert coverage_summary([dict(r) for r in auditor.db.report_rows(auditor.run_id)])['missing_urls'] == 0
    auditor.db.close()


def test_not_tracked_is_excluded_from_denominator_not_inventory():
    result = metrics(3, 1, 1, 1)
    assert result['eligible'] == 3 and result['unverified'] == 0
    assert result['verification_completion'] == 100 and result['verified_coverage'] == 50


def test_resource_contract_upgrade_resumes_unchecked_old_results(tmp_path, monkeypatch):
    config, auditor = inventory(tmp_path, count=0)
    config.corpus_batch_size = 1
    for i in range(2):
        url = f'https://example.test/file-{i}.pdf'
        auditor.db.add_url(auditor.run_id, DiscoveredURL(url,url,'STATIC',1,page_type='DOCUMENT'))
        auditor.db.add_http_result(auditor.run_id,url,validity='URL_ONLY')
        auditor.db.add_corpus_match(auditor.run_id,url,'NOT_FOUND',None,None,None,None,'URL_ONLY',provider='coverage_api:https://api.test')
    failed, calls = [True], []
    def handler(request):
        assert request.url.path == '/v1/held'
        urls = json.loads(request.content)['urls']; calls.extend(urls)
        if failed[0] and urls[0].endswith('1.pdf'):
            return httpx.Response(503)
        return httpx.Response(200,json={'results':[response(url) for url in urls]})
    mock_api(monkeypatch,handler)
    assert asyncio.run(auditor._verify_all()) == 'CORPUS_PAUSED'
    failed[0] = False
    assert asyncio.run(auditor._verify_all()) == 'LIMITED'
    assert calls == ['https://example.test/file-0.pdf','https://example.test/file-1.pdf','https://example.test/file-1.pdf']
    assert all(r['match_status']=='EXACT_MATCH' for r in auditor.db.report_rows(auditor.run_id))
    assert auditor.db.connection.execute('SELECT COUNT(*) FROM corpus_match_history').fetchone()[0] == 2
    auditor.db.close()

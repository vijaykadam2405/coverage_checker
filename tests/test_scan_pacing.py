"""Moderately faster scans, using local fixtures only; never production API requests."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_coverage_api import response

from crawler_coverage_auditor.config import (
    AUTOMATIC_DISCOVERY_SOURCES,
    PACING_VERSION,
    paced_scan_settings,
)
from crawler_coverage_auditor.corpus.coverage_api import CoverageAPIClient
from crawler_coverage_auditor.services.jobs import AuditJobs
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.web.api import create_app


def test_defaults_are_faster_but_still_serial_and_paced(tmp_path, monkeypatch):
    jobs = AuditJobs()
    monkeypatch.setattr(jobs, '_launch', lambda config, *args: config)
    config = jobs.start('https://example.test/', tmp_path/'audit.sqlite', tmp_path/'reports', 'https://api.test/')
    assert config.website_delay_seconds == 1 and config.corpus_interval_seconds == 2
    assert config.max_concurrency == config.corpus_batch_size == 1
    assert config.pacing_version == PACING_VERSION
    client = TestClient(create_app(tmp_path/'audit.sqlite', tmp_path/'reports'))
    settings = client.get('/api/state').json()['settings']
    assert settings['website_delay_seconds'] == 1 and settings['corpus_interval_seconds'] == 2
    assert settings['corpus_timeout_seconds'] == 60
    for invalid in ({'website_delay_seconds': .99}, {'corpus_interval_seconds': 1.99}):
        assert client.put('/api/settings', headers={'X-CrawlScope-Client': '1'}, json=settings | invalid).status_code == 422


def test_old_saved_defaults_upgrade_without_changing_capture_scope(tmp_path):
    settings_file = tmp_path/'capture_settings.json'
    settings_file.write_text(json.dumps({'schema_version': 2, 'capture_types': ['HTML', 'PDF'],
        'discovery_sources': ['STATIC', 'SITEMAP'], 'website_delay_seconds': 1.5, 'corpus_interval_seconds': 3,
        'use_browser': False}))
    settings = TestClient(create_app(tmp_path/'audit.sqlite', tmp_path/'reports')).get('/api/state').json()['settings']
    assert settings['website_delay_seconds'] == 1 and settings['corpus_interval_seconds'] == 2
    assert settings['capture_types'] == ['HTML', 'PDF']
    assert set(settings['discovery_sources']) == AUTOMATIC_DISCOVERY_SOURCES and settings['use_browser']
    assert settings['pacing_version'] == PACING_VERSION
    # A read upgrades the effective configuration, not the user's saved file/data.
    assert json.loads(settings_file.read_text())['corpus_interval_seconds'] == 3


@pytest.mark.parametrize('values', [
    {'website_delay_seconds': 4, 'corpus_interval_seconds': 7},
    {'pacing_version': PACING_VERSION, 'website_delay_seconds': 1.5, 'corpus_interval_seconds': 3},
])
def test_intentional_slower_settings_are_preserved(values):
    result = paced_scan_settings(values, upgrade_legacy=True)
    assert result['website_delay_seconds'] == values['website_delay_seconds']
    assert result['corpus_interval_seconds'] == values['corpus_interval_seconds']


@pytest.mark.parametrize('inventory_ready', [False, True])
def test_old_discovery_and_corpus_resumes_use_updated_defaults(tmp_path, monkeypatch, inventory_ready):
    database = tmp_path/'audit.sqlite'
    db = AuditDatabase(database)
    run_id = db.create_run('https://example.test/', 'https://api.test/',
        {'capture_types': ['HTML'], 'website_delay_seconds': 1.5, 'corpus_interval_seconds': 3})
    if inventory_ready:
        db.inventory_ready(run_id)
        db.finish_run(run_id, 'CORPUS_PAUSED')
    db.close()
    jobs = AuditJobs()
    monkeypatch.setattr(jobs, '_launch', lambda config, *args: config)
    config = jobs.resume(run_id, database, tmp_path/'reports')
    assert config.website_delay_seconds == 1 and config.corpus_interval_seconds == 2
    assert config.capture_types == {'HTML'}
    assert config.max_concurrency == config.corpus_batch_size == 1


def test_faster_corpus_default_still_honors_retry_after_and_slows_down():
    calls, waits, intervals = [], [], []
    async def exercise():
        def handler(request):
            calls.append(json.loads(request.content))
            intervals.append(api._interval)
            return httpx.Response(429, headers={'Retry-After': '7'}) if len(calls) == 1 else httpx.Response(200, json={'results': [response('https://example.test/')]})
        async with CoverageAPIClient('https://api.test', min_interval=2, retries=1,
                                     transport=httpx.MockTransport(handler)) as api:
            async def wait(seconds):
                waits.append(seconds)
            api._wait = wait
            assert (await api.held(['https://example.test/']))['https://example.test/']['held']
            assert api._interval >= 2
    asyncio.run(exercise())
    assert len(calls) == 2 and all(len(body['urls']) == 1 for body in calls)
    assert 7 in waits and intervals[1] >= 4


def test_slow_successful_corpus_response_increases_spacing(monkeypatch):
    now, waits, requests = [0.0], [], []
    async def exercise():
        async with CoverageAPIClient('https://api.test', min_interval=2) as api:
            async def post(endpoint, *, json):
                requests.append(json)
                now[0] += 6
                return httpx.Response(200, json={'results': [response(json['urls'][0])]})
            async def wait(seconds):
                waits.append(seconds)
                now[0] += seconds
            # No I/O/yields under the synthetic clock; restore before loop teardown.
            with monkeypatch.context() as patch:
                patch.setattr(asyncio.get_running_loop(), 'time', lambda: now[0])
                patch.setattr(api._client, 'post', post)
                patch.setattr(api, '_wait', wait)
                await api.held(['https://example.test/a'])
                await api.held(['https://example.test/b'])
                assert api._interval == 3
    asyncio.run(exercise())
    assert waits == [0, 3]
    assert all(len(body['urls']) == 1 for body in requests)

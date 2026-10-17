import json
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_react_api import Jobs

from crawler_coverage_auditor.config import (
    AUTOMATIC_DISCOVERY_SOURCES,
    DEFAULT_DISCOVERY_SOURCES,
)
from crawler_coverage_auditor.corpus import credentials
from crawler_coverage_auditor.services.jobs import AuditJobs
from crawler_coverage_auditor.web.api import create_app

HEADERS = {'X-CrawlScope-Client': '1'}


def test_all_normal_discovery_is_enabled_even_with_old_disabled_settings(tmp_path):
    settings = {'capture_types': ['PDF'], 'discovery_sources': ['SITEMAP'],
                'use_browser': False, 'include_subdomains': False,
                'external_resources': False, 'website_delay_seconds': 2.5}
    (tmp_path / 'capture_settings.json').write_text(json.dumps(settings))
    jobs = Jobs()
    client = TestClient(create_app(tmp_path / 'audit.sqlite', tmp_path / 'reports', jobs=jobs))
    value = client.get('/api/state').json()['settings']
    assert set(value['discovery_sources']) == AUTOMATIC_DISCOVERY_SOURCES
    assert value['use_browser'] and value['include_subdomains'] and not value['external_resources']
    assert value['capture_types'] == ['PDF'] and value['website_delay_seconds'] == 2.5
    value.update(discovery_sources=[], use_browser=False)
    saved = client.put('/api/settings', headers=HEADERS, json=value).json()
    assert set(saved['discovery_sources']) == AUTOMATIC_DISCOVERY_SOURCES and saved['use_browser']


def test_normal_job_cannot_disable_discovery_methods(tmp_path, monkeypatch):
    jobs = AuditJobs()
    captured = []
    monkeypatch.setattr(jobs, '_launch', lambda config, *args: captured.append(config))
    jobs.start('https://example.test/', tmp_path/'audit.sqlite', tmp_path/'reports', 'https://api.test/',
               capture_types={'HTML'}, settings={'discovery_sources': [], 'use_browser': False})
    assert captured[0].discovery_sources == AUTOMATIC_DISCOVERY_SOURCES
    assert captured[0].use_browser and captured[0].capture_types == {'HTML'}


def test_secure_key_endpoint_does_not_echo_or_store_plaintext(tmp_path, monkeypatch):
    monkeypatch.delenv('CRAWLSCOPE_CORPUS_API_KEY', raising=False)
    monkeypatch.setattr(credentials, '_protect', lambda data, **kwargs: data[::-1])
    jobs = Jobs()
    client = TestClient(create_app(tmp_path/'audit.sqlite', tmp_path/'reports', jobs=jobs))
    key = 'test-only-new-corpus-key'
    assert client.post('/api/corpus/key', json={'key': key}).status_code == 403
    assert client.post('/api/corpus/key', headers=HEADERS | {'Origin':'https://outside.example'}, json={'key':key}).status_code == 403
    response = client.post('/api/corpus/key', headers=HEADERS, json={'key': key})
    assert response.status_code == 200 and response.json()['configured']
    assert response.json()['source'] == 'protected_local'
    assert key not in response.text and key not in client.get('/api/state').text
    assert key not in credentials.credential_path().read_text()
    assert credentials.load_key() == key
    assert not (tmp_path / 'capture_settings.json').exists()
    assert not (tmp_path / 'audit.sqlite').exists()
    assert jobs.calls == []
    old = credentials.credential_path().read_bytes()
    invalid = client.post('/api/corpus/key', headers=HEADERS, json={'key': 'invalid secret $ value'})
    assert invalid.status_code == 422 and 'invalid secret' not in invalid.text
    assert credentials.credential_path().read_bytes() == old
    jobs.job = SimpleNamespace(run_id=1, action='discovery', target='https://example.test/', stop_event=threading.Event())
    assert client.post('/api/corpus/key', headers=HEADERS, json={'key': 'another-test-key'}).status_code == 409
    assert credentials.credential_path().read_bytes() == old


def test_key_validation_redacts_inputs_and_environment_keys_take_precedence(tmp_path):
    client = TestClient(create_app(tmp_path/'audit.sqlite', tmp_path/'reports', jobs=Jobs()))
    for body in ({'key': ' '}, {'key': 'secret'*200}, {'key': ['secret']}, ['secret'], {}):
        response = client.post('/api/corpus/key', headers=HEADERS, json=body)
        assert response.status_code == 422 and 'secret' not in response.text
    response = client.post('/api/corpus/key', headers=HEADERS, json={'key': 'valid-test-key'})
    assert response.status_code == 409 and 'environment' in response.text
    assert not credentials.credential_path().exists()
    too_large = client.post('/api/corpus/key', headers=HEADERS, json={'key':'secret'*1000})
    assert too_large.status_code == 413 and 'secret' not in too_large.text


def test_wayback_only_upgrade_preserves_completed_page_work(tmp_path):
    from crawler_coverage_auditor.auditor import CoverageAuditor
    from crawler_coverage_auditor.config import AuditConfig
    from crawler_coverage_auditor.storage.database import AuditDatabase
    from crawler_coverage_auditor.storage.models import DiscoveredURL
    path = tmp_path/'audit.sqlite'
    db = AuditDatabase(path)
    url = 'https://example.test/'
    run_id = db.create_run(url, 'https://api.test/', {'discovery_version':2,
        'discovery_sources': list(DEFAULT_DISCOVERY_SOURCES), 'use_browser':True,
        'include_subdomains':True, 'external_resources':True})
    db.add_url(run_id, DiscoveredURL(url,url,'SEED',0))
    db.add_http_result(run_id,url,status_code=200,validity='VALID_PAGE')
    db.set_url_state(run_id,url,'DONE')
    db.close()
    audit = CoverageAuditor(AuditConfig(target_url=url,database_path=path,resume=True,
        use_browser=True,include_subdomains=True,discovery_sources=set(AUTOMATIC_DISCOVERY_SOURCES)),run_id=run_id)
    try:
        assert audit._discovery_upgrade and not audit._upgrade_revisit_sources
        assert audit.advanced.prepare_upgrade(revisit_sources=False) == 0
        audit.advanced.seed_external()
        assert audit.db.connection.execute('SELECT state FROM urls').fetchone()[0] == 'DONE'
        assert audit.db.connection.execute('SELECT kind FROM discovery_tasks').fetchone()[0] == 'WAYBACK'
    finally:
        audit.db.close()

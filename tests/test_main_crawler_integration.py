"""Complete source accounting and live bridges without production side effects."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from crawler_coverage_auditor.discovery.browser import BrowserExplorer
from crawler_coverage_auditor.discovery.evidence import header_links
from crawler_coverage_auditor.discovery.static_links import extract_links
from crawler_coverage_auditor.integrations import main_crawler as bridge
from crawler_coverage_auditor.validation.access import detect_access
from crawler_coverage_auditor.web.api import create_app
from scripts.import_main_crawler import import_source


def test_every_imported_source_artifact_verifies_without_execution():
    data = bridge.manifest()
    assert data['python_sources'] == 311
    assert len([row for row in data['files'] if row['path'].endswith('.py')]) == 311
    assert all(row['status'] == 'copied' for row in data['files'] if row['path'].endswith('.py'))
    result = bridge.verify_source(check_syntax=True)
    assert result['ok'] and result['checked_files'] == 483
    assert result['excluded_files'] == 41
    excluded_images = {row['path'] for row in data['files'] if row['status'] == 'excluded' and Path(row['path']).suffix in {'.jpg', '.png'}}
    assert excluded_images == {'crawler/tests/fixtures/caesar.jpg', 'crawler/tests/fixtures/logo.png'}
    core = [row for row in data['files'] if row['path'].startswith('crawler/crawler/') and row['path'].endswith('.py')]
    assert {'siteauth.py', 'captcha.py', 'captcha_solver.py', 'hostsolver.py', 'camofox_client.py',
            'stealth.py', 'paid_proxy.py', 'async_engine.py', 'pdfextract.py', 'ingest_client.py'} <= {Path(row['path']).name for row in core}


def test_import_excludes_images_and_cleans_fixture_references_idempotently(tmp_path):
    source=tmp_path/'Crawler.zip'
    destination=tmp_path/'vendor'/'main_crawler'
    with ZipFile(source,'w') as archive:
        archive.writestr('Crawler/crawler/__init__.py','')
        archive.writestr('Crawler/crawler/tests/fixtures/photo.jpg',b'not needed')
        archive.writestr('Crawler/crawler/tests/fixtures/logo.png',b'not needed')
        archive.writestr('Crawler/crawler/tests/fixtures/page.html','<h1>Kept</h1>')
        archive.writestr('Crawler/crawler/tests/fixtures/index.json',json.dumps({
            'https://example.test/':{'file':'page.html'},
            'https://example.test/photo.jpg':{'file':'photo.jpg'},
            'https://example.test/logo.png':{'file':'logo.png'},
        }))
    manifest=import_source(source,destination)
    assert manifest['summary']=={'copied':2,'redacted':1,'excluded':2}
    assert not list(destination.rglob('*.png')) and not list(destination.rglob('*.jpg'))
    assert json.loads((destination/'crawler/tests/fixtures/index.json').read_text())=={
        'https://example.test/':{'file':'page.html'}}
    assert import_source(source,destination)==manifest
    # Harmless Git line-ending normalization must not defeat repeat imports.
    path=destination/'IMPORT_MANIFEST.json'
    path.write_bytes(path.read_bytes().replace(b'\n',b'\r\n'))
    assert import_source(source,destination)==manifest


def test_pure_loader_never_imports_account_ingestion_or_engine_entrypoints():
    for name in ['siteauth', 'async_engine', 'ingest_client', 'captcha_solver', 'hostsolver', '../stealth']:
        with pytest.raises(ValueError, match='pure helpers'):
            bridge.load_helper(name)
    assert bridge.load_helper('parse').__name__.startswith('_crawlscope_main_crawler.')


def test_owned_parsers_replace_the_second_vendor_execution_pass(monkeypatch):
    def forbidden(*args):
        raise AssertionError('Discovery must not run a second vendor parser')
    monkeypatch.setattr(bridge, 'html_links', forbidden)
    monkeypatch.setattr(bridge, 'http_links', forbidden)
    assert {'https://example.test/static', 'https://example.test/next'} <= {
        link.url for link in extract_links('<a href="/static">Normal</a><link rel="previous" href="/next">', 'https://example.test/')}
    assert header_links('</last>; rel="last"', 'https://example.test/')[0].url == 'https://example.test/last'


def test_source_vendor_detector_is_used_without_accepting_widget_presence(monkeypatch):
    detection = SimpleNamespace(found=True, blocking=False, kind='example', solvability='vendor_task')
    monkeypatch.setattr(bridge, 'load_helper', lambda name: SimpleNamespace(detect_http=lambda *args: detection))
    monkeypatch.setenv('CRAWLSCOPE_MAIN_CRAWLER_HELPERS', '1')
    assert detect_access(200, {}, '<h1>Public article</h1>') is None
    detection.blocking = True
    assert detect_access(403, {}, '<h1>Unavailable</h1>').provider == 'example'
    assert bridge.challenge(429, {}, '') is None  # Never use a different tier to defeat a rate limit.


def test_optional_browser_identity_uses_exact_source_helpers(monkeypatch):
    monkeypatch.setenv('CRAWLSCOPE_MAIN_CRAWLER_HELPERS', '1')
    monkeypatch.setenv('CRAWLSCOPE_MAIN_CRAWLER_STEALTH', '1')
    monkeypatch.delenv('CRAWLER_STEALTH', raising=False)
    original = bridge.load_helper('stealth')
    profile = bridge.browser_profile()
    assert profile['init_script'] == original.STEALTH_INIT_JS
    assert profile['args'] == original.stealth_launch_args()
    assert profile['ignore_default_args'] == original.stealth_ignore_default_args()
    monkeypatch.setenv('CRAWLER_STEALTH', '0')
    assert not bridge.browser_profile()['enabled']


def test_disabled_helper_profile_does_not_load_any_original_module(monkeypatch):
    monkeypatch.setenv('CRAWLSCOPE_MAIN_CRAWLER_HELPERS', '0')
    monkeypatch.setenv('CRAWLSCOPE_MAIN_CRAWLER_STEALTH', '1')
    def forbidden(name):
        raise AssertionError('Disabled profile must not import original code')
    monkeypatch.setattr(bridge, 'load_helper', forbidden)
    assert bridge.html_links('', '') == [] and bridge.http_links('', '') == []
    assert bridge.challenge(403, {}, '') is None and not bridge.browser_profile()['enabled']


def test_feature_api_is_readonly_does_not_probe_services_or_expose_keys(tmp_path, monkeypatch):
    monkeypatch.setenv('CAMOFOX_API_KEY', 'fixture-private-key')
    def forbidden(*args, **kwargs):
        raise AssertionError('Feature inspection must not contact services')
    monkeypatch.setattr('httpx.AsyncClient.send', forbidden)
    response = TestClient(create_app(tmp_path/'absent.sqlite', tmp_path/'reports')).get('/api/main-crawler/features')
    assert response.status_code == 200
    data = response.json()
    assert data['python_source_files'] == 311
    assert data['coverage_rules'] == {'html_text_only': True, 'files_url_only': True, 'sequential': True, 'corpus_readonly': True}
    assert 'fixture-private-key' not in response.text and 'CAMOFOX_URL' not in response.text
    assert not (tmp_path/'absent.sqlite').exists()


def test_safe_import_accounts_for_exclusions_and_redaction_without_running_source(tmp_path):
    source = tmp_path/'Crawler.zip'
    with ZipFile(source, 'w') as archive:
        archive.writestr('Crawler/crawler/crawler/example.py', 'raise RuntimeError("must not execute")\n')
        archive.writestr('Crawler/.env', 'SECRET=never-import\n')
        archive.writestr('Crawler/.git/config', 'private metadata')
        archive.writestr('Crawler/crawler/fixtures/example.json', json.dumps({'key': 'cov_'+'A'*30}))
    destination = tmp_path/'vendor/main_crawler'
    data = import_source(source, destination)
    assert data['summary'] == {'copied': 1, 'redacted': 1, 'excluded': 2}
    assert not (destination/'.env').exists() and not (destination/'.git').exists()
    assert 'cov_' not in (destination/'crawler/fixtures/example.json').read_text()
    assert import_source(source, destination) == data
    (destination/'crawler/crawler/example.py').write_text('changed by user')
    with pytest.raises(ValueError, match='overwrite'):
        import_source(source, destination)


@pytest.mark.parametrize('name', ['Crawler/../../escape.py', '/absolute.py', 'Crawler/CON.py', 'Crawler/a.py.', 'Crawler/a.py '])
def test_import_rejects_unsafe_archive_paths_before_creating_source_files(tmp_path, name):
    source = tmp_path/'Crawler.zip'
    with ZipFile(source, 'w') as archive:
        archive.writestr(name, 'invalid')
    with pytest.raises(ValueError):
        import_source(source, tmp_path/'vendor/main_crawler')
    assert not (tmp_path/'vendor').exists()


def test_integrity_verifier_detects_changed_optional_source(tmp_path, monkeypatch):
    source = tmp_path/'Crawler.zip'
    with ZipFile(source, 'w') as archive:
        archive.writestr('Crawler/crawler/crawler/example.py', 'VALUE = 1\n')
    destination = tmp_path/'vendor/main_crawler'
    data = import_source(source, destination)
    monkeypatch.setattr(bridge, 'VENDOR', destination)
    monkeypatch.setattr(bridge, 'manifest', lambda: data)
    file = destination/'crawler/crawler/example.py'
    file.write_bytes(b'VALUE = 1\r\n')
    assert bridge.verify_source()['ok']  # Git line endings are not a logic change.
    file.write_bytes(b'VALUE = 2\n')
    assert bridge.verify_source()['changed'] == ['crawler/crawler/example.py']
    with pytest.raises(RuntimeError, match='changed'):
        bridge._verified_path('crawler/crawler/example.py')


def test_actual_browser_wires_optional_source_profile_without_relaxing_tls(monkeypatch):
    monkeypatch.setenv('CRAWLSCOPE_MAIN_CRAWLER_HELPERS', '1')
    monkeypatch.setenv('CRAWLSCOPE_MAIN_CRAWLER_STEALTH', '1')
    monkeypatch.delenv('CRAWLER_STEALTH', raising=False)
    async def exercise():
        async with BrowserExplorer(actions=1) as browser:
            assert not browser.error
            page = await browser.context.new_page()
            assert await page.evaluate('navigator.webdriver') is None
            await page.close()
    asyncio.run(exercise())

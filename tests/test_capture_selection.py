import asyncio
from types import SimpleNamespace

import pytest

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.services.inventory import list_runs, load_report_rows


def test_capture_types_are_validated():
    with pytest.raises(ValueError):
        AuditConfig(target_url='https://example.test/', capture_types=set())
    with pytest.raises(ValueError):
        AuditConfig(target_url='https://example.test/', capture_types={'EXE'})
    config = AuditConfig(target_url='https://example.test/', capture_types={'PDF'})
    assert config.captures('DOCUMENT', 'https://example.test/a.PDF?x=1')
    assert config.captures('DOCUMENT', 'https://example.test/download', 'application/pdf')
    assert not config.captures('DOCUMENT', 'https://example.test/a.docx')
    assert not config.captures('IMAGE', 'https://example.test/a.jpg')


def test_pdf_only_traverses_html_but_does_not_capture_it(tmp_path, monkeypatch):
    config = AuditConfig(target_url='https://example.test/', capture_types={'PDF'},
                         database_path=tmp_path/'audit.sqlite', output_dir=tmp_path/'reports', discovery_only=True)
    auditor = CoverageAuditor(config)
    requests = []
    class Website:
        async def get(self, url, **kwargs):
            requests.append(url)
            body = '<h1>Public content</h1><a href="/deep">Deep section</a>' if url.endswith('/') else '<h1>Deep content</h1><a href="/report.pdf">Report</a><a href="/doc.docx">Document</a><img src="/photo.jpg"><a href="/archive.zip">Archive</a>'
            return SimpleNamespace(headers={'content-type':'text/html'}, text=body, status_code=200,
                                   final_url=url, url_only=False, error=None)
    try:
        auditor._add_discovery(config.target_url, 'SEED', 0, None, '')
        asyncio.run(auditor._crawl_static(Website()))
        rows = load_report_rows(config.database_path, auditor.run_id)
        assert [row['url'] for row in rows] == ['https://example.test/report.pdf']
        assert rows[0]['validity'] == 'URL_ONLY'
        assert list_runs(config.database_path)[0]['discovered_urls'] == 1
        assert requests == ['https://example.test/', 'https://example.test/deep']
        assert auditor.db.connection.execute('SELECT COUNT(*) FROM page_snapshots').fetchone()[0] == 0
        assert len(auditor.db.report_rows(auditor.run_id, include_traversal=True)) == 3
        assert len(auditor.db.report_rows(auditor.run_id)) == 1
        auditor.db.inventory_ready(auditor.run_id)
        # The real saved-inventory verification selector must omit traversal-only HTML.
        selected=[]
        class API:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def held(self, urls):
                selected.extend(urls)
                return {url: {'url': url, 'held': False, 'lookup_status': 'missing'} for url in urls}
        monkeypatch.setattr('crawler_coverage_auditor.auditor.CoverageAPIClient', API)
        asyncio.run(auditor._verify_all())
        assert selected == ['https://example.test/report.pdf']
    finally:
        auditor.db.close()


def test_extensionless_pdf_reclassification_respects_capture_settings(tmp_path):
    auditor=CoverageAuditor(AuditConfig(target_url='https://example.test/', capture_types={'HTML'}, database_path=tmp_path/'audit.sqlite'))
    try:
        auditor._add_discovery('https://example.test/opaque', 'STATIC', 1, None, '')
        row=dict(auditor.db.pending_urls(auditor.run_id)[0])
        auditor._record_resource_url(row, 'DOCUMENT', content_type='application/pdf')
        assert auditor.db.report_rows(auditor.run_id) == []
        assert auditor.db.connection.execute('SELECT COUNT(*) FROM documents').fetchone()[0] == 0
    finally:
        auditor.db.close()


def test_other_selection_includes_archives_not_office_documents():
    config=AuditConfig(target_url='https://example.test/', capture_types={'OTHER'})
    assert config.captures('DOCUMENT','https://example.test/archive.zip')
    assert not config.captures('RESOURCE','https://example.test/movie.mp4')
    assert AuditConfig(target_url='https://example.test/', capture_types={'MEDIA'}).captures('RESOURCE','https://example.test/movie.mp4')
    assert not config.captures('DOCUMENT','https://example.test/notes.docx')
    assert config.captures('DOCUMENT','https://example.test/unknown-download')
    documents=AuditConfig(target_url='https://example.test/', capture_types={'DOCUMENT'})
    assert not documents.captures('DOCUMENT','https://example.test/unknown-download')

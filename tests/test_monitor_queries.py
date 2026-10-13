"""Dashboard correctness and scale fixtures; never production/network audits."""
import asyncio
import csv
import io
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from crawler_coverage_auditor.config import AuditConfig, capture_category, resource_subtype
from crawler_coverage_auditor.discovery.resources import resource_type
from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.services import queries
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.validation.network_safety import validate_target, check_public_request
from crawler_coverage_auditor.web.api import create_app
from test_react_api import HEADERS, Jobs, make_inventory


def test_metrics_do_not_conflate_verified_coverage_and_completion():
    assert queries.metrics(10, 4, 1) == {'eligible': 10, 'checked': 5, 'found': 4, 'missing': 1, 'unverified': 5,
        'verification_completion': 50, 'verified_coverage': 80, 'coverage_lower_bound': 40, 'coverage_upper_bound': 90}
    assert queries.metrics(10, 0, 0)['verified_coverage'] is None
    assert queries.metrics(0, 0, 0)['verification_completion'] is None
    assert queries.metrics(10, 0, 10)['verified_coverage'] == 0


def test_latest_target_sites_are_not_history_or_resource_hosts(tmp_path):
    path = tmp_path/'audit.sqlite'
    make_inventory(path)
    db = AuditDatabase(path)
    latest = db.create_run('http://www.example.test/archive', 'https://api.test', {})
    other = db.create_run('https://second.test', 'https://api.test', {})
    url = 'https://cdn.external.test/image.png'
    db.add_url(latest, DiscoveredURL(url, url, 'STATIC', 1, page_type='IMAGE'))
    db.add_http_result(latest, url, validity='URL_ONLY')
    db.close()
    items = queries.sites(path)
    assert {item['site'] for item in items} == {'example.test', 'second.test'}
    assert {item['run']['id'] for item in items} == {latest, other}
    assert next(item for item in items if item['site']=='example.test')['summary']['counts']['HTML'] == 0


def test_paginated_filters_details_and_full_filtered_export(tmp_path):
    path=tmp_path/'audit.sqlite'; run=make_inventory(path)
    client=TestClient(create_app(path,tmp_path/'reports',jobs=Jobs()))
    page=client.get(f'/api/runs/{run}/urls?page_size=2').json()
    assert page['total']==5 and len(page['items'])==2 and page['pages']==3
    assert client.get(f'/api/runs/{run}/urls?page=3&page_size=2').json()['items'][0]['url'] not in {item['url'] for item in page['items']}
    selected=client.get(f'/api/runs/{run}/urls?type=HTML&host=example.test&corpus_status=missing&search=missing&method=STATIC&depth=1&sort=depth&order=desc&http_status=200').json()
    assert selected['total']==1
    assert selected['host_summary']['html']==2 and selected['host_summary']['files']==2
    assert selected['host_summary']['discovered']==5
    detail=client.get(f"/api/runs/{run}/urls/{selected['items'][0]['id']}").json()
    assert detail['detail']['Normalized URL'].endswith('/missing')
    assert detail['text_label']=='Not assessed'
    exported=list(csv.reader(io.StringIO(client.get(f'/api/runs/{run}/urls/export?corpus_status=missing').text.lstrip('\ufeff'))))
    assert len(exported)==3 # two matches, independent of page size
    assert 'pdf' in exported[2][0] or 'pdf' in exported[1][0]
    assert client.get(f'/api/runs/{run}/urls?search=%25').json()['total']==0 # literal %, no wildcard
    assert client.get(f'/api/runs/{run}/urls?page_size=9999').status_code==422


def test_extraction_health_and_incremental_activity_have_real_evidence(tmp_path):
    path=tmp_path/'audit.sqlite'; run=make_inventory(path)
    db=AuditDatabase(path)
    db.connection.execute("UPDATE http_results SET text_length=100,fingerprint='shared' WHERE validity='VALID_PAGE'")
    db.event(run,'Website','DISCOVERED','A URL',url='https://example.test/',depth=0, cookie='secret-marker')
    db.event(run,'Website','LIMITED','HTTP 429',severity='WARNING')
    db.connection.commit(); db.close()
    summary=queries.summary(path,run)
    assert summary['health']['with_text']==2 and summary['health']['extraction_rate']==100
    assert summary['health']['median_characters']==100
    assert summary['health']['duplicate_fingerprint_groups']==1
    first=queries.activity(path,run,limit=1)
    assert first['items'][0]['next_step']
    assert queries.activity(path,run,after_id=first['cursor'])['items']==[]
    events=queries.activity(path,run)
    assert 'secret-marker' not in json.dumps(events)
    db=AuditDatabase(path); db.event(run,'Corpus API','FOUND','Confirmed URL'); db.close()
    assert len(queries.activity(path,run,after_id=first['cursor'])['items'])==1
    assert len(queries.activity(path,run,severity='WARNING')['items'])==1


@pytest.mark.parametrize('url,mime,kind,subtype',[
    ('https://example.test/a.pdf','text/html','HTML','HTML'),
    ('https://example.test/a.html','application/pdf','DOCUMENT','PDF'),
    ('https://example.test/a.pdf','image/png','IMAGE','Image'),
    ('https://example.test/api','application/json','API','JSON API'),
    ('https://example.test/feed','application/atom+xml','FEED','Atom'),
    ('https://example.test/video','video/mp4','RESOURCE','Video'),
    ('https://example.test/a.docx','','DOCUMENT','Word'),
    ('https://example.test/a.csv','','DOCUMENT','CSV'),
    ('https://example.test/unknown','application/octet-stream','DOCUMENT','Unknown binary'),
])
def test_mime_precedence_and_resource_subtypes(url,mime,kind,subtype):
    assert resource_type(url,mime)==kind
    assert resource_subtype(kind,url,mime)==subtype
    if subtype=='PDF': assert capture_category(kind,url,mime)=='PDF'


def test_controls_are_validated_persisted_and_passed_to_worker(tmp_path):
    class Recording(Jobs):
        def start(self,*args,settings=None,**kwargs):
            self.settings=settings
            return super().start(*args,settings=settings,**kwargs)
    jobs=Recording(); path=tmp_path/'audit.sqlite'
    client=TestClient(create_app(path,tmp_path/'reports',jobs=jobs))
    settings=client.get('/api/state').json()['settings']
    settings.update(capture_types=['HTML','API'],discovery_sources=['STATIC'],max_urls=500,max_concurrency=1,website_delay_seconds=1.5,corpus_batch_size=1)
    assert client.put('/api/settings',headers=HEADERS,json=settings).status_code==200
    restored=TestClient(create_app(path,tmp_path/'reports',jobs=jobs)).get('/api/state').json()['settings']
    assert restored['discovery_sources']==['STATIC']
    assert client.post('/api/discover',headers=HEADERS,json={'target_url':'https://example.test/'}).status_code==202
    assert 'max_urls' not in jobs.settings and jobs.settings['website_delay_seconds']==1.5
    assert jobs.settings['corpus_batch_size']==1 and jobs.settings['capture_types']==['API','HTML']
    jobs.job=None
    for invalid in ({'max_concurrency':2},{'corpus_batch_size':10},{'corpus_interval_seconds':0},{'website_delay_seconds':0},{'max_retries':5},{'discovery_sources':['STEALTH']}):
        assert client.put('/api/settings',headers=HEADERS,json=settings|invalid).status_code==422


def test_capture_only_legacy_preferences_are_upgraded_without_losing_selection(tmp_path):
    (tmp_path/'capture_settings.json').write_text(json.dumps({'capture_types':['OTHER','PDF']}))
    settings=TestClient(create_app(tmp_path/'audit.sqlite',tmp_path/'reports')).get('/api/state').json()['settings']
    assert set(settings['capture_types'])=={'OTHER','PDF','API','FEED','MEDIA'}
    assert settings['include_subdomains'] is True


def test_source_controls_actually_gate_discovery_and_external_resources(tmp_path):
    config=AuditConfig(target_url='https://example.test/', database_path=tmp_path/'audit.sqlite',
        discovery_sources={'STATIC'},external_resources=False)
    auditor=CoverageAuditor(config)
    try:
        assert not auditor._add_discovery('https://example.test/sitemap-only','SITEMAP',1,None,'')
        assert not auditor._add_discovery('https://cdn.test/a.pdf','STATIC',1,None,'')
        assert auditor._add_discovery('https://example.test/deep','STATIC',12,None,'')
        auditor._analyze_static_filters('<select name="year"><option value="2020">2020</option></select>',config.target_url,0)
        assert auditor.db.filter_rows(auditor.run_id)==[]
        asyncio.run(auditor._analyze_pagination(None,'<a href="?page=2">Next</a>',config.target_url,0))
        assert auditor.db.pagination_rows(auditor.run_id)==[]
    finally: auditor.db.close()


def test_old_schema_read_then_additive_upgrade_preserves_data(tmp_path):
    from test_dashboard_data import make_database
    path=tmp_path/'old.sqlite';make_database(path)
    before=queries.summary(path,1)
    assert before['counts']['HTML']==1
    assert queries.url_page(path,1)['items'][0]['navigation_depth'] is None
    db=AuditDatabase(path)
    assert db.connection.execute('SELECT COUNT(*) FROM urls').fetchone()[0]==1
    assert db.connection.execute('SELECT first_discovery_method FROM urls').fetchone()[0] is None
    assert db.connection.execute('SELECT host FROM urls').fetchone()[0]=='source.example'
    assert len(db.connection.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%run%'").fetchall())>=6
    db.close()


@pytest.mark.parametrize('url',['http://127.0.0.1/','http://[::1]/','http://169.254.169.254/latest/meta-data/','http://10.0.0.1/','http://localhost/','https://user:password@example.test/'])
def test_private_network_and_embedded_credentials_are_blocked(monkeypatch,url):
    monkeypatch.delenv('CRAWLSCOPE_ALLOW_PRIVATE_TARGETS')
    with pytest.raises(ValueError): validate_target(url)


def test_dns_public_name_cannot_resolve_to_metadata(monkeypatch):
    monkeypatch.delenv('CRAWLSCOPE_ALLOW_PRIVATE_TARGETS')
    monkeypatch.setattr('socket.getaddrinfo',lambda *args,**kwargs:[(2,1,6,'',('169.254.169.254',80))])
    with pytest.raises(ValueError): asyncio.run(check_public_request('https://apparently-public.example/'))


def test_blocked_or_repeated_pagination_cannot_retain_verified_maximum(tmp_path):
    path=tmp_path/'audit.sqlite';db=AuditDatabase(path); run=db.create_run('https://example.test/','https://api.test',{})
    db.upsert_pagination_series(run,'key','https://example.test/news?page=2','page','https://example.test/',2,max_page=197,status='VERIFIED')
    db.upsert_pagination_series(run,'key','https://example.test/news?page=2','page','https://example.test/',128,status='LIMITED',evidence='Boundary blocked at 128')
    row=dict(db.pagination_rows(run)[0]);db.close()
    assert row['max_page'] is None and row['observed_max']==128


def large_inventory(path):
    db=AuditDatabase(path)
    config={'capture_types':['HTML','PDF','DOCUMENT','IMAGE','API','FEED','MEDIA','OTHER']}
    for _ in range(2):
        old=db.create_run('https://scale.test/','https://api.test/',config)
        url='https://scale.test/old'
        db.add_url(old,DiscoveredURL(url,url,'STATIC',1))
        db.add_http_result(old,url,status_code=200,validity='VALID_PAGE',text_length=120)
    run=db.create_run('https://scale.test/','https://api.test/',config)
    rows=[]; responses=[]; matches=[]
    for index in range(20000):
        kinds=[('HTML',''),('HTML',''),('DOCUMENT','.pdf'),('DOCUMENT','.docx'),('IMAGE','.png'),('API','.json'),('FEED','.rss'),('RESOURCE','.mp4')]
        kind,suffix=kinds[index%8];url=f'https://host{index%10}.scale.test/page/{index}{suffix}'
        rows.append((run,url,url,kind,index%15,index%15,'["STATIC","SITEMAP"]','STATIC',f'host{index%10}.scale.test'))
        valid='VALID_PAGE' if kind in {'HTML','API','FEED'} else 'URL_ONLY'
        if index%37==0: valid='HTTP_ERROR'
        responses.append((run,url,200 if valid=='VALID_PAGE' else None,valid,100+index if kind=='HTML' else None,'2026-10-08T00:00:00+00:00'))
        if index%3:
            matches.append((run,url,'EXACT_MATCH' if index%5 else 'NOT_FOUND','2026-10-08T00:01:00+00:00'))
    db.connection.executemany('INSERT INTO urls(audit_run_id,url,normalized_url,page_type,min_depth,navigation_depth,discovery_methods,first_discovery_method,host) VALUES(?,?,?,?,?,?,?,?,?)',rows)
    db.connection.executemany('INSERT INTO http_results(audit_run_id,normalized_url,status_code,validity,text_length,fetched_at) VALUES(?,?,?,?,?,?)',responses)
    db.connection.executemany('INSERT INTO corpus_matches(audit_run_id,normalized_url,match_status,checked_at) VALUES(?,?,?,?)',matches)
    db.connection.executemany("INSERT INTO audit_events(audit_run_id,stage,kind,severity,message,created_at) VALUES(?,'Website','DISCOVERED','INFO',?,'2026-10-08T00:00:00+00:00')",[(run,f'Fixture event {i}') for i in range(5000)])
    for index in range(3): db.create_run(f'https://extra{index}.test/','https://api.test/',{})
    db.connection.commit();db.close();return run


def test_20000_url_queries_have_bounded_payloads_and_lazy_analysis(tmp_path):
    path=tmp_path/'scale.sqlite';run=large_inventory(path)
    client=TestClient(create_app(path,tmp_path/'reports',jobs=Jobs()))
    measurements={}
    for endpoint in ('state','sites',f'runs/{run}/summary',f'runs/{run}/urls',f'runs/{run}/urls?host=host7.scale.test&search=page%2F17',f'runs/{run}/activity',f'runs/{run}/analysis'):
        started=time.perf_counter(); response=client.get('/api/'+endpoint); elapsed=time.perf_counter()-started
        assert response.status_code==200
        measurements[endpoint]={'milliseconds':round(elapsed*1000,2),'bytes':len(response.content)}
        data=response.json()
        if endpoint.endswith('/summary'):
            assert data['metrics']['discovered']==20000 and 'rows' not in data
            assert len(response.content)<20000
        if endpoint.endswith('/urls'):
            assert len(data['items'])==50 and data['total']==20000 and data['pages']==400
        if '?host=' in endpoint:
            assert len(data['items'])<=50 and data['total']<20000
            assert all(item['host']=='host7.scale.test' and '/page/17' in item['url'] for item in data['items'])
        if endpoint.endswith('/activity'): assert len(data['items'])==80
        if endpoint.endswith('/analysis'): assert 'details' not in data
        assert elapsed<10 # generous local regression ceiling, not a production SLA
    print('PERFORMANCE_FIXTURE',json.dumps(measurements))


def test_historical_notes_do_not_inflate_current_boundaries(tmp_path):
    path=tmp_path/'audit.sqlite';run=make_inventory(path);db=AuditDatabase(path)
    for stage,url,reason in [('Document text','https://example.test/old.pdf','Old extraction failure'),
        ('Website','https://example.test/','JavaScript error: optional script'),
        ('Website','https://example.test/blocked','HTTP 403'),
        ('Corpus API','https://example.test/','HTTP 503')]:
        db.limit(run,stage,url,reason,None)
    db.close()
    s=queries.summary(path,run)
    assert s['notes_count']==2 and s['counters']['boundaries']==2 and len(s['boundaries'])==2
    assert s['health']['extraction_rate'] is None
    assert s['counters']['queued']==0


def test_non_monotonic_pagination_stays_unknown(tmp_path):
    config=AuditConfig(target_url='https://example.test/',database_path=tmp_path/'audit.sqlite')
    auditor=CoverageAuditor(config)
    class Website:
        async def get(self,url):
            from urllib.parse import parse_qs,urlsplit
            page=int(parse_qs(urlsplit(url).query).get('page',['1'])[0])
            status=404 if page==3 else 200
            return SimpleNamespace(status_code=status,text=f'<h1>Unique page {page}</h1><article>Article {page}</article>',error=None,headers={},final_url=url)
    try:
        asyncio.run(auditor._analyze_pagination(Website(),'<a href="/news?page=2">Next</a>',config.target_url,0))
        row=dict(auditor.db.pagination_rows(auditor.run_id)[0])
        assert row['status']=='LIMITED' and row['max_page'] is None
        assert 'Non-monotonic' in row['evidence']
    finally: auditor.db.close()


def test_history_is_not_comparable_when_discovery_limits_change(tmp_path):
    path=tmp_path/'audit.sqlite';run=make_inventory(path);db=AuditDatabase(path)
    db.create_run('https://example.test/','https://corpus.test/',{'max_urls':12345})
    db.close()
    assert [item['Run'] for item in queries.analysis(path,run)['history']]==[run]


def test_redirect_to_private_network_is_blocked_before_transport(tmp_path,monkeypatch):
    import httpx
    from crawler_coverage_auditor.validation.http import PoliteHttpClient
    monkeypatch.delenv('CRAWLSCOPE_ALLOW_PRIVATE_TARGETS')
    monkeypatch.setattr('socket.getaddrinfo',lambda *args,**kwargs:[(2,1,6,'',('93.184.216.34',443))])
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302,headers={'Location':'http://169.254.169.254/latest/meta-data/'})
    async def exercise():
        async with PoliteHttpClient(1,5,0,'test') as client:
            client._client._transport=httpx.MockTransport(handler)
            result=await client.get('https://public.test/')
            assert result.error and 'blocked' in result.error
    asyncio.run(exercise())
    assert calls==['https://public.test/']

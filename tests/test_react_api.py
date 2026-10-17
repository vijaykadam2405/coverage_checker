import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient

from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.web.api import create_app

HEADERS={'X-CrawlScope-Client':'1'}


class Jobs:
    def __init__(self):
        self.job=None
        self.calls=[]
    def active(self): return self.job
    def start(self, target, database, reports, corpus, *, capture_types, settings=None):
        self.calls.append(('discovery', target, capture_types))
        self.job=SimpleNamespace(run_id=1, action='discovery', target=target, error=None, stop_event=threading.Event())
        return self.job
    def start_corpus(self, run_id, database, reports):
        self.calls.append(('corpus',run_id))
        return SimpleNamespace(run_id=run_id, action='corpus', error=None)


def test_preferences_actions_and_no_automatic_corpus_request(tmp_path):
    jobs=Jobs()
    client=TestClient(create_app(tmp_path/'audit.sqlite',tmp_path/'reports',jobs=jobs))
    assert client.get('/api/state').json()['settings']['capture_types']==['HTML','PDF','DOCUMENT']
    assert client.get('/api/state').json()['runs']==[]
    assert jobs.calls==[]
    assert client.put('/api/settings',json={'capture_types':['PDF']}).status_code==403
    assert client.put('/api/settings',headers=HEADERS,json={'capture_types':[]}).status_code==422
    assert client.put('/api/settings',headers=HEADERS,json={'capture_types':['PDF']}).status_code==200
    assert client.get('/api/state').json()['settings']['capture_types']==['PDF']
    assert client.post('/api/discover',headers=HEADERS,json={'target_url':'https://example.test/','capture_types':['PDF']}).status_code==202
    assert jobs.calls==[('discovery','https://example.test/',{'PDF'})]
    assert client.put('/api/settings',headers=HEADERS,json={'capture_types':['HTML']}).status_code==409
    assert client.post('/api/runs/1/stop',headers=HEADERS,json={}).status_code==202
    assert jobs.job.stop_event.is_set()
    assert client.post('/api/runs/2/stop',headers=HEADERS,json={}).status_code==409
    assert client.post('/api/discover',headers=HEADERS|{'Origin':'https://outside.example'},json={'target_url':'https://example.test/','capture_types':['HTML']}).status_code==403


def make_inventory(path):
    db=AuditDatabase(path)
    run=db.create_run('https://example.test/','https://private-corpus.test/',{})
    for name,kind,validity,status,capture in [('/','HTML','VALID_PAGE','EXACT_MATCH',True),('/missing','HTML','VALID_PAGE','NOT_FOUND',True),('/broken','HTML','HTTP_ERROR',None,True),('/report.pdf','DOCUMENT','URL_ONLY','NOT_FOUND',True),('/photo.jpg','IMAGE','URL_ONLY',None,True),('/traversal','HTML','VALID_PAGE',None,False)]:
        url='https://example.test'+name
        db.add_url(run,DiscoveredURL(url,url,'STATIC',1,page_type=kind,capture_enabled=capture))
        db.add_http_result(run,url,status_code=200 if kind=='HTML' else None,validity=validity)
        if status:
            db.connection.execute('INSERT INTO corpus_matches(audit_run_id,normalized_url,match_status,checked_at) VALUES(?,?,?,?)',(run,url,status,'2026-10-07T00:00:00+00:00'))
            db.connection.commit()
    db.inventory_ready(run)
    db.close()
    return run


def test_snapshot_disjoint_counts_and_html_only_coverage(tmp_path):
    path=tmp_path/'audit.sqlite';run=make_inventory(path);jobs=Jobs()
    client=TestClient(create_app(path,tmp_path/'reports',jobs=jobs))
    data=client.get(f'/api/runs/{run}').json()
    assert data['counts']=={'HTML':2,'PDF':1,'DOCUMENT':0,'IMAGE':1,'OTHER':0,'MEDIA':0,'API':0,'FEED':0}
    assert data['total']==4
    assert data['html_coverage']['verified_coverage']==50
    assert 'rows' not in data # lightweight polling, traversal excluded
    assert client.get(f'/api/runs/{run}/urls').json()['total']==5
    assert 'corpus_url' not in data['run']
    assert jobs.calls==[]
    assert client.post(f'/api/runs/{run}/verify',headers=HEADERS,json={}).status_code==202
    assert jobs.calls==[('corpus',run)]
    assert client.get('/api/runs/999').status_code==404
    assert client.get('/api/runs/1/reports/html').status_code==404
    assert client.get('/api/unknown').status_code==404


def test_preferences_persist_after_service_restart(tmp_path):
    path=tmp_path/'audit.sqlite'
    one=TestClient(create_app(path,tmp_path/'reports'))
    one.put('/api/settings',headers=HEADERS,json={'capture_types':['IMAGE','PDF']})
    two=TestClient(create_app(path,tmp_path/'reports'))
    assert two.get('/api/state').json()['settings']['capture_types']==['IMAGE','PDF']


def test_invalid_discovery_and_external_host_are_rejected(tmp_path):
    client=TestClient(create_app(tmp_path/'audit.sqlite',tmp_path/'reports'))
    assert client.post('/api/discover',headers=HEADERS,json={'target_url':'file:///local','capture_types':['HTML']}).status_code==422
    assert client.get('/api/state',headers={'Host':'evil.example'}).status_code==400

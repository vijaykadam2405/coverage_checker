"""Real browser QA, isolated fixtures; no website/Corpus actions or secret reads."""
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from test_react_api import Jobs, make_inventory

from crawler_coverage_auditor.web.api import create_app


def test_control_room_key_entry_is_masked_saved_and_not_returned(tmp_path, monkeypatch):
    import os
    if os.name != 'nt':
        pytest.skip('Protected desktop key entry requires Windows DPAPI')
    monkeypatch.delenv('CRAWLSCOPE_CORPUS_API_KEY', raising=False)
    root = Path(__file__).resolve().parents[1]
    frontend = root/'frontend'/'dist'
    if not (frontend/'index.html').is_file():
        pytest.skip('Build frontend before browser QA')
    database = tmp_path/'audit.sqlite'
    make_inventory(database)
    jobs = Jobs()
    app = create_app(database,tmp_path/'reports',frontend=frontend,jobs=jobs)
    sock = socket.socket()
    sock.bind(('127.0.0.1',0))
    address = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread = threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width':1600,'height':1000})
            page.goto(address)
            page.get_by_role('button',name='Control room',exact=True).click()
            expect(page.get_by_role('heading',name='Corpus API connection')).to_be_visible()
            field = page.locator('#corpus-api-key')
            expect(field).to_have_attribute('type','password')
            key = 'test-only-browser-credential'
            field.fill(key)
            page.get_by_role('button',name='Save API key',exact=True).click()
            expect(field).to_have_value('')
            expect(page.get_by_role('status')).to_contain_text('API key saved securely')
            assert key not in httpx.get(address+'/api/state').text
            assert key not in page.evaluate('JSON.stringify(localStorage)')
            # Polling must not undo capture edits that have not been submitted.
            page.get_by_role('switch',name='Capture Images',exact=True).check()
            page.wait_for_timeout(5500)
            expect(page.get_by_role('switch',name='Capture Images',exact=True)).to_be_checked()
            for width in (1600,820,390):
                page.set_viewport_size({'width':width,'height':1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                output=tmp_path/'ui-review'
                output.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(output/f'{width}-control-room-key.png'),full_page=True)
            assert jobs.calls == []
            browser.close()
    finally:
        server.should_exit=True
        thread.join(timeout=5)
        sock.close()


def test_react_inventory_controls_and_responsive_layout(tmp_path):
    root=Path(__file__).resolve().parents[1];frontend=root/'frontend'/'dist'
    if not (frontend/'index.html').is_file(): pytest.skip('Build frontend before browser QA')
    database=tmp_path/'audit.sqlite';make_inventory(database);jobs=Jobs()
    from crawler_coverage_auditor.storage.database import AuditDatabase
    db=AuditDatabase(database)
    db.connection.execute("UPDATE http_results SET text_length=100 WHERE normalized_url='https://example.test/'")
    db.connection.execute("UPDATE corpus_matches SET similarity=.75,corpus_text_length=80,text_status='FOUND_PARTIAL_TEXT' WHERE normalized_url='https://example.test/'")
    db.connection.commit();db.close()
    app=create_app(database,tmp_path/'reports',frontend=frontend,jobs=jobs)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    address=f'http://127.0.0.1:{port}'
    output=tmp_path/'ui-review';output.mkdir(parents=True,exist_ok=True)
    try:
        for _ in range(100):
            if server.started: break
            time.sleep(.05)
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch();page=browser.new_page(viewport={'width':1600,'height':1000})
            errors=[];reads=[];page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('request',lambda request:reads.append(request.url))
            page.goto(address)
            expect(page.get_by_role('heading',name='Corpus coverage analysis')).to_be_visible()
            expect(page.locator('.summary-item').filter(has_text='HTML pages found')).to_contain_text('2')
            expect(page.locator('.summary-item').filter(has_text='PDFs found')).to_contain_text('1')
            assert page.locator('.summary-strip').bounding_box()['y'] < page.get_by_role('heading',name='Overview',exact=True).bounding_box()['y']
            assert page.get_by_role('heading',name='Live HTML extraction health').count()==0
            assert page.locator('#saved-run').count()==0
            expect(page.locator('.live-url-table thead')).to_contain_text('Text coverage')
            expect(page.locator('.live-url-table thead')).to_contain_text('Period')
            expect(page.locator('.live-url-table tbody')).to_contain_text('75.0%')
            text_bar=page.locator('.live-url-table').get_by_role('progressbar',name='HTML text coverage')
            expect(text_bar).to_have_attribute('aria-valuenow','75')
            expect(text_bar).to_have_attribute('aria-valuetext','75.0% text similarity')
            expect(page.locator('.live-url-table .text-coverage-unavailable').first).to_have_text('—')
            assert page.locator('.live-url-table .cell-note').count()==0
            expect(page.locator('.donut strong')).to_have_text('50.0%')
            # The new text API has real scores, not the old blanket "unavailable" label.
            db=AuditDatabase(database)
            evidence={'text_comparison': {'method':'unique_word_coverage', 'segments': {'beginning':100,'middle':80,'ending':45}},
                      'text_api': {'version_id':'test-v1','last_fetched_at':'2026-10-10T00:00:00Z'}}
            db.connection.execute("UPDATE corpus_matches SET provider='coverage_api:https://api.test',evidence_json=? WHERE normalized_url='https://example.test/'", (json.dumps(evidence),))
            db.add_corpus_match(1,'https://example.test/photo.jpg','NOT_TRACKED',None,None,None,None,'URL_ONLY',provider='coverage_api:https://api.test')
            db.connection.commit();db.close()
            page.reload()
            expect(page.locator('.live-url-table').get_by_role('progressbar',name='HTML text coverage')).to_have_attribute('aria-valuetext','75.0% estimated word coverage')
            expect(page.locator('.live-url-table tbody')).to_contain_text('Not tracked by corpus')
            page.locator('.live-url-table').get_by_role('button',name='https://example.test/',exact=True).click()
            expect(page.get_by_role('dialog')).to_contain_text('Saved word coverage %')
            expect(page.get_by_role('dialog')).to_contain_text('Ending word coverage %')
            expect(page.get_by_role('dialog')).to_contain_text('test-v1')
            assert 'unavailable through this API' not in page.get_by_role('dialog').inner_text()
            page.keyboard.press('Escape')
            # Restore the legacy fixture for the existing responsive/progress checks.
            db=AuditDatabase(database)
            db.connection.execute("UPDATE corpus_matches SET provider=NULL,evidence_json=NULL WHERE normalized_url='https://example.test/'")
            db.connection.execute("DELETE FROM corpus_matches WHERE normalized_url='https://example.test/photo.jpg'")
            db.connection.commit();db.close();page.reload()
            page.get_by_role('button',name='Capture settings').click()
            expect(page.get_by_role('heading',name='Capture control room')).to_be_visible()
            assert page.get_by_role('dialog').count()==0
            page.get_by_role('switch',name='Capture Images',exact=True).check()
            assert page.get_by_role('tab').count()==0
            assert page.get_by_role('spinbutton').count()==0
            assert page.get_by_role('switch').count()==5
            expect(page.get_by_role('heading',name='Corpus API connection')).to_be_visible()
            expect(page.locator('#corpus-api-key')).to_have_attribute('type','password')
            expect(page.locator('#corpus-api-key')).to_have_value('')
            expect(page.get_by_role('button',name='Save API key')).to_be_disabled()
            page.get_by_role('button',name='Save capture choices').click()
            expect(page.get_by_role('status')).to_have_text('Capture choices saved for your next scan.')
            saved=httpx.get(address+'/api/state').json()['settings']
            assert saved['capture_types']==['DOCUMENT','HTML','IMAGE','PDF'] and saved['max_concurrency']==1
            assert 'EXTERNAL_INDEX' in saved['discovery_sources'] and saved['use_browser']
            page.reload()
            expect(page.get_by_role('button',name='Capture settings')).to_contain_text('4')
            page.get_by_role('button',name='URL inventory',exact=True).click()
            expect(page.locator('.url-table tbody tr')).to_have_count(5) # all types, including failure
            assert page.get_by_role('button',name='Start website scan').count()==0
            assert page.locator('.inventory-types').count()==0
            page.get_by_role('textbox',name='Search saved websites').fill('no-matching-site')
            expect(page.locator('.website-list tbody tr')).to_have_count(0)
            page.get_by_role('textbox',name='Search saved websites').fill('example.test')
            expect(page.locator('.website-list tbody tr')).to_have_count(1)
            page.locator('.website-link').click()
            expect(page.locator('.url-table tbody tr')).to_have_count(5)
            page.get_by_role('button',name='Missing from corpus',exact=True).click()
            expect(page.locator('.url-table tbody tr')).to_have_count(2)
            expect(page.locator('.url-table tbody')).to_contain_text('/missing')
            page.get_by_role('textbox',name='Search URLs',exact=True).fill('report.pdf')
            expect(page.locator('.url-table tbody tr')).to_have_count(1)
            expect(page.locator('.url-table tbody')).to_contain_text('report.pdf')
            expect(page.locator('.url-table .url-address')).to_have_count(1)
            assert page.locator('.url-table .url-title').count()==0
            assert page.locator('.url-table .url-button').inner_text()=='https://example.test/report.pdf'
            page.get_by_role('button',name='Inspect https://example.test/report.pdf').click()
            expect(page.get_by_role('dialog')).to_contain_text('No file download')
            page.keyboard.press('Escape')
            expect(page.get_by_role('dialog')).not_to_be_visible()
            expect(page.get_by_role('button',name='Inspect https://example.test/report.pdf')).to_be_focused()
            with page.expect_download() as download: page.get_by_role('link',name='Export CSV').click()
            assert download.value.suggested_filename=='crawlscope-1-all.csv'
            # Exact breakpoint coverage for every important view.
            page.set_viewport_size({'width':390,'height':844})
            page.get_by_role('button',name='Open navigation').click()
            expect(page.get_by_role('link',name='CrawlScope home')).to_be_focused()
            page.keyboard.press('Escape')
            expect(page.get_by_role('button',name='Open navigation')).to_be_focused()
            for width in [1600,1366,820,390]:
                page.set_viewport_size({'width':width,'height':1000 if width>800 else 844})
                for view in ['Overview','URL inventory','Analysis','Activity','Control room']:
                    if width<=800: page.get_by_role('button',name='Open navigation').click()
                    page.get_by_role('button',name=view,exact=True).click()
                    if view=='Overview': expect(page.get_by_role('heading',name='Corpus coverage analysis')).to_be_visible()
                    elif view=='URL inventory': expect(page.get_by_role('heading',name='Discovered inventory')).to_be_visible()
                    elif view=='Analysis':
                        expect(page.get_by_role('heading',name='First discovery source')).to_be_visible()
                        expect(page.get_by_role('heading',name='Coverage by area',exact=True)).to_be_visible()
                        expect(page.locator('.analysis-disclosure').filter(has_text='Pagination coverage and missing ranges')).to_contain_text('0 URLs')
                        expect(page.locator('.analysis-disclosure').filter(has_text='Filter coverage')).to_contain_text('N/A')
                        expect(page.locator('.analysis-disclosure').filter(has_text='Archive coverage by year')).to_contain_text('N/A')
                    elif view=='Activity': expect(page.get_by_role('heading',name='Live activity')).to_be_visible()
                    else: expect(page.get_by_role('heading',name='Capture control room')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), (width,view)
                    page.screenshot(path=str(output/f'{width}-{view.lower().replace(" ","-")}.png'),full_page=True,animations='disabled')
            assert not any('/api/runs/1?' in url or url.endswith('/api/runs/1') for url in reads)
            assert any('/summary' in url for url in reads) and any('/urls?' in url for url in reads)
            assert errors==[] and jobs.calls==[]
            # Read-only active-state fixtures demonstrate honest progress and locking.
            from types import SimpleNamespace

            from crawler_coverage_auditor.storage.database import AuditDatabase
            db=AuditDatabase(database)
            db.connection.execute("UPDATE audit_runs SET status='RUNNING',discovery_completed_at=NULL WHERE id=1")
            db.connection.commit();db.close()
            jobs.job=SimpleNamespace(run_id=1,action='discovery',target='https://example.test/',stop_event=threading.Event())
            page.set_viewport_size({'width':1600,'height':1000});page.reload()
            expect(page.get_by_role('heading',name='Scan progress')).to_be_visible()
            assert page.get_by_role('progressbar',name='Scan progress').get_attribute('aria-valuenow') is None
            assert page.get_by_role('button',name='Check corpus',exact=True).count()==0
            page.get_by_role('button',name='Capture settings').click()
            expect(page.get_by_role('switch',name='Capture HTML pages')).to_be_disabled()
            jobs.job=SimpleNamespace(run_id=1,action='corpus',target='https://example.test/',stop_event=threading.Event())
            page.reload()
            assert page.get_by_role('progressbar',name='Scan progress').get_attribute('aria-valuenow') is None
            jobs.job=None
            # A large saved website list remains searchable and opens the correct inventory.
            db=AuditDatabase(database)
            for index in range(105):
                db.create_run(f'https://saved-{index:03}.test/','https://api.test/',{})
            db.close()
            page.reload()
            page.get_by_role('button',name='URL inventory',exact=True).click()
            expect(page.locator('.website-list tbody tr')).to_have_count(106,timeout=15000)
            page.get_by_role('textbox',name='Search saved websites').fill('saved-042.test')
            expect(page.locator('.website-list tbody tr')).to_have_count(1)
            page.locator('.website-link').click()
            expect(page.locator('.summary-context')).to_contain_text('saved-042.test')
            expect(page.locator('.url-table tbody tr')).to_have_count(0)
            empty_state=httpx.get(address+'/api/state').json()
            empty_state.update(runs=[],active=None)
            page.route('**/api/state',lambda route:route.fulfill(json=empty_state))
            page.route('**/api/sites',lambda route:route.fulfill(json={'items':[]}))
            page.reload()
            expect(page.locator('.summary-context')).to_contain_text('No website selected')
            expect(page.locator('.summary-item strong')).to_have_text(['0']*6)
            page.screenshot(path=str(output/'empty-overview.png'),full_page=True,animations='disabled')
            assert jobs.calls==[] and errors==[]
            browser.close()
    finally:
        server.should_exit=True;thread.join(timeout=5);sock.close()

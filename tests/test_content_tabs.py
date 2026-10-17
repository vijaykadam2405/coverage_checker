import asyncio
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.discovery.browser import BrowserExplorer
from crawler_coverage_auditor.discovery.filters import detect_filters
from crawler_coverage_auditor.discovery.tabs import (
    detect_tabs,
    merge_tab_content,
    same_document_url,
)
from crawler_coverage_auditor.validation.text_extract import extract_text

STATIC_HTML = '''<html><head><title>Products</title><style>body{min-height:3000px}.hidden{display:none}</style></head>
<body><main><h1>Products</h1><div role="tablist">
<a id="tab-a" role="tab" href="#pane-a">Product A</a>
<a id="tab-b" role="tab" href="#pane-b">Product B</a>
<a id="tab-c" role="tab" href="#pane-c">Product C</a>
<a id="tab-d" role="tab" href="#pane-d">Product D</a></div>
<div id="pane-a" role="tabpanel"><p>First product full text.</p><a href="/report.pdf">Report</a></div>
<div id="pane-b" class="hidden" role="tabpanel"><p>Second product full text.</p></div>
<div id="pane-c" class="hidden" role="tabpanel"><p>Third product full text.</p></div>
<div id="pane-d" class="hidden" role="tabpanel"><p>Fourth product full text.</p></div>
<form action="/contact" method="post"><input name="email" type="email"><select name="country"><option value="IN">India</option></select></form>
</main><script>let absentVideo;window.addEventListener('scroll',()=>absentVideo.top);setTimeout(()=>window.dispatchEvent(new Event('scroll')),10);
document.querySelectorAll('[role=tab]').forEach(t=>t.onclick=()=>{throw Error('Preloaded tab must not be clicked')});</script></body></html>'''

LAZY_HTML = '''<html><head><title>Lazy products</title></head><body><main>
<h1>Lazy products</h1><div role="tablist">
<button id="one" role="tab" aria-controls="existing">Product one</button>
<a id="two" role="tab" aria-controls="lazy" href="#lazy" onclick="loadProduct('two')">Product two</a>
<a id="three" role="tab" aria-controls="lazy" href="#lazy" onclick="loadProduct('three')">Product three</a></div>
<div id="existing" role="tabpanel"><p>Original product content stays captured.</p></div>
<div id="lazy" role="tabpanel">Loading...</div></main>
<script>async function loadProduct(name){let r=await fetch('/api/tab?value='+name);let d=await r.json();document.getElementById('lazy').innerHTML=d.html;}</script></body></html>'''


class TabWebsite(BaseHTTPRequestHandler):
    paths = []

    def log_message(self, *_):
        pass

    def do_GET(self):
        type(self).paths.append(self.path)
        mime, body, status = 'text/html', '', 200
        if self.path == '/':
            body = STATIC_HTML
        elif self.path == '/lazy':
            body = LAZY_HTML
        elif self.path == '/broken':
            body = '<main><h1>Readable page</h1><button id="failed" role="tab" aria-controls="empty" onclick="throw Error(\'Tab load failed\')">Lazy tab</button><div id="empty" role="tabpanel"></div></main>'
        elif self.path == '/empty':
            body = '<main></main><script>throw Error("Rendering failed")</script>'
        elif self.path == '/gallery':
            body = '''<main><h1>Gallery results</h1><button id="gallery_btn" onclick="expandGallery()">Show More</button></main>
            <script>function expandGallery(){document.getElementById('gallery_btn').remove();document.querySelector('main').insertAdjacentHTML('beforeend','<a href="/gallery-child">New HTML detail</a>');document.body.insertAdjacentHTML('beforeend',`<div class="fancybox-container" style="position:fixed;inset:0;background:#333;z-index:1000"><button class="fancybox-button--close" onclick="this.parentElement.remove()">Close</button></div>`);}</script>'''
        elif self.path == '/unstable':
            body = '<main><h1>Recovered navigation page</h1><a href="/child">Child HTML</a></main>'
        elif self.path.startswith('/api/tab?'):
            name = self.path.split('=')[-1]
            mime, body = 'application/json', json.dumps({'html': f'<p>Loaded product {name} complete text.</p><a href="/detail-{name}">Detail</a>'})
        elif self.path == '/robots.txt':
            mime, body = 'text/plain', 'User-agent: *\nAllow: /'
        else:
            status = 404
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.end_headers()
        self.wfile.write(body.encode())


@pytest.fixture
def tab_site():
    TabWebsite.paths = []
    server = ThreadingHTTPServer(('127.0.0.1', 0), TabWebsite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()


def test_preloaded_hidden_sections_are_content_not_filters():
    tabs = detect_tabs(STATIC_HTML, 'https://example.test/')
    assert len(tabs) == 4
    assert all(tab.kind == 'CONTENT' and tab.loaded for tab in tabs)
    text, _, _ = extract_text(STATIC_HTML)
    assert all(word in text for word in ['First', 'Second', 'Third', 'Fourth'])
    assert detect_filters(STATIC_HTML, 'https://example.test/') == []


def test_navigation_filter_and_lazy_tabs_remain_distinct():
    html = '''<div role="tablist" aria-label="Year filter"><button role="tab" aria-controls="results">2021</button></div>
    <div id="results">Current results</div><a role="tab" href="/another-page">Other page</a>
    <button role="tab" aria-controls="empty">Details</button><div id="empty">Loading...</div>'''
    tabs = detect_tabs(html, 'https://example.test/')
    assert tabs[0].kind == 'FILTER'
    assert tabs[1].kind == 'NAVIGATION' and tabs[1].link_url == 'https://example.test/another-page'
    assert tabs[2].kind == 'CONTENT' and not tabs[2].loaded


def test_gallery_overlay_is_closed_without_forcing_clicks_or_downloading_images(tab_site):
    async def exercise():
        events = []
        async with BrowserExplorer(actions=6, on_event=lambda kind, *args: events.append(kind)) as browser:
            if browser.error:
                pytest.skip(browser.error)
            result = await browser.explore(tab_site + 'gallery')
            assert not result.error
            assert not any('could not be activated' in item for item in result.limitations)
            assert 'GALLERY_CLOSED' in events
            assert any(link.url == tab_site + 'gallery-child' for link in result.links)
    asyncio.run(exercise())


def test_transient_navigation_failure_retries_then_preserves_html(tab_site):
    async def exercise():
        events, attempts = [], []
        async with BrowserExplorer(actions=3, on_event=lambda kind, *args: events.append(kind)) as browser:
            if browser.error:
                pytest.skip(browser.error)
            async def unstable(route):
                attempts.append(route.request.url)
                if len(attempts) == 1:
                    await route.abort('connectionreset')
                else:
                    await route.continue_()
            await browser.context.route('**/unstable', unstable)
            result = await browser.explore(tab_site + 'unstable')
            assert not result.error
            assert len(attempts) == 2
            assert 'BROWSER_RETRY' in events
            assert 'Recovered navigation page' in result.html
    asyncio.run(exercise())


def test_responsive_duplicate_tabs_count_as_one_section():
    html = '''<a role="tab" href="#pane" id="desktop">Product</a>
    <div id="pane"><div role="tab"><a href="#collapse">Product</a></div>
    <div id="collapse"><p>Full product information</p></div></div>'''
    assert len(detect_tabs(html, 'https://example.test/')) == 1


def test_lazy_html_merge_keeps_one_page_and_all_unique_sections():
    html = merge_tab_content('<main><p>First content</p></main>', ['<p>Second content</p>', '<p>Second content</p>', '<p>Third content</p>'])
    text, _, _ = extract_text(html)
    assert text == 'First content Second content Third content'


def test_section_hash_is_same_page_but_spa_route_is_not():
    assert same_document_url('https://example.test/page#pane', 'https://example.test/page')
    assert not same_document_url('https://example.test/page#/route', 'https://example.test/page')
    assert not same_document_url('https://example.test/page?year=2021', 'https://example.test/page?year=2026')
    tab = detect_tabs('<a role="tab" href="#/another-route">Route</a>', 'https://example.test/')[0]
    assert tab.kind == 'NAVIGATION'


def test_static_tabs_not_clicked_and_cosmetic_js_errors_are_diagnostics(tab_site):
    events = []
    async def exercise():
        async with BrowserExplorer(actions=10, on_event=lambda kind, message, url: events.append((kind, message, url))) as browser:
            return await browser.explore(tab_site)
    result = asyncio.run(exercise())
    assert not result.error
    assert result.states == ()
    assert not any(kind in {'FILTER_APPLYING', 'TAB_OPENING'} for kind, _, _ in events)
    assert any(kind == 'PAGE_SECTIONS' for kind, _, _ in events)
    assert all(url != 'about:blank' for _, _, url in events)
    assert TabWebsite.paths.count('/') == 1
    assert '/report.pdf' not in TabWebsite.paths
    assert result.script_errors and "reading 'top'" in result.script_errors[0]['message']
    assert not any('JavaScript error' in reason for reason in result.limitations)
    text, _, _ = extract_text(result.html)
    assert all(word in text for word in ['First', 'Second', 'Third', 'Fourth'])


def test_lazy_tabs_capture_all_html_text_without_filter_classification(tab_site):
    events = []
    async def exercise():
        async with BrowserExplorer(actions=20, on_event=lambda kind, message, url: events.append((kind, message, url))) as browser:
            return await browser.explore(tab_site+'lazy')
    result = asyncio.run(exercise())
    assert not result.error
    assert [state['kind'] for state in result.states] == ['content_tab', 'content_tab']
    assert not any(kind == 'FILTER_APPLYING' for kind, _, _ in events)
    text, _, _ = extract_text(result.html)
    assert 'Original product content' in text
    assert 'Loaded product two complete text' in text and 'Loaded product three complete text' in text
    assert {tab_site+'detail-two', tab_site+'detail-three'} <= {link.url for link in result.links}
    assert TabWebsite.paths.count('/lazy') == 1


def test_actual_lazy_tab_failure_still_limits_discovery(tab_site):
    async def exercise():
        async with BrowserExplorer(actions=10) as browser:
            return await browser.explore(tab_site+'broken')
    result = asyncio.run(exercise())
    assert result.script_errors
    assert any('did not expose readable content' in reason for reason in result.limitations)


def test_js_failure_with_empty_html_still_limits_discovery(tab_site):
    async def exercise():
        async with BrowserExplorer(actions=10) as browser:
            return await browser.explore(tab_site+'empty')
    result = asyncio.run(exercise())
    assert result.script_errors
    assert any('no readable HTML content' in reason for reason in result.limitations)


def test_audit_keeps_sections_as_one_html_page_with_diagnostics_and_real_depth(tmp_path, tab_site):
    config = AuditConfig(target_url=tab_site, database_path=tmp_path/'audit.sqlite', output_dir=tmp_path/'reports',
        use_browser=True, discovery_only=True, max_retries=0, max_browser_actions=10)
    result = asyncio.run(CoverageAuditor(config).run())
    assert result['status'] == 'AWAITING_CORPUS'
    with sqlite3.connect(config.database_path) as db:
        assert db.execute('SELECT COUNT(*) FROM filter_definitions').fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM urls WHERE page_type='HTML'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM audit_events WHERE url='about:blank'").fetchone()[0] == 0
        note = db.execute("SELECT depth,severity,details_json FROM audit_events WHERE kind='JAVASCRIPT_DIAGNOSTIC'").fetchone()
        assert note and note[0] == 0 and note[1] == 'INFO' and 'script_stack' in note[2]
        assert db.execute("SELECT COUNT(*) FROM audit_limits WHERE reason LIKE '%JavaScript error%'").fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM corpus_matches').fetchone()[0] == 0
        assert db.execute('SELECT cleaned_text FROM page_snapshots WHERE normalized_url=?', (tab_site,)).fetchone()[0].count('Fourth product') == 1

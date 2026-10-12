from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from crawler_coverage_auditor.discovery.browser import BrowserExplorer


class RenderedHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        body, mime = "", "text/html"
        if self.path == "/":
            body = """<html><body><main><h1>Rendered archive</h1>
            <select name='year' id='year'>
              <option value='2026'>2026</option><option value='2021'>2021</option>
            </select><div id='filtered'></div><iframe src='/frame'></iframe><div id='results'></div>
            <button id='more' onclick='loadMore()'>Load More</button></main>
            <script>document.querySelector('#year').addEventListener('change',function(){document.querySelector('#filtered').innerHTML='<a href="/archive/'+this.value+'/article">Archive item</a>'});
            let n=0;async function loadMore(){n++;const r=await fetch('/api/items?page='+n);const data=await r.json();
              document.querySelector('#results').insertAdjacentHTML('beforeend','<a href="'+data.url+'">Item '+n+'</a>');
              if(n>=3)document.querySelector('#more').remove();}</script></body></html>"""
        elif self.path == "/frame":
            body = "<a href='/deep/frame/item'>Iframe content</a>"
        elif self.path.startswith("/api/items"):
            from urllib.parse import parse_qs, urlsplit
            page = parse_qs(urlsplit(self.path).query)["page"][0]
            body, mime = json.dumps({"url": f"/detail/{page}"}), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.end_headers()
        self.wfile.write(body.encode())


def test_rendered_filters_load_more_api_and_iframes_are_collected():
    server = ThreadingHTTPServer(("127.0.0.1", 0), RenderedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    async def explore():
        async with BrowserExplorer(actions=40) as explorer:
            assert not explorer.error, explorer.error
            return await explorer.explore(base + "/")
    try:
        result = asyncio.run(explore())
        assert not result.error, result.error
        urls = {link.url for link in result.links}
        assert {base + path for path in ["/deep/frame/item", "/detail/1", "/detail/2", "/detail/3", "/archive/2021/article"]} <= urls
        assert any(request.resource_type == "fetch" for request in result.network)
        assert {state["value"] for state in result.states} == {"2026", "2021"}
        assert any("DOM-only" in reason for reason in result.limitations)
    finally:
        server.shutdown()
        server.server_close()

from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from crawler_coverage_auditor.discovery.evidence import embedded_links, header_links
from crawler_coverage_auditor.discovery.page import analyzed
from crawler_coverage_auditor.discovery.readiness import ContentReadiness
from crawler_coverage_auditor.discovery.resources import (
    URL_ONLY_TYPES,
    json_links,
    resource_type,
)
from crawler_coverage_auditor.discovery.static_links import Link, extract_links
from crawler_coverage_auditor.discovery.tabs import (
    TAB_SELECTOR,
    detect_tabs,
    merge_tab_content,
    panel_has_content,
    same_document_url,
)
from crawler_coverage_auditor.validation.access import detect_access
from crawler_coverage_auditor.validation.fingerprints import sha256_text
from crawler_coverage_auditor.validation.resource_policy import excluded_resource_url
from crawler_coverage_auditor.validation.text_extract import extract_text


@dataclass(frozen=True, slots=True)
class NetworkRequest:
    url: str
    method: str
    resource_type: str
    status_code: int | None


@dataclass(frozen=True, slots=True)
class BrowserDiscovery:
    links: tuple[Link, ...]
    network: tuple[NetworkRequest, ...]
    error: str | None = None
    html: str = ""
    final_url: str = ""
    limitations: tuple[str, ...] = ()
    states: tuple[dict, ...] = ()
    http_status: int | None = None
    script_errors: tuple[dict, ...] = ()


class BrowserExplorer:
    """Reuse one cookie/session context; explore read-only controls until content stabilizes."""

    def __init__(self, actions: int | None = None, on_event=None, allowed=None, stopped=None, pace=None,
                 storage_state=None, challenge_wait_seconds: float = 12.0, on_response=None, user_agent=None,
                 sources=None, max_filter_values=None, target_scope=None):
        # Accept historical arguments without reinstating arbitrary count cutoffs.
        self.actions = None
        self.on_event = on_event
        self.allowed = allowed
        self.target_scope = target_scope
        self.stopped = stopped or (lambda: False)
        self.pace = pace
        self.storage_state = storage_state
        self.challenge_wait_seconds = min(30.0, max(0.0, challenge_wait_seconds))
        self.on_response = on_response
        self.user_agent = user_agent
        from crawler_coverage_auditor.config import DISCOVERY_SOURCES
        self.sources = set(DISCOVERY_SOURCES if sources is None else sources)
        self.max_filter_values = None
        self._paused_hosts: set[str] = set()
        self.playwright = self.browser = self.context = None
        self._camoufox = None
        self.engine = None
        self._route_links = {}
        self._route_limitations = []
        self._header_lock = asyncio.Lock()
        self.error = None

    async def __aenter__(self):
        try:
            from playwright.async_api import async_playwright

            from crawler_coverage_auditor.discovery.local_browser import (
                browser_engine,
                camoufox_options,
            )
            from crawler_coverage_auditor.integrations.main_crawler import (
                browser_profile,
            )
            self.engine = browser_engine()
            profile = browser_profile() if self.engine == 'chromium' else {'enabled': False, 'init_script': None}
            if self.engine == 'camoufox':
                from camoufox.async_api import AsyncCamoufox
                self._camoufox = AsyncCamoufox(**camoufox_options())
                self.browser = await self._camoufox.__aenter__()
            else:
                self.playwright = await async_playwright().start()
                self.browser = await self.playwright.chromium.launch(headless=True,
                    **({'args': profile['args'], 'ignore_default_args': profile['ignore_default_args']} if profile['enabled'] else {}))
            self.context = await self.browser.new_context(ignore_https_errors=False, accept_downloads=False, service_workers='block',
                                                         storage_state=self.storage_state,
                                                         **({'no_viewport': True} if self.engine == 'camoufox' else {}),
                                                         **({'user_agent': self.user_agent} if self.user_agent and self.engine == 'chromium' else {}))
            if self.on_event:
                self.on_event('BROWSER_ENGINE', f'Using local {self.engine} browser; no main-crawler browser service connection', None)
            if profile['init_script']:
                await self.context.add_init_script(profile['init_script'])
                if self.on_event:
                    self.on_event('MAIN_CRAWLER_PROFILE', 'Using the imported main crawler browser identity helpers; access success still requires actual valid content', None)
            async def restrict(route):
                request = route.request
                if request.is_navigation_request() and self.target_scope and not self.target_scope(request.url):
                    self._route_limitations.append(f'OUT_OF_SCOPE: external browser navigation blocked: {request.url}')
                    await route.abort()
                    return
                if excluded_resource_url(request.url):
                    await route.abort()
                    return
                from crawler_coverage_auditor.validation.network_safety import (
                    check_public_request,
                )
                try:
                    await check_public_request(request.url)
                except ValueError:
                    await route.abort()
                    return
                # Render HTML with scripts/styles, but never download linked files.
                kind = resource_type(request.url)
                if request.resource_type in {'image', 'media', 'font'} or (kind in URL_ONLY_TYPES and request.resource_type not in {'script', 'stylesheet'}):
                    await route.abort()
                    return
                main_navigation = request.is_navigation_request() and request.frame == request.frame.page.main_frame
                if request.resource_type not in {'script', 'stylesheet'} and not main_navigation:
                    try:
                        async with asyncio.timeout(0.5):
                            file_sources = await request.frame.page.locator('object[data], embed[src], a[download][href]').evaluate_all(
                                "els => els.map(e => e.tagName === 'OBJECT' ? e.data : e.tagName === 'A' ? e.href : e.src)")
                    except Exception:
                        file_sources = []
                    if request.url in file_sources:
                        await route.abort()
                        return
                if urlsplit(request.url).netloc in self._paused_hosts:
                    await route.abort()
                    return
                permitted = self.allowed(request.url) if request.resource_type in {"document", "xhr", "fetch"} and self.allowed else True
                permitted = await permitted if inspect.isawaitable(permitted) else permitted
                if permitted and request.resource_type in {'document', 'xhr', 'fetch'} and self.pace:
                    permitted = await self.pace(request.url)
                if urlsplit(request.url).netloc in self._paused_hosts:
                    permitted = False
                if not permitted:
                    await route.abort()
                else:
                    if self.engine == 'camoufox' and request.resource_type in {'document', 'xhr', 'fetch'}:
                        if not await self._headers_permit(request):
                            await route.abort()
                            return
                        # The HEAD probe and the actual browser request are both paced.
                        if self.pace and not await self.pace(request.url):
                            await route.abort()
                            return
                    await route.continue_()
            await self.context.route("**/*", restrict)
        except Exception as error:
            self.error = f"Browser rendering unavailable: {error}"
        return self

    async def _headers_permit(self, request):
        """Firefox has no CDP response interception: check headers without bodies.

        Unknown/unsupported HEAD responses fail closed. Do not claim the browser
        explored content that the URL-only resource guard prevented it loading.
        Redirects are rechecked by routing each subsequent browser request.
        """
        if request.method not in {'GET', 'HEAD'}:
            self._route_limitations.append(f'Local Camoufox skipped non-read-only request: {request.method} {request.url}')
            return False
        response = None
        try:
            async with self._header_lock:
                response = await self.context.request.head(request.url, max_redirects=0, timeout=15000)
                headers, status = response.headers, response.status
                cooldown = self.on_response(request.url, status, headers) if self.on_response else 0
                if status == 429 or (cooldown and cooldown > 60):
                    self._paused_hosts.add(urlsplit(request.url).netloc)
                    self._route_limitations.append(f'Browser header check rate limited: HTTP {status} at {request.url}')
                    return False
                if status in {401, 451} or status >= 500:
                    self._route_limitations.append(f'Local Camoufox header check blocked: HTTP {status} at {request.url}; body not requested')
                    return False
                kind = resource_type(request.url, headers.get('content-type', ''))
                if kind in URL_ONLY_TYPES or 'attachment' in headers.get('content-disposition', '').lower():
                    tag = 'img' if kind == 'IMAGE' else 'source' if kind == 'RESOURCE' else 'download'
                    self._route_links[request.url] = Link(request.url, 'Resource URL only', tag)
                    return False
                if status in {301, 302, 303, 307, 308}:
                    return True
                mime = headers.get('content-type', '').split(';')[0].lower().strip()
                if status != 405 and (mime in {'text/html', 'application/xhtml+xml', 'application/json', 'text/json'} or mime.endswith('+json')):
                    return True
                self._route_limitations.append(f'Local Camoufox cannot confirm safe HTML/JSON headers: HTTP {status} at {request.url}; body not requested')
                return False
        except Exception as error:
            self._route_limitations.append(f'Local Camoufox header check unavailable: {type(error).__name__} at {request.url}; body not requested')
            return False
        finally:
            if response:
                await response.dispose()

    async def __aexit__(self, *_):
        if self.context:
            await self._close_bounded(self.context.close, 'browser context')
        if self._camoufox:
            await self._close_bounded(lambda: self._camoufox.__aexit__(None, None, None), 'local Camoufox driver')
        elif self.browser:
            await self._close_bounded(self.browser.close, 'browser')
        if self.playwright:
            await self._close_bounded(self.playwright.stop, 'browser driver')

    async def _close_bounded(self, close, label, *, timeout=5.0):
        try:
            async with asyncio.timeout(timeout):
                await close()
        except Exception as error:
            if self.on_event:
                self.on_event('CLEANUP_WARNING', f'{label} cleanup failed or timed out: {type(error).__name__}; continuing remaining owned-resource cleanup', None)

    async def _drain_tasks(self, tasks, *, timeout=2.0):
        pending = {task for task in tasks if not task.done()}
        if pending:
            _, pending = await asyncio.wait(pending, timeout=timeout)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.wait(pending, timeout=timeout)
        # Retrieve completed exceptions, so failed observers do not leak warnings.
        for task in tasks:
            if task.done() and not task.cancelled():
                task.exception()
        return not pending

    @analyzed
    async def explore(self, url: str) -> BrowserDiscovery:
        if not self.context:
            return BrowserDiscovery((), (), self.error or "Browser unavailable")
        if urlsplit(url).netloc in self._paused_hosts:
            return BrowserDiscovery((), (), "Browser host paused after rate limiting; wait for the server cooldown before a new discovery scan")
        links: dict[str, Link] = {}
        network: list[NetworkRequest] = []
        limitations: list[str] = []
        self._route_links = links
        self._route_limitations = limitations
        states: list[dict] = []
        script_errors: dict[tuple[str, str], dict] = {}
        tab_fragments: list[str] = []
        pagination_urls: set[str] = set()
        tasks: list[asyncio.Task] = []
        frame_snapshots: dict[object, tuple[str, str]] = {}
        try:
            page = await self.context.new_page()
        except Exception as error:
            return BrowserDiscovery((), (), f'Browser page unavailable: {type(error).__name__}: {error}')
        session = None
        readiness = ContentReadiness(url)
        page.on('request', readiness.started)
        page.on('requestfinished', readiness.finished)
        page.on('requestfailed', readiness.finished)
        def resource_link(request):
            if excluded_resource_url(request.url):
                return
            kind = resource_type(request.url)
            if request.resource_type in {'image', 'media', 'font'} or (kind in URL_ONLY_TYPES and request.resource_type not in {'script', 'stylesheet'}):
                tag = 'img' if request.resource_type == 'image' or kind == 'IMAGE' else 'download' if kind == 'DOCUMENT' else 'source'
                links[request.url] = Link(request.url, 'Resource URL only', tag)
        page.on('request', resource_link)
        async def cancel_download(download):
            if not excluded_resource_url(download.url):
                links[download.url] = Link(download.url, 'Resource URL only', 'download')
            await download.cancel()
        page.on('download', lambda download: tasks.append(asyncio.create_task(cancel_download(download))))
        async def inspect_headers(event):
            # Extensionless downloads cannot be classified until headers arrive.
            # Pause at the response stage and cancel before reading their bodies.
            headers = {item['name'].lower(): item['value'] for item in event.get('responseHeaders', [])}
            target = event['request']['url']
            kind = resource_type(target, headers.get('content-type', ''))
            file_response = kind in URL_ONLY_TYPES or 'attachment' in headers.get('content-disposition', '').lower()
            try:
                if file_response and event.get('resourceType') not in {'Script', 'Stylesheet'}:
                    tag = 'img' if kind == 'IMAGE' else 'source' if kind == 'RESOURCE' else 'download'
                    links[target] = Link(target, 'Resource URL only', tag)
                    status = event.get('responseStatusCode')
                    if status and status >= 400:
                        network.append(NetworkRequest(target, event['request']['method'], event.get('resourceType', 'other').lower(), status))
                        limitations.append(f'Browser request blocked: HTTP {status} at {target}; response body not downloaded')
                        cooldown = self.on_response(target, status, headers) if self.on_response else 0
                        if status == 429 or (cooldown and cooldown > 60):
                            self._paused_hosts.add(urlsplit(target).netloc)
                    await session.send('Fetch.failRequest', {'requestId': event['requestId'], 'errorReason': 'Aborted'})
                else:
                    await session.send('Fetch.continueRequest', {'requestId': event['requestId']})
            except Exception:
                # Page/context may already be closing after a stopped scan.
                pass
        used = 0
        current_response = None
        async def record(response):
            nonlocal current_response
            request = response.request
            if request.resource_type not in {"xhr", "fetch", "document", "image"}:
                return
            network.append(NetworkRequest(response.url, request.method, request.resource_type, response.status))
            if self.on_response:
                cooldown = self.on_response(response.url, response.status, response.headers)
                if cooldown and cooldown > 60:
                    self._paused_hosts.add(urlsplit(response.url).netloc)
                    limitations.append(f"Browser host paused: Retry-After {cooldown:.0f}s at {response.url}; restart discovery after the cooldown")
            if response.status == 429:
                self._paused_hosts.add(urlsplit(response.url).netloc)
            main_document = request.resource_type == "document" and request.frame == page.main_frame
            if main_document:
                current_response = response
            for link in header_links(response.headers.get('link', ''), response.url) if 'HTTP_LINK' in self.sources else []:
                links[link.url] = link
            if not main_document and (response.status in {401, 403, 429} or response.status >= 500):
                limitations.append(f"Browser {request.resource_type} blocked: HTTP {response.status} at {response.url}")
            if 'API' in self.sources and "json" in response.headers.get("content-type", ""):
                try:
                    body = await response.text()
                    for link in json_links(body, response.url):
                        links[link.url] = link
                    observer = getattr(self, 'on_json_response', None)
                    if observer and request.method == 'GET':
                        from datetime import UTC, datetime

                        from crawler_coverage_auditor.validation.http import FetchResult
                        headers = {key: value for key, value in response.headers.items()
                                   if key.lower() not in {'content-encoding', 'content-length'}}
                        headers['content-type'] = 'application/json; charset=utf-8'
                        observer(FetchResult(request.url, response.url, response.status, headers,
                            body.encode('utf-8'), captured_at=datetime.now(UTC).isoformat()), await request.all_headers())
                except Exception:
                    pass
        page.on("response", lambda response: tasks.append(asyncio.create_task(record(response))))
        def event(kind, message):
            if self.on_event:
                self.on_event(kind, message, page.url if page.url not in {'about:blank', ''} else url)
        def script_error(error):
            source = page.url if page.url not in {'about:blank', ''} else url
            message = str(error)
            script_errors[(source, message)] = {'url': source, 'message': message, 'stack': getattr(error, 'stack', '')}
        async def collect():
            html = await page.content()
            for frame in page.frames:
                try:
                    frame_html = html if frame == page.main_frame else await frame.content()
                    frame_url = frame.url or page.url
                    if frame_snapshots.get(frame) == (frame_url, frame_html):
                        continue
                    frame_snapshots[frame] = (frame_url, frame_html)
                    for link in extract_links(frame_html, frame_url):
                        links[link.url] = link
                    for link in embedded_links(frame_html, frame_url) if 'STRUCTURED_DATA' in self.sources else []:
                        links[link.url] = link
                except Exception:
                    pass
            if not same_document_url(page.url, url):
                links[page.url] = Link(page.url, "Rendered navigation", 'pagination' if page.url in pagination_urls else "browser")
            return html
        async def settle():
            if not await readiness.wait(page, stopped=self.stopped) and readiness.pending and not self.stopped():
                reason = 'Content requests are still pending after the browser readiness wait; some content may be incomplete'
                if reason not in limitations:
                    limitations.append(reason)
        async def navigate(target):
            for attempt in range(3):
                try:
                    return await page.goto(target, wait_until='domcontentloaded', timeout=45000)
                except Exception as error:
                    if self.stopped() or attempt == 2 or not any(token in str(error) for token in ('ERR_NAME_NOT_RESOLVED', 'ERR_CONNECTION_RESET', 'ERR_CONNECTION_CLOSED', 'Timeout')):
                        raise
                    event('BROWSER_RETRY', f'Temporary navigation failure; retry {attempt + 1}/2: {str(error).split("Call log:")[0].strip()}')
                    for _ in range((attempt + 1) * 8):
                        if self.stopped():
                            raise
                        await page.wait_for_timeout(250)
        async def close_gallery():
            # Close only known media lightboxes, never consent/login/CAPTCHA dialogs.
            closers = page.locator('.fancybox-container .fancybox-button--close, .fancybox-close, .mfp-wrap .mfp-close')
            for index in range(await closers.count()):
                closer = closers.nth(index)
                if await closer.is_visible():
                    await closer.click(timeout=2000)
                    event('GALLERY_CLOSED', 'Closed image preview to continue discovering page links; images remain URL-only')
                    await page.wait_for_timeout(150)
                    return
        async def expand():
            nonlocal used
            if 'LOAD_MORE' not in self.sources:
                return
            seen: set[tuple[str, int]] = set()
            stable = 0
            while not self.stopped():
                await collect()
                await close_gallery()
                # Result cards can have identical labels while exposing new URLs.
                # Repeated text alone must not terminate useful discovery.
                fingerprint = (sha256_text(await page.locator("body").inner_text()), len(links))
                stable = stable + 1 if fingerprint in seen else 0
                seen.add(fingerprint)
                more = page.get_by_role("button", name=re.compile(r"^(load more|show more|more results|next|older|next page)$", re.I))
                before_url = page.url
                activated = False
                if await more.count() and await more.first.is_visible() and await more.first.is_enabled():
                    event("LOAD_MORE", "Opening more results for the current filter state")
                    try:
                        await more.first.click(timeout=3000)
                        activated = True
                    except Exception as error:
                        # A final asynchronous response may remove the button between inspection and click.
                        if await more.count():
                            limitations.append(f"More-results control could not be activated: {str(error).split('Call log:')[0].strip()}")
                            break
                else:
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await settle()
                if activated and not same_document_url(before_url, page.url):
                    pagination_urls.add(page.url)
                    links[page.url] = Link(page.url, 'URL reached through more-results control', 'pagination')
                used += 1
                if stable >= 2:
                    break
        try:
            if self.engine != 'camoufox':
                session = await self.context.new_cdp_session(page)
                session.on('Fetch.requestPaused', lambda event: tasks.append(asyncio.create_task(inspect_headers(event))))
                await session.send('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Response'}]})
            event("RENDERING", "Rendering JavaScript and inspecting controls")
            page.on('pageerror', script_error)
            initial_response = await navigate(url)
            await settle()
            current_response = current_response or initial_response
            initial_status = current_response.status if current_response else None
            initial_html = await page.content()
            issue = detect_access(initial_status, current_response.headers if current_response else {}, initial_html, page.url, requested_url=url)
            if issue and issue.browser_wait and self.challenge_wait_seconds:
                event("BROWSER_WAIT", f"{issue.provider or 'Website'}: waiting for ordinary browser verification (no CAPTCHA interaction)")
                deadline = asyncio.get_running_loop().time() + self.challenge_wait_seconds
                while issue and issue.browser_wait and not self.stopped() and asyncio.get_running_loop().time() < deadline:
                    await page.wait_for_timeout(400)
                    initial_html = await page.content()
                    initial_status = current_response.status if current_response else None
                    issue = detect_access(initial_status, current_response.headers if current_response else {}, initial_html, page.url, requested_url=url)
                if not issue:
                    event("BROWSER_RECOVERED", "Ordinary browser verification completed; inspecting actual website content")
            if issue:
                hint = " Open the public-session helper and complete it yourself, or ask the owner to allowlist CrawlScope." if issue.status in {"CAPTCHA_REQUIRED", "BOT_BLOCKED", "BLOCKED"} else ""
                event(issue.status, issue.reason + hint)
                return BrowserDiscovery((), tuple(network), issue.reason + hint, initial_html, page.url, http_status=initial_status, script_errors=tuple(script_errors.values()))
            if initial_status in {401, 403, 429} or (initial_status and initial_status >= 500):
                return BrowserDiscovery(tuple(links.values()), tuple(network), f"Browser access blocked: HTTP {initial_status}", initial_html, page.url, http_status=initial_status, script_errors=tuple(script_errors.values()))
            initial_html = await collect()
            initial_url = page.url
            baseline_ready = True
            control_script = """els => els.map(e => ({
                name:e.name||e.id, type:e.tagName.toLowerCase()==='select'?'select':e.type,
                values:e.tagName.toLowerCase()==='select'?Array.from(e.options).filter(o=>!o.disabled&&o.value).map(o=>({value:o.value,label:o.textContent})): [{value:e.value,label:e.getAttribute('aria-label')||e.labels?.[0]?.textContent||e.value}],
                method:e.form?.method||'javascript', action:e.form?.action||'', id:e.id,
                unsafe:!!e.form?.querySelector('input[type=password], input[type=file], input[type=email], input[type=tel], textarea, input[name=email], input[name=phone], input[name=organization], input[name^=captcha]') || /login|checkout|cart|register|delete|subscribe|contact|feedback/i.test(e.form?.action||'')
            })).filter(e=>e.name)"""
            control_specs = await page.locator("select, input[type=checkbox], input[type=radio]").evaluate_all(control_script)
            baseline_specs = list(control_specs)
            seen_dependencies = set()
            if await page.locator('[role=combobox]:not(select), [role=checkbox]:not(input)').count():
                limitations.append("Custom JavaScript filter controls detected; full value enumeration may require a website adapter")
            for spec in control_specs if 'FILTER_BROWSER' in self.sources else []:
                if self.stopped():
                    break
                if spec["method"].lower() == "get":
                    continue
                if spec["unsafe"]:
                    continue
                event("FILTER_DETECTED", f"Filter detected: {spec['name']} ({spec['method']})")
                for value in spec["values"]:
                    if self.stopped():
                        break
                    if not baseline_ready:
                        await navigate(url)
                        await settle()
                        baseline_ready = True
                    # Reconstruct only observed read-only dependencies after reset.
                    for prerequisite in spec.get('setup', []):
                        locator = page.locator(f'[name={json.dumps(prerequisite["name"])}]').first
                        if prerequisite['type'] == 'select':
                            await locator.select_option(prerequisite['value'], timeout=3000)
                        else:
                            locator = page.locator(f'[name={json.dumps(prerequisite["name"])}][value={json.dumps(prerequisite["value"])}]').first
                            await locator.check(timeout=3000)
                        await settle()
                    escaped = spec["name"].replace('"', '\\"')
                    control = page.locator(f'{"select" if spec["type"] == "select" else "input"}[name="{escaped}"], {"select" if spec["type"] == "select" else "input"}[id="{escaped}"]')
                    if spec["type"] != "select":
                        escaped_value = value["value"].replace('"', '\\"')
                        control = control.and_(page.locator(f'[value="{escaped_value}"]'))
                    control = control.first
                    if not await control.count():
                        continue
                    event("FILTER_APPLYING", f"Applying {spec['name']}: {value['label']}")
                    baseline_ready = False
                    if spec["type"] == "select":
                        await control.select_option(value["value"], timeout=3000)
                    else:
                        await control.check(timeout=3000)
                    if spec["method"].lower() == "post":
                        form = control.locator("xpath=ancestor::form")
                        submit = form.get_by_role("button", name=re.compile(r"apply|filter|search", re.I))
                        if await submit.count():
                            await submit.first.click(timeout=3000)
                        else:
                            limitations.append(f"POST filter {spec['name']} has no recognized read-only submit action")
                    await settle()
                    html = await collect()
                    from crawler_coverage_auditor.discovery.filters import (
                        dependent_controls,
                    )
                    changed = await page.locator("select, input[type=checkbox], input[type=radio]").evaluate_all(control_script)
                    selection = {'name': spec['name'], 'type': spec['type'], 'value': value['value'], 'setup': spec.get('setup', []), 'method': spec['method']}
                    for dependency in dependent_controls(baseline_specs, changed, selection):
                        key = (dependency['name'], tuple(sorted({entry['name']: entry['value'] for entry in dependency['setup']}.items())),
                               tuple(item['value'] for item in dependency['values']))
                        if key not in seen_dependencies:
                            seen_dependencies.add(key)
                            control_specs.append(dependency)
                            event('FILTER_DEPENDENCY', f"Filter dependency observed: {spec['name']} changes {dependency['name']} values")
                    states.append({"source_url": url, "state_url": page.url, "control": spec["name"], "value": value["value"], "label": value["label"], "method": spec["method"], "html": html})
                    used += 1
                    if page.url == url and html != initial_html:
                        limitations.append(f"DOM-only filter {spec['name']}={value['value']} has no independent URL; linked items checked, state-level corpus presence unverified")
                    await expand()
            # Restore the baseline only if an actual filter was explored.
            if not baseline_ready and not self.stopped():
                await navigate(url)
                await settle()
                baseline_ready = True
            tabs = detect_tabs(await page.content(), page.url) if 'TABS' in self.sources else []
            loaded_tabs = [tab for tab in tabs if tab.kind == 'CONTENT' and tab.loaded]
            if loaded_tabs:
                event('PAGE_SECTIONS', f'{len(loaded_tabs)} same-page sections already contain their content; collected together as one HTML page (not filters)')
            for spec in tabs:
                if spec.kind == 'NAVIGATION':
                    links[spec.link_url] = Link(spec.link_url, spec.label, 'browser')
                    continue
                if spec.kind == 'CONTENT' and spec.loaded:
                    continue
                if any(token in (spec.control_id or '').lower() for token in ('gallery', 'lightbox', 'fancybox')):
                    # A gallery opener is not a content filter/tab. Its resource URLs
                    # have already been captured without opening/downloading images.
                    continue
                if self.stopped():
                    break
                if spec.control_id:
                    escaped = spec.control_id.replace('\\', '\\\\').replace('"', '\\"')
                    tab = page.locator(f'[id="{escaped}"]').first
                else:
                    tab = page.locator(TAB_SELECTOR).filter(has_text=spec.label).first
                if await tab.count() and await tab.is_visible():
                    is_filter = spec.kind == 'FILTER'
                    event('FILTER_APPLYING' if is_filter else 'TAB_OPENING', f"Applying filter tab: {spec.label}" if is_filter else f"Loading same-page content tab: {spec.label}")
                    await tab.click(timeout=3000)
                    await settle()
                    if is_filter or not same_document_url(page.url, initial_url):
                        baseline_ready = False
                    html = await collect()
                    states.append({"source_url": url, "state_url": page.url, "control": "tab", "value": spec.label, "label": spec.label, "method": "javascript", "html": html, 'kind': 'filter' if is_filter else 'content_tab'})
                    used += 1
                    if not is_filter and same_document_url(page.url, initial_url):
                        snapshot = BeautifulSoup(html, 'lxml')
                        panel = snapshot.find(id=spec.target_id) if spec.target_id else snapshot.find('main') or snapshot.body
                        if panel_has_content(panel):
                            tab_fragments.append(str(panel))
                        elif spec.target_id:
                            limitations.append(f'Content tab {spec.label} did not expose readable content; tab discovery may be incomplete')
                    await expand()
            if len([spec for spec in control_specs if not spec['unsafe']]) > 1:
                limitations.append("Multiple rendered filters: independent values explored; dependent combinations may require a website adapter")
            if (not baseline_ready or not same_document_url(page.url, initial_url)) and not self.stopped():
                await navigate(url)
                await settle()
            await expand()
            html = await collect()
            if same_document_url(page.url, initial_url):
                initial_html = html
            if tasks:
                if not await self._drain_tasks(tasks):
                    limitations.append('Browser response observation exceeded its deadline; some API/resource evidence may be incomplete')
            if script_errors and not extract_text(initial_html)[0].strip():
                limitations.append('Website JavaScript failed and no readable HTML content was available; exploration is incomplete')
            return BrowserDiscovery(tuple(links.values()), tuple(network), None, merge_tab_content(initial_html, tab_fragments), initial_url, tuple(dict.fromkeys(limitations)), tuple(states), initial_status, tuple(script_errors.values()))
        except Exception as error:
            return BrowserDiscovery(tuple(links.values()), tuple(network), f"{type(error).__name__}: {error}", limitations=tuple(limitations), states=tuple(states), script_errors=tuple(script_errors.values()))
        finally:
            if tasks:
                await self._drain_tasks(tasks)
            await self._close_bounded(page.close, 'page')


async def discover_rendered_page(url: str) -> BrowserDiscovery:
    async with BrowserExplorer() as browser:
        return await browser.explore(url)

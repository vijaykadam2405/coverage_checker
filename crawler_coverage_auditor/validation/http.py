from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from http.cookiejar import DefaultCookiePolicy
from tempfile import SpooledTemporaryFile
from time import perf_counter
from urllib.parse import urlsplit
from urllib.request import Request as CookieRequest

import httpx

from crawler_coverage_auditor.validation.failure_policy import (
    quota_delay,
    retry_after,
    retryable_status,
    status_reason,
    transport_reason,
)
from crawler_coverage_auditor.validation.resource_policy import excluded_resource_url
from crawler_coverage_auditor.validation.response_cache import ObservedResponses
from crawler_coverage_auditor.validation.site_session import import_cookies


@dataclass(slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    content: bytes
    error: str | None = None
    url_only: bool = False
    failure_reason: str | None = None
    captured_at: str | None = None

    @property
    def text(self) -> str:
        response = httpx.Response(200, headers=self.headers, content=self.content)
        if response.charset_encoding or not self.content:
            return response.text
        # Without a transport charset, HTML may declare one in meta or a BOM.
        # Preserve meaningful multilingual text instead of silently dropping bytes.
        from bs4 import UnicodeDammit
        decoded = UnicodeDammit(self.content, is_html=True)
        return decoded.unicode_markup if decoded.unicode_markup is not None else response.text


class OutOfScopeRequest(httpx.RequestError):
    """A target navigation redirected outside the audit's website scope."""


class PoliteHttpClient:
    """Retrying asynchronous client with per-process concurrency control.

    Certificate verification stays enabled for targets. Corpus uses its own dedicated
    read-only API client; target credentials and Corpus headers are never shared.
    """

    def __init__(self, concurrency: int, timeout: float, retries: int, user_agent: str,
                 *, verify: bool = True, on_event: Callable | None = None, allowed: Callable | None = None,
                 min_interval: float = 0.0, stopped: Callable | None = None,
                 max_response_bytes: int = 8 * 1024 * 1024, target_scope: Callable | None = None) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        self._retries = retries
        self._attempt_budget = timeout
        if max_response_bytes < 1:
            raise ValueError('Response byte limit must be positive')
        self.max_response_bytes = max_response_bytes
        self._on_event = on_event
        self._allowed = allowed
        self._target_scope = target_scope
        self._scope_guard = ContextVar('target_navigation_scope', default=False)
        self.min_interval = max(0.0, min_interval)
        self._stopped = stopped or (lambda: False)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._next_allowed: dict[str, float] = {}
        self._host_intervals: dict[str, float] = {}
        self.observed_responses = ObservedResponses()
        self.metrics = {'request_attempts': 0, 'response_headers_seconds': 0.0}
        self._client = httpx.AsyncClient(
            follow_redirects=True, verify=verify, timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        self._client.cookies.jar.set_policy(DefaultCookiePolicy(
            strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
        async def scoped_headers(request):
            if self._scope_guard.get() and self._target_scope and not self._target_scope(str(request.url)):
                raise OutOfScopeRequest('OUT_OF_SCOPE: external redirect not followed', request=request)
            if excluded_resource_url(str(request.url)):
                # Also protects redirects from an HTML URL to an EOT font.
                raise httpx.RequestError('EOT font excluded from CrawlScope', request=request)
            from crawler_coverage_auditor.validation.network_safety import (
                check_public_request,
            )
            try:
                await check_public_request(str(request.url))
            except ValueError as error:
                raise httpx.RequestError(str(error), request=request) from None
            # HTTPX merges cookies into a fresh jar; explicitly enforce the source
            # jar's host-only policy on every outgoing request, including redirects.
            cookie_request = CookieRequest(str(request.url))
            self._client.cookies.jar.add_cookie_header(cookie_request)
            request.headers.pop('Cookie', None)
            cookie_header = cookie_request.get_header('Cookie')
            if cookie_header:
                request.headers['Cookie'] = cookie_header
            if request.headers.get('Referer'):
                source = urlsplit(request.headers['Referer'])
                target = urlsplit(str(request.url))
                if (source.scheme, source.netloc) != (target.scheme, target.netloc):
                    request.headers.pop('Referer', None)
            self.metrics['request_attempts'] += 1
            request.extensions['crawlscope_started'] = perf_counter()
        self._client.event_hooks['request'].append(scoped_headers)
        async def received_headers(response):
            started = response.request.extensions.get('crawlscope_started')
            if started is not None:
                self.metrics['response_headers_seconds'] += perf_counter() - started
        self._client.event_hooks['response'].append(received_headers)

    def use_browser_session(self, url: str, cookies: list[dict], user_agent: str | None = None) -> None:
        import_cookies(self._client, cookies, url)
        if user_agent:
            self._client.headers["User-Agent"] = user_agent

    def browser_cookies(self, url: str) -> list[dict]:
        host = urlsplit(url).hostname.lower()
        return [{"name": cookie.name, "value": cookie.value, "domain": cookie.domain,
                 "path": cookie.path, "expires": cookie.expires if cookie.expires is not None else -1,
                 "secure": cookie.secure, "httpOnly": cookie.has_nonstandard_attr("HttpOnly") and cookie.get_nonstandard_attr("HttpOnly") is not False, "sameSite": "Lax"}
                for cookie in self._client.cookies.jar
                if host == cookie.domain.lstrip('.') or (cookie.domain.startswith('.') and host.endswith(cookie.domain))]

    def remember_browser_response(self, response, request_headers):
        return self.observed_responses.remember(response, request_headers)

    def _equivalent_headers(self, url, referer):
        request = self._client.build_request('GET', url)
        cookies = CookieRequest(url)
        self._client.cookies.jar.add_cookie_header(cookies)
        request.headers.pop('Cookie', None)
        if cookies.get_header('Cookie'):
            request.headers['Cookie'] = cookies.get_header('Cookie')
        if referer:
            source, target = urlsplit(referer), urlsplit(url)
            if not source.username and not source.password and (source.scheme, source.netloc) == (target.scheme, target.netloc):
                request.headers['Referer'] = referer
        return dict(request.headers)

    def observe_response(self, url: str, status: int, headers: dict[str, str]) -> float:
        """Apply browser AND HTTP Retry-After to the same host schedule."""
        host = urlsplit(url).netloc.lower().removeprefix('www.')
        interval = self._host_intervals.get(host, self.min_interval)
        headers = {name.lower(): value for name, value in headers.items()}
        budget_delay = quota_delay(headers)
        cooldown = max(retry_after(headers.get('retry-after')) or 0.0, budget_delay or 0.0)
        if status in {429, 503} or budget_delay is not None:
            interval = max(interval, min(5.0, max(0.25, interval * 2)))
            self._host_intervals[host] = interval
            cooldown = max(cooldown, interval)
            if budget_delay is not None and self._on_event:
                self._on_event('HOST_COOLDOWN', f'Website request quota exhausted; preserving this response and waiting at least {cooldown:.1f}s before another request', url, severity='WARNING')
        self._next_allowed[host] = max(self._next_allowed.get(host, 0.0),
            asyncio.get_running_loop().time() + max(interval, cooldown))
        return cooldown

    async def __aenter__(self) -> "PoliteHttpClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    def set_host_interval(self, url: str, seconds: float) -> None:
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        self._host_intervals[host] = max(self.min_interval, seconds)

    async def _wait(self, seconds: float) -> bool:
        end = asyncio.get_running_loop().time() + seconds
        while not self._stopped():
            remaining = end - asyncio.get_running_loop().time()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(0.25, remaining))
        return False

    async def wait_turn(self, url: str) -> bool:
        """Share HTTP host pacing with browser navigations and content API requests."""
        host = urlsplit(url).netloc.lower().removeprefix('www.')
        async with self._host_locks.setdefault(host, asyncio.Lock()):
            if not await self._wait(max(0.0, self._next_allowed.get(host, 0.0) - asyncio.get_running_loop().time())):
                return False
            self._next_allowed[host] = asyncio.get_running_loop().time() + self._host_intervals.get(host, self.min_interval)
            return True

    @asynccontextmanager
    async def metadata_stream(self, url):
        with SpooledTemporaryFile(max_size=1024*1024, mode='w+b') as sink:
            response = await self.get(url, discovery_metadata=True, _sink=sink)
            sink.seek(0)
            yield response, sink

    async def head(self, url: str, *, referer: str | None = None) -> FetchResult:
        """Validate file links/redirects using HEAD only; never request file bodies.

        Unsupported HEAD remains unverified, rather than falling back to a download.
        """
        return await self.get(url, referer=referer, _headers_only=True)

    async def get(self, url: str, *, check_policy: bool = True, referer: str | None = None, discovery_metadata: bool = False, _sink=None, _headers_only: bool = False) -> FetchResult:
        # External scripts/historical indexes are discovery sources, not inventory
        # pages. Only explicit metadata requests bypass the navigation scope guard.
        token = self._scope_guard.set(not discovery_metadata)
        try:
            if not discovery_metadata and self._target_scope and not self._target_scope(url):
                return FetchResult(url, url, 0, {}, b'', 'OUT_OF_SCOPE: external target excluded', failure_reason='OUT_OF_SCOPE')
            return await self._get_impl(url, check_policy=check_policy, referer=referer,
                discovery_metadata=discovery_metadata, _sink=_sink, _headers_only=_headers_only)
        finally:
            self._scope_guard.reset(token)

    async def _get_impl(self, url: str, *, check_policy: bool = True, referer: str | None = None, discovery_metadata: bool = False, _sink=None, _headers_only: bool = False) -> FetchResult:
        if excluded_resource_url(url):
            return FetchResult(url, url, 0, {}, b'', 'EOT font excluded from CrawlScope', failure_reason='RESOURCE_EXCLUDED')
        if check_policy and self._allowed:
            decision = self._allowed(url)
            decision = await decision if inspect.isawaitable(decision) else decision
            if not decision:
                return FetchResult(url, url, 0, {}, b"", "Robots restriction: URL disallowed or policy unavailable")
        if not _headers_only and not discovery_metadata and _sink is None and not self._stopped():
            observed = self.observed_responses.take(url, self._equivalent_headers(url, referer))
            if observed:
                if self._on_event:
                    self._on_event('RESPONSE_REUSED', 'Reused fresh GET JSON with matching request/session headers; no additional website request', url)
                return observed
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        last_error: str | None = None
        for attempt in range(self._retries + 1):
            if _sink is not None:
                _sink.seek(0)
                _sink.truncate()
            try:
                async with lock:
                    delay = max(0.0, self._next_allowed.get(host, 0.0) - asyncio.get_running_loop().time())
                    if not await self._wait(delay):
                        return FetchResult(url, url, 0, {}, b"", "Stopped by user")
                    async with self._semaphore:
                        # Page-session resource requests can require their referring page.
                        # Never transmit a cross-origin referrer or a credential-bearing URL.
                        source = urlsplit(referer) if referer else None
                        target = urlsplit(url)
                        headers = {"Referer": referer} if source and not source.username and not source.password and (source.scheme, source.netloc) == (target.scheme, target.netloc) else {}
                        # A slow trickle must not keep one worker alive indefinitely.
                        async with asyncio.timeout(self._attempt_budget):
                            async with self._client.stream('HEAD' if _headers_only else 'GET', url, headers=headers) as streamed:
                                if not discovery_metadata and self._target_scope and not self._target_scope(str(streamed.url)):
                                    return FetchResult(url, str(streamed.url), streamed.status_code, dict(streamed.headers), b'',
                                        'OUT_OF_SCOPE: external redirect excluded', failure_reason='OUT_OF_SCOPE')
                                if _headers_only:
                                    self.observe_response(str(streamed.url), streamed.status_code, dict(streamed.headers))
                                    # No body iterator is touched, including errors and
                                    # misleading .pdf links that now serve HTML.
                                    return FetchResult(url, str(streamed.url), streamed.status_code, dict(streamed.headers), b'',
                                        url_only=True, failure_reason=status_reason(streamed.status_code) if streamed.status_code >= 400 else None)
                                # Unknown/extensionless download links: inspect headers only,
                                # close the stream before consuming any resource bytes.
                                from crawler_coverage_auditor.discovery.resources import (
                                    URL_ONLY_TYPES,
                                    resource_type,
                                )
                                kind = resource_type(str(streamed.url), streamed.headers.get('content-type', ''))
                                attachment = 'attachment' in streamed.headers.get('content-disposition', '').lower()
                                mime = streamed.headers.get('content-type', '').lower()
                                declared_kind = resource_type(url)
                                metadata_path = urlsplit(url).path.lower().endswith(('.xml','.xml.gz','.txt','.js','.mjs','.json','.webmanifest'))
                                # Robots and sitemap XML are discovery instructions,
                                # not website documents whose content is audited.
                                metadata_body = discovery_metadata and not attachment and (metadata_path or declared_kind not in {'DOCUMENT','IMAGE'}) and ('xml' in mime or 'text/plain' in mime or 'gzip' in mime
                                    or 'javascript' in mime or 'json' in mime)
                                known_document = declared_kind in {'DOCUMENT','IMAGE'} and not metadata_path and mime not in {'text/html', 'application/xhtml+xml'}
                                if (kind in URL_ONLY_TYPES or attachment or known_document) and not metadata_body:
                                    if 200 <= streamed.status_code < 300:
                                        self.observe_response(url, streamed.status_code, dict(streamed.headers))
                                        return FetchResult(url, str(streamed.url), streamed.status_code, dict(streamed.headers), b'', url_only=True)
                                    # Keep error/retry handling without downloading a file body.
                                    content = b''
                                else:
                                    length = streamed.headers.get('content-length', '')
                                    cap = 50*1024*1024 if _sink is not None else self.max_response_bytes
                                    if length.isdigit() and int(length) > cap:
                                        self.observe_response(url, streamed.status_code, dict(streamed.headers))
                                        return FetchResult(url, str(streamed.url), streamed.status_code, dict(streamed.headers), b'',
                                            f'TOO_LARGE: declared body exceeds {cap} bytes; not downloaded', failure_reason='TOO_LARGE')
                                    buffer = bytearray()
                                    received = 0
                                    async for chunk in streamed.aiter_bytes(chunk_size=64 * 1024):
                                        received += len(chunk)
                                        if received > cap:
                                            self.observe_response(url, streamed.status_code, dict(streamed.headers))
                                            return FetchResult(url, str(streamed.url), streamed.status_code, dict(streamed.headers), b'',
                                                f'TOO_LARGE: decoded response exceeds {cap} bytes; partial body discarded', failure_reason='TOO_LARGE')
                                        if _sink is not None:
                                            _sink.write(chunk)
                                        else:
                                            buffer.extend(chunk)
                                    content = bytes(buffer)
                                decoded_headers = {key: value for key, value in streamed.headers.items()
                                                   if key.lower() not in {'content-encoding', 'content-length'}}
                                response = httpx.Response(streamed.status_code, headers=decoded_headers,
                                                          content=content, request=streamed.request)
                    cooldown = self.observe_response(url, response.status_code, dict(response.headers))
                    if cooldown > 60 or self._stopped():
                        # Never retry before a long server-requested delay; report the boundary instead.
                        return FetchResult(str(response.request.url), str(response.url), response.status_code, dict(response.headers), response.content,
                                           failure_reason=status_reason(response.status_code) if response.status_code >= 400 else None)
                if retryable_status(response.status_code) and attempt < self._retries:
                    requested_delay = retry_after(response.headers.get('Retry-After'))
                    delay = requested_delay if requested_delay is not None else min(20, 2 ** attempt)
                    if self._on_event:
                        self._on_event("RETRY", f"HTTP {response.status_code}; retry {attempt + 1} in {delay:.0f}s", url, severity="WARNING")
                    if not await self._wait(delay + random.uniform(0, 0.4)):
                        return FetchResult(url, url, 0, {}, b"", "Stopped by user")
                    continue
                return FetchResult(str(response.request.url), str(response.url), response.status_code,
                                   dict(response.headers), response.content,
                                   failure_reason=status_reason(response.status_code) if response.status_code >= 400 else None)
            except OutOfScopeRequest as exc:
                return FetchResult(url, str(exc.request.url), 0, {}, b'', str(exc), failure_reason='OUT_OF_SCOPE')
            except (httpx.HTTPError, ValueError, TimeoutError) as exc:
                reason = transport_reason(exc)
                last_error = f"{reason}: {type(exc).__name__}: {exc}"
                if self._on_event:
                    self._on_event("RETRY", last_error, url, severity="WARNING")
                if reason in {'SSL_ERROR', 'REDIRECT_LOOP', 'INVALID_REQUEST'}:
                    return FetchResult(url, url, 0, {}, b'', last_error, failure_reason=reason)
                if attempt < self._retries:
                    if not await self._wait(min(20, 1.5 ** attempt) + random.uniform(0, 0.4)):
                        return FetchResult(url, url, 0, {}, b"", "Stopped by user")
        return FetchResult(url, url, 0, {}, b"", last_error or "request failed", failure_reason=reason if last_error else 'FETCH_ERROR')

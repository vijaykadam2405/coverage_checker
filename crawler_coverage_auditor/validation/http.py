from __future__ import annotations

import asyncio
import random
import inspect
from urllib.parse import urlsplit
from collections.abc import Callable
from dataclasses import dataclass
from http.cookiejar import DefaultCookiePolicy
from urllib.request import Request as CookieRequest

import httpx

from crawler_coverage_auditor.validation.site_session import import_cookies
from crawler_coverage_auditor.validation.failure_policy import retry_after, quota_delay, retryable_status, transport_reason, status_reason


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


class PoliteHttpClient:
    """Retrying asynchronous client with per-process concurrency control.

    Certificate verification stays enabled for targets. Corpus uses its own dedicated
    read-only API client; target credentials and Corpus headers are never shared.
    """

    def __init__(self, concurrency: int, timeout: float, retries: int, user_agent: str,
                 *, verify: bool = True, on_event: Callable | None = None, allowed: Callable | None = None,
                 min_interval: float = 0.0, stopped: Callable | None = None,
                 max_response_bytes: int = 8 * 1024 * 1024) -> None:
        self._semaphore = asyncio.Semaphore(concurrency)
        self._retries = retries
        self._attempt_budget = timeout
        if max_response_bytes < 1:
            raise ValueError('Response byte limit must be positive')
        self.max_response_bytes = max_response_bytes
        self._on_event = on_event
        self._allowed = allowed
        self.min_interval = max(0.0, min_interval)
        self._stopped = stopped or (lambda: False)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._next_allowed: dict[str, float] = {}
        self._host_intervals: dict[str, float] = {}
        self._client = httpx.AsyncClient(
            follow_redirects=True, verify=verify, timeout=timeout,
            headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )
        self._client.cookies.jar.set_policy(DefaultCookiePolicy(
            strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
        async def scoped_headers(request):
            from crawler_coverage_auditor.validation.network_safety import check_public_request
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
        self._client.event_hooks['request'].append(scoped_headers)

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

    async def get(self, url: str, *, check_policy: bool = True, referer: str | None = None, discovery_metadata: bool = False) -> FetchResult:
        if check_policy and self._allowed:
            decision = self._allowed(url)
            decision = await decision if inspect.isawaitable(decision) else decision
            if not decision:
                return FetchResult(url, url, 0, {}, b"", "Robots restriction: URL disallowed or policy unavailable")
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        last_error: str | None = None
        for attempt in range(self._retries + 1):
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
                            async with self._client.stream('GET', url, headers=headers) as streamed:
                                # Unknown/extensionless download links: inspect headers only,
                                # close the stream before consuming any resource bytes.
                                from crawler_coverage_auditor.discovery.resources import resource_type, URL_ONLY_TYPES
                                kind = resource_type(str(streamed.url), streamed.headers.get('content-type', ''))
                                attachment = 'attachment' in streamed.headers.get('content-disposition', '').lower()
                                mime = streamed.headers.get('content-type', '').lower()
                                # Robots and sitemap XML are discovery instructions,
                                # not website documents whose content is audited.
                                metadata_body = discovery_metadata and ('xml' in mime or 'text/plain' in mime or 'gzip' in mime)
                                if (kind in URL_ONLY_TYPES or attachment) and not metadata_body:
                                    if 200 <= streamed.status_code < 300:
                                        self.observe_response(url, streamed.status_code, dict(streamed.headers))
                                        return FetchResult(url, str(streamed.url), streamed.status_code, dict(streamed.headers), b'', url_only=True)
                                    # Keep error/retry handling without downloading a file body.
                                    content = b''
                                else:
                                    length = streamed.headers.get('content-length', '')
                                    if length.isdigit() and int(length) > self.max_response_bytes:
                                        self.observe_response(url, streamed.status_code, dict(streamed.headers))
                                        return FetchResult(url, str(streamed.url), streamed.status_code, dict(streamed.headers), b'',
                                            f'TOO_LARGE: declared body exceeds {self.max_response_bytes} bytes; not downloaded', failure_reason='TOO_LARGE')
                                    buffer = bytearray()
                                    async for chunk in streamed.aiter_bytes(chunk_size=64 * 1024):
                                        if len(buffer) + len(chunk) > self.max_response_bytes:
                                            self.observe_response(url, streamed.status_code, dict(streamed.headers))
                                            return FetchResult(url, str(streamed.url), streamed.status_code, dict(streamed.headers), b'',
                                                f'TOO_LARGE: decoded response exceeds {self.max_response_bytes} bytes; partial body discarded', failure_reason='TOO_LARGE')
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

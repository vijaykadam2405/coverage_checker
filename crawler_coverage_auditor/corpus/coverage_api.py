"""Read-only URL presence and HTML text API, with one shared paced request lane."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from .credentials import load_key
from .errors import CorpusUnavailable


class CoverageAPIClient:
    def __init__(self, base_url: str, *, timeout: float = 60, retries: int = 3,
                 min_interval: float = 2, on_event=None, stopped=None, transport=None):
        parts = urlsplit(base_url)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise CorpusUnavailable('Invalid Corpus API address')
        if parts.scheme != 'https' and parts.hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise CorpusUnavailable('Corpus API requires HTTPS')
        try:
            key = load_key()
        except ValueError as error:
            raise CorpusUnavailable(str(error)) from None
        self.base_url = base_url.rstrip('/')
        self._client = httpx.AsyncClient(base_url=self.base_url + '/', timeout=timeout,
            headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'},
            verify=not (parts.scheme == 'https' and parts.hostname == '187.127.134.12' and parts.port == 8445),
            follow_redirects=False, trust_env=False, transport=transport)
        self._lock = asyncio.Lock()
        self._interval = self._base_interval = max(0, min_interval)
        self._retries = min(4, max(0, retries))
        self._next = 0.0
        self._stopped = stopped or (lambda: False)
        self._on_event = on_event

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self._client.aclose()

    async def _wait(self, seconds: float):
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            if self._stopped():
                raise CorpusUnavailable('Corpus checking stopped; completed checks are saved')
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.2, remaining))

    def _event(self, kind, message):
        if self._on_event:
            self._on_event(kind, message, severity='WARNING' if kind == 'CORPUS_BACKOFF' else 'INFO')

    @staticmethod
    def _retry_after(value: str | None) -> float:
        try:
            return max(0, float(value))
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(value)
                return max(0, (date.astimezone(UTC) - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                return 0

    @staticmethod
    def _identity(url: str) -> tuple:
        parts = urlsplit(url)
        return ((parts.hostname or '').lower().removeprefix('www.'), parts.port,
                parts.path.rstrip('/') or '/', parts.query)

    def _consistent(self, requested, matched):
        try:
            parts = urlsplit(matched)
            return parts.scheme in {'http', 'https'} and bool(parts.hostname) and not parts.username and not parts.password and self._identity(requested) == self._identity(matched)
        except (ValueError, TypeError, AttributeError):
            return False

    async def _post(self, endpoint, body):
        if len(json.dumps(body).encode()) > 2_000_000:
            raise CorpusUnavailable('Corpus API batch exceeds the documented request-size limit')
        async with self._lock:
            for attempt in range(self._retries + 1):
                await self._wait(max(0, self._next - asyncio.get_running_loop().time()))
                started = asyncio.get_running_loop().time()
                try:
                    response = await self._client.post(endpoint, json=body)
                    status = response.status_code
                except httpx.RequestError:
                    response, status = None, 0
                latency = asyncio.get_running_loop().time() - started
                if latency > 3:
                    self._interval = max(self._base_interval, min(12, max(self._interval, latency / 2)))
                self._next = asyncio.get_running_loop().time() + self._interval
                if status == 200:
                    if len(response.content) > 50_000_000:
                        raise CorpusUnavailable('Corpus API response exceeds the safe response-size limit')
                    try:
                        payload = response.json()
                    except ValueError:
                        raise CorpusUnavailable('Corpus API returned invalid JSON; URLs remain unverified') from None
                    if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
                        raise CorpusUnavailable('Corpus API response has no valid results list')
                    if payload.get('error') or payload.get('ok') is False or payload.get('reachable') is False:
                        raise CorpusUnavailable('Corpus API reported an unsuccessful check; URLs remain unverified')
                    self._interval = max(self._base_interval, self._interval * .8) if latency <= 3 else self._interval
                    if attempt:
                        self._event('CORPUS_RECOVERED', 'Corpus API recovered; continuing saved checks')
                    return payload
                if status in {401, 403}:
                    raise CorpusUnavailable(f'Corpus API HTTP {status}: key rejected or required scope unavailable; update the local key/access')
                if status not in {0, 408, 425, 429, 500, 502, 503, 504}:
                    raise CorpusUnavailable(f'Corpus API HTTP {status}: request failed; URLs remain unverified')
                delay = max(5 * 2 ** attempt, self._retry_after(response.headers.get('Retry-After')) if response else 0)
                if attempt >= self._retries or delay > 60:
                    raise CorpusUnavailable(f'Corpus API HTTP {status}: unavailable or overloaded; resume later (retry delay {delay:.0f}s)')
                self._interval = max(self._base_interval, min(12, self._interval * 2))
                self._event('CORPUS_BACKOFF', f'Corpus API HTTP {status}; retry in {delay:.0f}s, only one request in flight')
                await self._wait(delay)

    async def held(self, urls: list[str]) -> dict[str, dict]:
        urls = list(dict.fromkeys(urls))
        if not urls or len(urls) > 500:
            raise ValueError('Corpus API requires 1–500 URLs per batch')
        payload = await self._post('v1/held', {'urls': urls})
        results = {}
        for item in payload['results']:
            if not isinstance(item, dict) or item.get('url') not in urls or item['url'] in results:
                raise CorpusUnavailable('Corpus API returned inconsistent URL results')
            status = item.get('lookup_status')
            if status is None:
                if type(item.get('held')) is not bool:
                    raise CorpusUnavailable('Corpus API returned inconsistent URL results')
                status = 'found' if item['held'] else 'missing'
            if not isinstance(status, str) or status not in {'found', 'missing', 'not_tracked', 'error'}:
                raise CorpusUnavailable('Corpus API returned an unknown lookup status')
            if status in {'error', 'not_tracked'}:
                if item.get('held') is not None and item.get('held') is not False:
                    raise CorpusUnavailable('Corpus API returned contradictory URL results')
            elif type(item.get('held')) is not bool or item['held'] != (status == 'found') or item.get('error'):
                raise CorpusUnavailable('Corpus API returned contradictory URL results')
            if status == 'found':
                if item.get('content_type') is not None and not isinstance(item['content_type'], str):
                    raise CorpusUnavailable('Corpus API returned invalid content-type metadata')
                if not isinstance(item.get('matched_url'), str) or not isinstance(item.get('match'), str) or item['match'] not in {'exact', 'variant'}:
                    raise CorpusUnavailable('Corpus API returned incomplete match evidence')
                if not self._consistent(item['url'], item['matched_url']) or item['match'] == 'exact' and item['url'] != item['matched_url']:
                    raise CorpusUnavailable('Corpus API returned a match for a different URL; needs review')
            results[item['url']] = item | {'lookup_status': status}
        if set(results) != set(urls):
            raise CorpusUnavailable('Corpus API omitted URL results; absence cannot be established')
        return results

    async def text(self, urls: list[str]) -> dict[str, dict]:
        """HTML callers only; no resource downloads or document text requests."""
        urls = list(dict.fromkeys(urls))
        if not urls or len(urls) > 25:
            raise ValueError('Corpus text API requires 1–25 items per batch')
        payload = await self._post('v1/text', {'items': [{'url': url} for url in urls]})
        results = {}
        for item in payload['results']:
            if not isinstance(item, dict) or item.get('requested_url') not in urls or item['requested_url'] in results:
                raise CorpusUnavailable('Corpus text API returned inconsistent URL results')
            if not isinstance(item.get('lookup_status'), str) or item['lookup_status'] not in {'found', 'missing', 'error', 'not_tracked'}:
                raise CorpusUnavailable('Corpus text API returned an unknown lookup status')
            if item['lookup_status'] == 'found':
                if item.get('content_type') is not None and not isinstance(item['content_type'], str):
                    raise CorpusUnavailable('Corpus text API returned invalid content-type metadata')
                if item.get('title') is not None and not isinstance(item['title'], str):
                    raise CorpusUnavailable('Corpus text API returned invalid title metadata')
                if not self._consistent(item['requested_url'], item.get('matched_url')) or item.get('error'):
                    raise CorpusUnavailable('Corpus text API returned contradictory match evidence')
                text = item.get('html_text')
                if not isinstance(text, dict) or type(text.get('truncated')) is not bool or not isinstance(text.get('status'), str):
                    raise CorpusUnavailable('Corpus text API omitted availability/truncation evidence')
                if text.get('text') is not None and not isinstance(text['text'], str):
                    raise CorpusUnavailable('Corpus text API returned invalid stored text')
                if text['status'] in {'available', 'empty'} and not isinstance(text.get('text'), str):
                    raise CorpusUnavailable('Corpus text API omitted the stored text')
                if text['status'] == 'empty' and text['text'].strip():
                    raise CorpusUnavailable('Corpus text API returned contradictory text availability')
            results[item['requested_url']] = item
        if set(results) != set(urls):
            raise CorpusUnavailable('Corpus text API omitted results; text completeness remains unverified')
        return results

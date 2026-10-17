"""Shared conservative fetch policy adapted from the production ZIP's errors.py.

Restrictions are reported, not defeated. Origin-side TLS faults are distinct from
failure to verify the certificate of our own connection.
"""
import math
import re
import socket
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx


def retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    if not value:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        seconds = float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = (parsed - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def quota_delay(headers: dict[str, str], *, now: datetime | None = None) -> float | None:
    """A spent request quota delays the NEXT fetch; it does not invalidate this page."""
    headers = {str(key).lower(): str(value) for key, value in headers.items()}
    structured = headers.get('ratelimit', '')
    spent = any(headers.get(name, '').strip() == '0' for name in
                ('x-ratelimit-remaining', 'ratelimit-remaining', 'x-rate-limit-remaining'))
    spent = spent or bool(re.search(r'(?:^|[;,])\s*r\s*=\s*0(?:\s*[;,]|\s*$)', structured))
    if not spent:
        return None
    now = now or datetime.now(timezone.utc)
    delays = [retry_after(headers.get('retry-after'), now=now)]
    for name in ('ratelimit-reset', 'x-ratelimit-reset', 'x-rate-limit-reset'):
        value = headers.get(name)
        if value:
            try:
                amount = float(value)
                # Common X-RateLimit APIs expose an epoch, RateLimit-Reset a delay.
                delay = amount - now.timestamp() if name.startswith('x-') and amount >= 1_000_000_000 else amount
                if math.isfinite(delay):
                    delays.append(max(0.0, delay))
            except ValueError:
                pass
    match = re.search(r'(?:^|[;,])\s*t\s*=\s*(\d+(?:\.\d+)?)', structured)
    if match:
        delays.append(float(match[1]))
    return max([delay for delay in delays if delay is not None], default=5.0)


def retryable_status(status: int) -> bool:
    return status in {408, 425, 429} or 500 <= status <= 599 and status != 511


def status_reason(status: int) -> str:
    return {408: 'TIMEOUT', 425: 'TOO_EARLY', 429: 'RATE_LIMITED', 451: 'GEO_BLOCKED',
            522: 'ORIGIN_TIMEOUT', 523: 'ORIGIN_UNREACHABLE', 524: 'ORIGIN_TIMEOUT',
            525: 'ORIGIN_TLS_ERROR', 526: 'ORIGIN_TLS_ERROR',
            511: 'NETWORK_LOGIN_REQUIRED'}.get(status, f'HTTP_{status}')


def transport_reason(error: Exception) -> str:
    if isinstance(error, httpx.TooManyRedirects):
        return 'REDIRECT_LOOP'
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return 'TIMEOUT'
    causes, item = [], error
    while item is not None and id(item) not in {id(cause) for cause in causes}:
        causes.append(item)
        item = item.__cause__ or item.__context__
    message = ' '.join(str(cause) for cause in causes).lower()
    if any(isinstance(cause, ssl.SSLError) for cause in causes) or any(marker in message for marker in ('certificate_verify_failed', 'certificate verify failed', 'ssl:', 'tls handshake')):
        return 'SSL_ERROR'
    if any(isinstance(cause, socket.gaierror) for cause in causes) or any(marker in message for marker in ('getaddrinfo', 'name or service not known', 'name_not_resolved', 'nxdomain')):
        return 'DNS_FAILURE'
    if isinstance(error, httpx.ConnectError):
        return 'CONNECTION_FAILURE'
    if isinstance(error, ValueError):
        return 'INVALID_REQUEST'
    return 'FETCH_ERROR'

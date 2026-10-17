"""One-shot reuse of explicitly cacheable browser GET JSON, never file bodies."""

import re
from collections import OrderedDict
from hashlib import sha256
from time import monotonic

SEMANTIC_HEADERS = (
    "accept",
    "accept-language",
    "user-agent",
    "cookie",
    "referer",
    "origin",
)
IGNORED_HEADERS = frozenset(
    {
        "host",
        "connection",
        "accept-encoding",
        "priority",
        "dnt",
        "cache-control",
        "pragma",
        "upgrade-insecure-requests",
    }
)


def request_signature(url, headers):
    values = {name.lower(): value for name, value in headers.items()}
    # Hash session/header values; never persist or log cookies/credentials.
    digest = sha256(
        repr(tuple(values.get(name, "") for name in SEMANTIC_HEADERS)).encode()
    ).hexdigest()
    return url, digest


class ObservedResponses:
    def __init__(self, *, clock=monotonic, ttl=15.0, capacity=8, max_body=1024 * 1024):
        self.clock, self.ttl, self.capacity, self.max_body = (
            clock,
            ttl,
            capacity,
            max_body,
        )
        self.items = OrderedDict()
        self.hits = 0

    def remember(self, response, request_headers):
        headers = {key.lower(): value for key, value in response.headers.items()}
        request_headers = {key.lower(): value for key, value in request_headers.items()}
        cache = headers.get("cache-control", "").lower()
        age = re.search(r"(?:^|,)\s*max-age\s*=\s*(\d+)", cache)
        mime = headers.get("content-type", "").split(";")[0].lower().strip()
        if (
            response.status_code != 200
            or response.error
            or response.url_only
            or response.requested_url != response.final_url
        ):
            return False
        if (
            not (mime in {"application/json", "text/json"} or mime.endswith("+json"))
            or len(response.content) > self.max_body
        ):
            return False
        if (
            not age
            or not int(age[1])
            or any(word in cache for word in ("no-store", "no-cache"))
        ):
            return False
        if (
            "set-cookie" in headers
            or headers.get("vary")
            or "authorization" in request_headers
            or "range" in request_headers
        ):
            return False
        if any(
            name not in SEMANTIC_HEADERS
            and name not in IGNORED_HEADERS
            and not name.startswith(("sec-fetch-", "sec-ch-"))
            for name in request_headers
        ):
            return False  # Unknown application headers can carry filter/session state.
        try:
            remaining = int(age[1]) - max(0, int(headers.get("age", "0")))
        except ValueError:
            return False
        if remaining <= 0:
            return False
        self.items[request_signature(response.requested_url, request_headers)] = (
            self.clock() + min(self.ttl, remaining),
            response,
        )
        while len(self.items) > self.capacity:
            self.items.popitem(last=False)
        return True

    def take(self, url, headers):
        if any(
            name.lower() not in SEMANTIC_HEADERS
            and name.lower() not in IGNORED_HEADERS
            and not name.lower().startswith(("sec-fetch-", "sec-ch-"))
            for name in headers
        ):
            return None
        cached = self.items.pop(request_signature(url, headers), None)
        if cached and cached[0] > self.clock():
            self.hits += 1
            return cached[1]
        return None

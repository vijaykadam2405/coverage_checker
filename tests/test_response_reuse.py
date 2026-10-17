import asyncio
from dataclasses import replace

import httpx
import pytest

from crawler_coverage_auditor.validation.http import FetchResult, PoliteHttpClient
from crawler_coverage_auditor.validation.response_cache import ObservedResponses

URL = "https://example.test/api/articles?page=2"


def response(**headers):
    return FetchResult(
        URL,
        URL,
        200,
        {
            "content-type": "application/json",
            "cache-control": "public,max-age=60",
            **headers,
        },
        b'{"url":"/article"}',
    )


def test_exact_request_reuse_is_one_shot_and_session_query_sensitive():
    cache = ObservedResponses()
    a = response()
    assert cache.remember(a, {"cookie": "session=a", "user-agent": "Browser"})
    assert cache.take(URL, {"cookie": "session=b", "user-agent": "Browser"}) is None
    assert (
        cache.take(
            URL,
            {"cookie": "session=a", "user-agent": "Browser", "x-filter": "different"},
        )
        is None
    )
    assert (
        cache.take(URL + "&year=2024", {"cookie": "session=a", "user-agent": "Browser"})
        is None
    )
    assert cache.take(URL, {"cookie": "session=a", "user-agent": "Browser"}) is a
    assert cache.take(URL, {"cookie": "session=a", "user-agent": "Browser"}) is None


@pytest.mark.parametrize(
    "headers",
    [
        {"cache-control": "no-store"},
        {"cache-control": "public,max-age=0"},
        {"vary": "Accept"},
        {"set-cookie": "session=b"},
        {"content-type": "application/pdf"},
        {"age": "60"},
        {"age": "invalid"},
    ],
)
def test_reuse_rejects_uncacheable_or_non_json_responses(headers):
    assert not ObservedResponses().remember(response(**headers), {})


@pytest.mark.parametrize(
    "headers",
    [{"authorization": "test-only"}, {"range": "bytes=0-2"}, {"x-filter": "year=2024"}],
)
def test_reuse_rejects_unproven_request_context(headers):
    assert not ObservedResponses().remember(response(), headers)


def test_reuse_is_bounded_expires_and_rejects_redirect_or_error():
    now = [0.0]
    cache = ObservedResponses(clock=lambda: now[0], capacity=1)
    assert cache.remember(response(), {})
    now[0] = 16
    assert cache.take(URL, {}) is None
    assert not cache.remember(replace(response(), status_code=403), {})
    assert not cache.remember(replace(response(), final_url=URL + "&redirect=1"), {})
    assert not cache.remember(replace(response(), content=b"x" * (1024 * 1024 + 1)), {})


def test_http_client_reuses_observed_json_without_second_get():
    calls = []

    async def allowed(url):
        return True

    async def run():
        async with PoliteHttpClient(1, 2, 0, "Fixture", allowed=allowed) as client:

            def request(req):
                calls.append(str(req.url))
                return httpx.Response(
                    200,
                    headers={"content-type": "application/json"},
                    json={"url": "/new"},
                )

            client._client._transport = httpx.MockTransport(request)
            original = response()
            assert client.remember_browser_response(
                original, client._equivalent_headers(URL, None)
            )
            assert await client.get(URL) is original
            assert not calls and client.observed_responses.hits == 1
            await client.get(URL)
            assert calls == [URL]

    asyncio.run(run())

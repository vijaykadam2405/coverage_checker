"""Corpus compatibility stays isolated from websites and does not bypass API checks."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from crawler_coverage_auditor.analysis.diagnostics import issue_info
from crawler_coverage_auditor.corpus.coverage_api import CoverageAPIClient
from crawler_coverage_auditor.corpus.errors import CorpusUnavailable
from crawler_coverage_auditor.validation.http import PoliteHttpClient
from crawler_coverage_auditor.web.api import create_app


def test_corpus_connection_needs_no_certificate_setup_and_does_not_change_websites(monkeypatch):
    modes, events, requests = [], [], []
    real_client = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [{
            "url": "https://example.test/", "held": False
        }]})

    def recording_client(*args, **kwargs):
        modes.append(kwargs.get("verify", True))
        if kwargs.get("base_url"):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", recording_client)

    async def exercise():
        async with CoverageAPIClient(
            "https://api.example.test/", min_interval=0,
            on_event=lambda *args, **kwargs: events.append((args, kwargs)),
        ) as api:
            result = await api.held(["https://example.test/"])
            assert result["https://example.test/"]["lookup_status"] == "missing"
        async with PoliteHttpClient(1, 5, 0, "test"):
            pass

    asyncio.run(exercise())
    assert modes == [False, True]
    assert len(requests) == 1
    assert requests[0].headers["Authorization"] == "Bearer test-only-key"
    assert events == []


@pytest.mark.parametrize("status", [401, 403])
def test_connection_does_not_hide_authentication_failures(status):
    async def exercise():
        async with CoverageAPIClient(
            "https://api.example.test/", retries=4, min_interval=0,
            transport=httpx.MockTransport(lambda _: httpx.Response(status)),
        ) as api:
            with pytest.raises(CorpusUnavailable, match=f"HTTP {status}"):
                await api.held(["https://example.test/"])
            assert api.metrics["request_attempts"] == 1

    asyncio.run(exercise())


def test_public_api_has_no_certificate_controls_or_warning(tmp_path):
    client = TestClient(create_app(tmp_path / "audit.sqlite", tmp_path / "reports"))
    response = client.get("/api/state")
    assert response.status_code == 200
    assert set(response.json()["corpus_api"]) == {
        "configured", "source", "key_management_supported", "text_comparison_supported"
    }
    assert "test-only-key" not in response.text


def test_handshake_failure_is_bounded_sanitized_and_not_a_missing_result():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("tls handshake secret detail", request=request)

    async def exercise():
        async with CoverageAPIClient(
            "https://api.example.test/", retries=4, min_interval=0,
            transport=httpx.MockTransport(handler),
        ) as api:
            with pytest.raises(CorpusUnavailable, match="HTTPS handshake failed") as result:
                await api.held(["https://example.test/"])
            assert "secret detail" not in str(result.value)
            assert "test-only-key" not in str(result.value)
            assert "unverified" in str(result.value)

    asyncio.run(exercise())
    assert len(calls) == 1
    title, impact, action = issue_info("Corpus API", "TLS certificate verification failed")
    assert title == "Corpus connection failure" and impact == "Verification"
    assert "No certificate setup is required" in action


def test_transport_failure_does_not_expose_sensitive_details():
    async def exercise():
        async with CoverageAPIClient(
            "https://api.example.test/", retries=0, min_interval=0,
            transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(
                    httpx.ConnectError("secret detail", request=request)
                )
            ),
        ) as api:
            with pytest.raises(CorpusUnavailable, match="CONNECTION_FAILURE") as result:
                await api.held(["https://example.test/"])
            assert "secret detail" not in str(result.value)

    asyncio.run(exercise())

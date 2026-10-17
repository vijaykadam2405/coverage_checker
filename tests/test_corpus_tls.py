import asyncio
import ssl
from pathlib import Path

import certifi
import httpx
import pytest

from crawler_coverage_auditor.analysis.diagnostics import issue_info
from crawler_coverage_auditor.corpus import tls
from crawler_coverage_auditor.corpus.coverage_api import CoverageAPIClient
from crawler_coverage_auditor.corpus.errors import CorpusUnavailable


def test_default_verification_cannot_be_disabled_by_legacy_environment(monkeypatch):
    monkeypatch.setenv("CRAWLSCOPE_CORPUS_VERIFY_TLS", "false")
    assert tls.verification_context() is True


def test_trusted_bundle_adds_trust_without_disabling_verification(monkeypatch):
    monkeypatch.setenv("CRAWLSCOPE_CORPUS_CA_BUNDLE", certifi.where())
    context = tls.verification_context()
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    assert context.cert_store_stats()["x509_ca"] > 0


def test_relative_bundle_resolves_against_project_not_working_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(tls, "ROOT", Path(certifi.where()).parent)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRAWLSCOPE_CORPUS_CA_BUNDLE", Path(certifi.where()).name)
    assert tls.verification_context().check_hostname


@pytest.mark.parametrize("contents", [None, "not a certificate"])
def test_invalid_bundle_fails_closed_without_exposing_contents(
    tmp_path, monkeypatch, contents
):
    bundle = tmp_path / "ca.pem"
    if contents is not None:
        bundle.write_text(contents)
    monkeypatch.setenv("CRAWLSCOPE_CORPUS_CA_BUNDLE", str(bundle))
    with pytest.raises(
        CorpusUnavailable, match="CA bundle could not be loaded"
    ) as result:
        tls.verification_context()
    assert str(bundle) not in str(result.value)


def test_tls_failure_is_not_retried_and_is_not_a_missing_result():
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError(
            "certificate_verify_failed secret server detail", request=request
        )

    async def exercise():
        async with CoverageAPIClient(
            "https://api.example.test/",
            retries=4,
            min_interval=0,
            transport=httpx.MockTransport(handler),
        ) as api:
            with pytest.raises(
                CorpusUnavailable, match="TLS certificate verification failed"
            ) as result:
                await api.held(["https://example.test/"])
            assert "secret server detail" not in str(result.value)
            assert "test-only-key" not in str(result.value)

    asyncio.run(exercise())
    assert len(calls) == 1
    title, impact, _ = issue_info(
        "Corpus API", "Corpus API TLS certificate verification failed"
    )
    assert (
        title == "Corpus certificate could not be verified" and impact == "Verification"
    )


def test_transport_failure_reports_reason_without_sensitive_exception_details():
    async def exercise():
        async with CoverageAPIClient(
            "https://api.example.test/",
            retries=0,
            min_interval=0,
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

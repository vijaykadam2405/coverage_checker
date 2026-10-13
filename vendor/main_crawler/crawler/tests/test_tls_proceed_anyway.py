"""`Job.tls_insecure_hosts` — the honest "Advanced -> Proceed anyway".

The whole value of this feature is that it CANNOT be reached by accident, so most of what is
pinned here is what it refuses to do. Network tests are marked and skipped by default; the
default-off and SSRF assertions are pure and always run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import edgecase, errors  # noqa: E402
from crawler.fetcher import Fetcher  # noqa: E402
from crawler.models import Job  # noqa: E402

UA = "Mozilla/5.0 (edgecase-tests)"
LIVE = os.environ.get("CRAWLER_LIVE_TESTS") == "1"


def _f(hosts=None) -> Fetcher:
    return Fetcher(user_agent=UA, timeout_s=15, delay_s=0, max_retries=0,
                   respect_robots=False, tls_insecure_hosts=hosts)


def test_default_is_off_everywhere():
    """A job that names no host must leave no way for any fetch to skip verification."""
    assert Job(job_id="j", job_type="profile",
                  seed_urls=["https://x.test/"]).tls_insecure_hosts == []
    assert _f().tls_insecure_hosts == set()
    assert _f([]).tls_insecure_hosts == set()
    assert _f(["", "  "]).tls_insecure_hosts == set()


def test_host_list_is_normalised_not_matched_loosely():
    """Per-ORIGIN, like a browser's stored exception. A listed host must not admit its neighbours."""
    f = _f(["www.Expired.BadSSL.com ", "self-signed.badssl.com."])
    assert f.tls_insecure_hosts == {"expired.badssl.com", "self-signed.badssl.com"}
    # Not a suffix match: listing one host must never trust a subdomain or a lookalike.
    assert "evil-expired.badssl.com" not in f.tls_insecure_hosts
    assert "a.expired.badssl.com" not in f.tls_insecure_hosts


def test_ssrf_guard_still_refuses_the_internal_targets():
    """The allowlist is checked AFTER the internal-address guard, so listing an internal host buys
    nothing. This asserts the guard the retry re-runs, on the exact literals it must catch."""
    for u in ("http://169.254.169.254/latest/meta-data/",
              "http://metadata.google.internal/computeMetadata/v1/",
              "http://127.0.0.1:8099/v1/metrics",
              "http://10.0.0.5/admin",
              "http://[fd00:ec2::254]/latest/"):
        assert edgecase.private_target(u), f"SSRF guard missed {u}"


def test_an_unverified_page_is_never_reported_as_a_plain_success():
    """The point of the flag is that the result stays distinguishable. TLS_UNVERIFIED is a
    recovered-by tag, not an error_category, so it must not fall into either failure bucket."""
    assert errors.TLS_UNVERIFIED not in errors._TERMINAL_REASONS
    assert errors.TLS_UNVERIFIED not in errors._RETRYABLE_REASONS
    assert errors.TLS_UNVERIFIED != "c1_live"


@pytest.mark.skipif(not LIVE, reason="network; set CRAWLER_LIVE_TESTS=1")
def test_live_expired_cert_fails_closed_then_opens_only_for_the_listed_host():
    """The behavioural pair, against badssl.com's deliberately-broken certs.

    Without the opt-in an expired cert is an SSL failure and stays one. With the host listed, the
    SAME url comes back with content AND with insecure_tls carrying the original error — so nothing
    downstream can mistake it for a trusted fetch."""
    url = "https://expired.badssl.com/"
    closed = _f()._http_fetch(url, None)
    assert closed.status is None and closed.reason == errors.SSL, closed.reason
    assert closed.insecure_tls is None

    opened = _f(["expired.badssl.com"])._http_fetch(url, None)
    assert opened.status == 200, f"proceed-anyway did not retrieve the page: {opened.error}"
    assert opened.insecure_tls, "an unverified fetch must carry the cert error it clicked through"
    assert "certificate" in opened.insecure_tls.lower()
    assert opened.error is None, "an untrusted SUCCESS must not also be reported as a failure"

    # A different broken host, not listed, still fails closed while the first one is allowed.
    other = _f(["expired.badssl.com"])._http_fetch("https://self-signed.badssl.com/", None)
    assert other.status is None and other.reason == errors.SSL

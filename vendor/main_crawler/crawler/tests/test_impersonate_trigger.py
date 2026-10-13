"""The cheap JA3 tier has to be REACHED, or shipping it changes nothing.

The engine carries curl_cffi real-Chrome impersonation and only tried it on 401/403. Measured from
the data centre against catalogued manufacturers that had been sitting at ZERO pages, curl_cffi
cleared every one of these — and the engine never reached the tier, purely because of the status:

    thalesgroup.com       200 + a 212-byte shell   ->  146,278 bytes
    siatt.com.br          429                      ->   17,526
    xads.com              454                      ->   10,533
    kalashnikovgroup.ru   403                      ->   86,905
    projectq.ai           403                      ->  409,379
    rada.com              403                      ->   95,398

Three sites blocked by a status code the branch did not list, on a capability we already had.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import async_engine as ae


class _FR:
    """Enough FetchResult for the two predicates under test."""

    def __init__(self, status=200, html="", error=None, ctype="text/html"):
        self.status, self.text_html, self.error = status, html, error
        self.url = self.final_url = "https://x.com/"
        self._ctype = ctype

    def is_html(self):
        return "html" in self._ctype


# ── which statuses reach the tier ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [401, 403, 429, 451, 454, 456])
def test_a_waf_refusal_status_reaches_the_ja3_tier(status):
    """429 (siatt.com.br) and 454 (xads.com) are refusals, not resource problems. Both cleared
    under JA3; neither was in the original 401/403 set."""
    assert status in ae._IMPERSONATE_STATUSES


@pytest.mark.parametrize("status", [200, 301, 404, 410, 500, 502, 503])
def test_a_status_describing_the_RESOURCE_does_not(status):
    """404/410 mean the page is gone and 5xx means the server is broken — re-fetching with a
    different TLS fingerprint cannot change either, and trying wastes a request on every dead URL
    in a large crawl."""
    assert status not in ae._IMPERSONATE_STATUSES


# ── the thin-shell case ───────────────────────────────────────────────────────────────────────

def test_a_200_that_is_really_a_shell_is_worth_a_ja3_get():
    """thalesgroup.com: a 212-byte 200 to plain httpx, 146 KB to curl_cffi. One request beats a
    browser tab's 10-90 seconds."""
    assert ae._looks_thin(_FR(200, "<html><body>" + "x" * 200 + "</body></html>"))


def test_a_real_page_is_not_thin():
    assert not ae._looks_thin(_FR(200, "<html>" + "x" * 50_000 + "</html>"))


def test_a_non_200_is_not_judged_on_size():
    """Errors are handled by the status set above; judging them on size too would double-fire."""
    assert not ae._looks_thin(_FR(403, "x" * 10))


def test_a_binary_response_is_never_thin():
    """A small PDF or image is complete, not a shell — httpx already holds the bytes."""
    assert not ae._looks_thin(_FR(200, "", ctype="application/pdf"))


def test_an_errored_fetch_is_not_thin():
    assert not ae._looks_thin(_FR(200, "", error="timeout"))


# ── a TLS refusal never becomes a status ──────────────────────────────────────────────────────

def test_a_transport_level_block_reaches_the_tier():
    """THE one the first version missed, caught by re-crawling and getting nothing.

    A TLS-level refusal kills the handshake; httpx raises and there IS no status, so a
    status-keyed trigger misses exactly the blocks it exists to clear. Measured from the data
    centre: plain httpx could not complete a request to kalashnikovgroup.ru or projectq.ai at all,
    while curl_cffi returned 86,905 and 409,379 bytes."""
    assert ae._transport_blocked(_FR(status=None, error="SSLError: record layer failure"))
    assert ae._transport_blocked(_FR(status=None, error="ConnectError: connection reset"))


@pytest.mark.parametrize("err", [
    "getaddrinfo failed",
    "[Errno -2] Name or service not known",
    "Temporary failure in name resolution",
])
def test_a_DNS_failure_does_not(err):
    """No TLS fingerprint resurrects a host with no A record. Firing here would add a wasted
    request to every dead domain in the catalogue — and this catalogue has dozens."""
    assert not ae._transport_blocked(_FR(status=None, error=err))


def test_a_response_with_a_status_is_judged_on_the_status_not_the_error():
    """Otherwise the two paths double-fire on the same fetch."""
    assert not ae._transport_blocked(_FR(status=403, error="whatever"))


def test_a_clean_success_is_not_a_transport_block():
    assert not ae._transport_blocked(_FR(status=200, html="x" * 9000))


# ── wiring: the branch must actually use them ─────────────────────────────────────────────────

def test_the_engine_gates_the_tier_on_both_conditions():
    """A constant nothing reads is not a fix."""
    src = Path(ae.__file__).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert "_IMPERSONATE_STATUSES or _looks_thin(" in code, \
        "the JA3 tier is not gated on the widened status/shell condition"
    assert "_transport_blocked(http_fr)" in code, \
        "a TLS refusal still cannot reach the tier"
    assert "http_fr.status in (401, 403)" not in code, "the narrow check is still live"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

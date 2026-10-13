"""A transport-level refusal must reach the Chrome-JA3 fallback, on BOTH lanes.

WHY THIS EXISTS — learned from a competitor, not from theory. Benchmarked 2026-08-18 against
Scrapling on 30 tender portals: contrataciondelestado.es (es-pcsp) gave it 55 pages in 57s and
gave us `conn_refused` with nothing stored. Scrapling has no cleverer bypass for that host; its
stealth session simply presents a real browser TLS stack on the first byte, while we lead with
plain httpx and escalate to curl_cffi's Chrome JA3 only on a STATUS CODE.

A refused connection has no status code. The httpx lane already knew that — it routes on
`_transport_blocked()` — but the render lane gated its only impersonate call on
`400 <= status < 500`, and render-first is precisely where the careful / always-render hosts
live. So the one class of host that most needs a different TLS fingerprint was the one class
that could never get one.

    python -m pytest tests/test_transport_block_impersonate.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crawler.async_engine import _DNS_ERR, _transport_blocked  # noqa: E402
from crawler.fetcher import FetchResult  # noqa: E402


def _fr(status=None, error=None):
    return FetchResult(url="https://x.test/", final_url="https://x.test/", status=status,
                       fetched_at="2026-08-18T00:00:00Z", error=error)


def test_connection_refused_is_impersonate_worthy():
    """The es-pcsp shape: no status, a socket-level refusal."""
    assert _transport_blocked(_fr(error="conn_refused")) is True
    assert _transport_blocked(_fr(error="[Errno 111] Connection refused")) is True
    assert _transport_blocked(_fr(error="TLSV1_ALERT_INTERNAL_ERROR")) is True
    assert _transport_blocked(_fr(error="Server disconnected without sending a response")) is True


def test_a_dns_failure_is_not_a_fingerprint_problem():
    """A host that does not resolve will not resolve for curl_cffi either. Spending the extra
    request there is waste, and worse, it would mask a genuinely dead source as 'blocked'."""
    for err in _DNS_ERR:
        assert _transport_blocked(_fr(error=f"httpx: {err}")) is False


def test_a_real_http_answer_is_not_a_transport_block():
    """Anything that ANSWERED — even with a 403 — is handled by the status-code branch. This
    guard must not double-fire on it."""
    assert _transport_blocked(_fr(status=403, error="Forbidden")) is False
    assert _transport_blocked(_fr(status=200)) is False
    assert _transport_blocked(_fr()) is False          # no status, no error: nothing to act on
    assert _transport_blocked(None) is False


def test_render_lane_routes_transport_blocks_to_impersonate():
    """The regression itself: the render lane must consult _transport_blocked, not only 4xx.

    Asserted against the source because the surrounding call needs a live browser tab. If the
    branch is ever narrowed back to a status test, this fails.
    """
    src = (Path(__file__).resolve().parents[1] / "crawler" / "async_engine.py").read_text(
        encoding="utf-8")
    i = src.index("A TRANSPORT-LEVEL REFUSAL NEVER HAD A STATUS TO GATE ON")
    j = src.index("if status and 400 <= status < 500:", i)
    branch = src[i:j]
    assert "if _transport_blocked(fr):" in branch, "render lane no longer routes on transport"
    assert "_impersonate_try" in branch, "render lane no longer reaches the JA3 fallback"

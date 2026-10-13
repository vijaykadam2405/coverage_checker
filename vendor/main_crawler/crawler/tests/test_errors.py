"""Unit tests for the crawl-error taxonomy (crawler/errors.py) — pure, offline."""
from crawler import errors


def test_classify_failure_status():
    assert errors.classify_failure(404, None) == "http_404"
    assert errors.classify_failure(503, None) == "http_503"
    assert errors.classify_failure(200, None) is None      # success
    assert errors.classify_failure(301, None) is None


def test_classify_failure_network():
    cases = {
        "getaddrinfo failed": errors.DNS,
        "net::ERR_NAME_NOT_RESOLVED at https://x": errors.DNS,
        "net::ERR_CERT_AUTHORITY_INVALID": errors.SSL,
        "SSL: CERTIFICATE_VERIFY_FAILED": errors.SSL,
        "Connection refused": errors.CONN_REFUSED,
        "net::ERR_CONNECTION_REFUSED": errors.CONN_REFUSED,
        "Timeout 30000ms exceeded": errors.TIMEOUT,
        "net::ERR_TIMED_OUT": errors.TIMEOUT,
        "render_failed: Page crashed": errors.RENDER_CRASH,
        "nav:some playwright thing": errors.RENDER_CRASH,
        "something totally unknown": errors.OTHER,
    }
    for text, want in cases.items():
        assert errors.classify_failure(None, text) == want, text


def test_classify_failure_robots():
    assert errors.classify_failure(None, "blocked_by_robots") == errors.ROBOTS


def test_policy_table():
    assert errors.policy_for_status(403) == errors.DISGUISE
    assert errors.policy_for_status(404) == errors.GONE
    assert errors.policy_for_status(410) == errors.GONE
    assert errors.policy_for_status(429) == errors.COOLDOWN
    assert errors.policy_for_status(503) == errors.COOLDOWN
    assert errors.policy_for_status(500) == errors.RETRY_LATER
    assert errors.policy_for_status(418) == errors.DROP        # unknown 4xx
    assert errors.policy_for_status(599) == errors.RETRY_LATER  # unknown 5xx


def test_parse_retry_after():
    assert errors.parse_retry_after("120") == 120.0
    assert errors.parse_retry_after(None) is None
    assert errors.parse_retry_after("garbage") is None
    assert errors.parse_retry_after("99999999") == 300.0       # clamped to cap
    # Non-positive => None, so the caller's `or <default>` supplies a real cooldown.
    #
    # "0" used to return 0.0, which every caller's `or` already rescued. A NEGATIVE did not: -5.0 is
    # truthy, so it went through as the cooldown and put the host's retry time in the PAST, turning
    # a hard block into a hot retry loop. Real blockers send this — harborfreight.com answers a
    # permanent PerimeterX block with `Retry-After: 0`. A server refusing us while telling us to
    # come back immediately is not giving us information worth obeying.
    assert errors.parse_retry_after("0") is None
    assert errors.parse_retry_after("-5") is None
    assert errors.parse_retry_after("-999999") is None
    assert (errors.parse_retry_after("-5") or 30.0) == 30.0    # the shape every caller uses
    # HTTP-date form parses to a non-negative float
    v = errors.parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
    assert v is not None and 0.0 <= v <= 300.0


def test_is_careful_host():
    assert errors.is_careful_host("epa.gov")
    assert errors.is_careful_host("www.ddpmod.gov.in")
    assert errors.is_careful_host("defence.mod.gov.uk")
    assert errors.is_careful_host("army.mil")
    assert not errors.is_careful_host("govtrack.us")          # substring, not a segment
    assert not errors.is_careful_host("email.com")
    assert not errors.is_careful_host("navalnews.com")
    assert not errors.is_careful_host(None)


def test_looks_like_trap():
    assert errors.looks_like_trap("https://x.com/" + "a" * 2100) == errors.TRAP  # too long
    assert errors.looks_like_trap("https://x.com/a/b/a/b/a/b/x") == errors.TRAP  # segment loop
    assert errors.looks_like_trap("https://x.com/" + "/".join(str(i) for i in range(15))) == errors.TRAP
    assert errors.looks_like_trap("https://x.com/news/article-123") is None      # normal
    assert errors.looks_like_trap("https://x.com/") is None

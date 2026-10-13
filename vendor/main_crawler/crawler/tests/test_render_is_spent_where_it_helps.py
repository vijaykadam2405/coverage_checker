"""A render is the scarcest thing the crawler has. Spend it only where a browser can help.

MEASURED 2026-08-05, and the reason this rule exists. `ps` reports a LIFETIME AVERAGE and is
useless for this (three consecutive samples returned an identical 303%); cgroup `usage_usec` deltas
are the real measurement:

    whole box   40.58 of 40 cores   — saturated, 39.9 of it the 12 crawler containers
    one instance 4.97 cores with render_lane_used = 4   ->  ~1.2 cores per concurrent render

The data centre has no GPU, so every render rasterizes through llvmpipe in software. 40 cores is
therefore ~33 concurrent renders for the WHOLE machine; the fleet already admits 48, the adaptive
governor pins render_limit to 4, and render_q stood at 104,040 fleet-wide. At that price a render
spent confirming a 429 is a render a real page waits behind.

So the rule is about what a browser can actually FIX:

  * 403 -> DISGUISE. "The origin objects to who we appear to be." A browser is the answer; this is
    what C3/C4 are for. RENDERS.
  * 429/503 -> COOLDOWN, and 408/425/500/502/504 -> RETRY_LATER. Both said "ask again later". A
    browser creates neither quota nor a working origin. DOES NOT RENDER.
  * 404/410/451 -> GONE. Already excluded; no browser makes a deleted page exist.
  * transport errors -> still render: a reset or handshake failure can be a TLS/JA3 block that a
    real browser clears.
"""
from __future__ import annotations

import pytest

from crawler import async_engine as ae
from crawler import errors


class _FR:
    def __init__(self, status=200, error=None, html="<html><body>x</body></html>",
                 ctype="text/html"):
        self.status, self.error, self.text_html = status, error, html
        self.content_type, self.url, self.final_url = ctype, "https://h.test/p", "https://h.test/p"

    def is_html(self):
        return (self.content_type or "").startswith("text/html")


def test_a_rate_limit_does_not_buy_a_browser():
    assert not ae._needs_render(_FR(status=429)), (
        "429 is a quota statement; a browser does not create quota")


def test_503_STILL_RENDERS_even_though_it_shares_429s_bucket():
    """The counterexample that stopped this rule being wrong. 503 and 429 are both COOLDOWN, so
    taking the bucket wholesale looked clean — but 503 is the classic WAF-challenge status, an
    interstitial a browser genuinely clears. Excluding it would have closed the Cloudflare door
    that C3/C4 exist to open. test_gone_is_not_blocked guarded this and was right to."""
    assert errors.policy_for_status(503) == errors.COOLDOWN
    assert errors.policy_for_status(429) == errors.COOLDOWN
    assert ae._needs_render(_FR(status=503))


@pytest.mark.parametrize("status", [408, 425, 500, 502, 504])
def test_a_server_saying_later_does_not_buy_a_browser(status):
    assert errors.policy_for_status(status) == errors.RETRY_LATER
    assert not ae._needs_render(_FR(status=status))


def test_a_403_still_renders():
    """The one failure a browser genuinely answers — and the whole reason the C3/C4 tiers exist.
    Narrowing the render budget must not close this door."""
    assert errors.policy_for_status(403) == errors.DISGUISE
    assert ae._needs_render(_FR(status=403))


@pytest.mark.parametrize("status", [404, 410, 451])
def test_a_gone_page_still_does_not_render(status):
    assert not ae._needs_render(_FR(status=status))


@pytest.mark.parametrize("err", ["timeout", "connect", "reset"])
def test_a_transport_failure_still_renders(err):
    """A reset or handshake failure can be a TLS/JA3 block, which a real browser clears. Kept
    deliberately: this is the cheap-tier-to-browser path, not a server saying 'later'."""
    assert ae._needs_render(_FR(status=None, error=err))


def test_a_healthy_page_still_does_not_render():
    html = ("<html><body>" + "<p>%s</p>" % ("real editorial copy. " * 80) +
            "".join('<a href="/a%d">l</a>' % i for i in range(25)) + "</body></html>")
    assert not ae._needs_render(_FR(html=html))


def test_a_thin_shell_still_renders():
    assert ae._needs_render(
        _FR(html='<html><body><div id="root"></div><script src="/a.js"></script></body></html>'))


def test_the_rule_reads_the_policy_table_rather_than_listing_codes():
    """Status->meaning belongs to errors._STATUS_POLICY. A second hand-written list of codes here
    would drift the moment one is reclassified — 408 and 425 were both reclassified once already."""
    import inspect

    src = inspect.getsource(ae._needs_render)
    assert "policy_for_status" in src
    # Comments and the docstring NAME these codes on purpose — that is the explanation, and the
    # first version of this test failed on its own prose. Only executable lines are the subject.
    code = "\n".join(l.split("#", 1)[0] for l in src.splitlines() if not l.strip().startswith("#"))
    code = code.split('"""')[0] + '"""'.join(code.split('"""')[2:])   # drop the docstring
    # 429 is the ONE deliberate exception, named because its bucket-mate 503 must behave differently
    # — see test_503_STILL_RENDERS_even_though_it_shares_429s_bucket.
    for literal in ("503", "500", "502", "504", "408", "425"):
        assert literal not in code, (
            "%s is hard-coded; read it from the policy table instead" % literal)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))

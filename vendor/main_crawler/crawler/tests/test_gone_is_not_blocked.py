"""A page that is GONE must not be sent to a browser.

_needs_render escalated on `status >= 400`, which is right for a WAF block and wrong for a 404: no
browser makes a deleted page exist. errors._STATUS_POLICY already says this about 451 — "No retry,
no proxy and no browser changes a legal takedown" — and it is equally true of 404 and 410.

MEASURED: news article URLs pulled straight from the live frontiers came back 404 and were queued
for rendering anyway. The manufacturer corpus alone holds 20,937 http_404 URLs; at ~15s a render
that is roughly 80 browser-hours spent confirming pages are gone, while real pages wait behind them
in a render queue 18,066 deep.
"""
from __future__ import annotations

import pytest

from crawler import async_engine as ae, errors


class _FR:
    def __init__(self, status=200, error=None, kind="html", html="<html><body>" + "x" * 900 +
                 "<a href='/a'>a</a><a href='/b'>b</a><a href='/c'>c</a></body></html>"):
        self.status, self.error, self.kind = status, error, kind
        self.text_html, self.url, self.final_url = html, "https://x.test/p", "https://x.test/p"

    def is_html(self):
        return self.kind == "html"


@pytest.mark.parametrize("code", [404, 410, 451])
def test_a_gone_page_is_not_rendered(code):
    assert errors.policy_for_status(code) == errors.GONE, "policy changed — re-check this test"
    assert ae._needs_render(_FR(status=code)) is False, (
        "%s is definitive; a browser cannot make the page exist" % code)


@pytest.mark.parametrize("code", [403, 401, 503])
def test_a_BLOCKED_or_broken_page_is_still_rendered(code):
    """The escalation must keep working for everything a browser genuinely might clear — that is
    what it is for, and narrowing it too far would be the worse regression.

    This guard did its job: a later change excluded the whole COOLDOWN bucket to save render
    capacity, which would have taken 503 — the classic WAF-challenge status, an interstitial a
    browser genuinely clears — along with 429. 503 stays here.

    429 and the RETRY_LATER codes (408/425/500/502/504) moved OUT deliberately, on a measurement
    this test predates: a render costs ~1.2 cores because the data centre has no GPU, so the box
    serves ~33 concurrent renders and the fleet was queueing 104,040. A server that says "ask again
    later" is answered by asking again later, not by a browser. See
    test_render_is_spent_where_it_helps.py.
    """
    assert ae._needs_render(_FR(status=code)) is True


def test_a_transport_failure_is_still_rendered():
    assert ae._needs_render(_FR(status=None, error="timeout")) is True
    assert ae._needs_render(None) is True


def test_a_healthy_page_is_still_served_by_httpx():
    assert ae._needs_render(_FR(status=200)) is False


def test_a_thin_shell_is_still_rendered():
    assert ae._needs_render(_FR(status=200, html="<html><body><div id='root'></div></body></html>")) is True

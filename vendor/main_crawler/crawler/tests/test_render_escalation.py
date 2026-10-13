"""A host must not be marked always-render because it had a bad minute.

MEASURED: of 105 hosts on the always-render list, 91 answer a plain httpx GET with HTTP 200 and
real content today — General Dynamics, Fincantieri, Embraer, GRSE among them. Every page on those
sites was being pushed through a browser, forever, and that is what filled the render lane with
4,514 queued items draining 5 at a time.

Cause: escalation fired whenever "httpx wasn't enough", and _needs_render counts a failure as not
enough. A 403, a timeout and a JS shell were identical evidence. _note_render early-returns for a
host already in the set, so nothing ever re-tested it — one bad minute, browsers forever.
"""
from __future__ import annotations

import pytest

from crawler import async_engine as ae


class _FR:
    def __init__(self, status=200, error=None, kind="html", html="<html><body>hi</body></html>"):
        self.status, self.error, self.kind = status, error, kind
        self.text_html, self.url, self.final_url = html, "https://x.test/", "https://x.test/"

    def is_html(self):
        return self.kind == "html"


@pytest.mark.parametrize("fr,why", [
    (_FR(status=403), "a WAF block is not evidence about JavaScript"),
    (_FR(status=429), "a rate-limit is not evidence about JavaScript"),
    (_FR(status=500), "an origin error is not evidence about JavaScript"),
    (_FR(status=None, error="timeout"), "a timeout is not evidence about JavaScript"),
    (_FR(status=None, error="ssl"), "a TLS refusal is not evidence about JavaScript"),
    (None, "no result at all is not evidence about JavaScript"),
])
def test_failures_do_not_count_as_a_js_signal(fr, why):
    assert ae._is_js_shell_signal(fr) is False, why


def test_a_real_page_that_is_a_shell_DOES_count():
    """The genuine case the escalation exists for: fetched fine, but the body is an app shell."""
    assert ae._is_js_shell_signal(_FR(status=200)) is True


def test_a_non_html_body_is_not_a_js_signal():
    """A PDF served over httpx is not a JavaScript site; the bytes are already in hand."""
    assert ae._is_js_shell_signal(_FR(status=200, kind="pdf")) is False


def test_the_escalation_site_is_gated_on_it():
    """Guards the wiring: _needs_render still (correctly) treats a failure as 'try a browser' for
    THIS page. The permanent per-host mark must not share that signal."""
    import inspect
    src = inspect.getsource(ae)
    i = src.find("self._note_render(host, True)")
    assert i > 0, "the escalation site moved"
    assert "_is_js_shell_signal" in src[max(0, i - 700):i], (
        "always-render escalation is not gated on a real JS-shell signal")


def test_needs_render_still_treats_a_failure_as_render_worthy():
    """The two must stay different. Collapsing them the other way would stop a blocked page from
    ever trying a browser, which is a worse regression."""
    assert ae._needs_render(_FR(status=403)) is True
    assert ae._needs_render(_FR(status=None, error="timeout")) is True


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))

"""A host earns a PERMANENT always-render mark only on evidence it needs JavaScript.

Companion to test_render_escalation.py, which pins the other half: that a FAILURE (403, timeout,
429) is not evidence about JavaScript. This file pins the half that regressed.

MEASURED. The first version of _is_js_shell_signal ended `return fr.is_html()` — true of every
successful HTML page — so every ordinary page escalated its host and the always-render set grew
from 105 hosts to 329 in a day. Nothing caught it: the function's own docstring described the
thin-shell test correctly, the escalation is permanent and silent, and the symptom shows up only
as a render queue that will not drain.

`_note_render` returns early for a host already in the set, so a wrong mark is never re-tested:
one bad page, browsers forever.
"""
from __future__ import annotations

import pytest

from crawler import async_engine as ae


class _FR:
    """Enough FetchResult for the predicate: it reads status/error/is_html/text_html/final_url."""

    def __init__(self, html, status=200, error=None, ctype="text/html"):
        self.text_html, self.status, self.error = html, status, error
        self.content_type, self.url, self.final_url = ctype, "https://h.test/p", "https://h.test/p"

    def is_html(self):
        return (self.content_type or "").startswith("text/html")


_REAL_PAGE = ("<html><body>" + "<p>%s</p>" % ("real editorial copy. " * 80) +
              "".join('<a href="/a%d">link %d</a>' % (i, i) for i in range(25)) +
              "</body></html>")
_SHELL = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'


def test_an_ordinary_html_page_does_NOT_escalate_its_host():
    """The regression, stated directly. This is the common case — most pages on most sites."""
    assert not ae._is_js_shell_signal(_FR(_REAL_PAGE)), (
        "a healthy page with real text and 25 links marked its host always-render")


def test_a_thin_link_less_shell_DOES_escalate():
    assert ae._is_js_shell_signal(_FR(_SHELL))


def test_an_empty_body_escalates():
    assert ae._is_js_shell_signal(_FR("   "))


@pytest.mark.parametrize("fr", [
    _FR(_SHELL, status=403),                       # blocked — a browser may clear it, but that is
    _FR(_SHELL, status=429),                       # not evidence the SITE needs JavaScript
    _FR(_SHELL, status=500),
    _FR(None, status=None, error="timeout"),
    _FR(_SHELL, status=None, error="connect"),
])
def test_a_failure_is_never_evidence_about_javascript(fr):
    assert not ae._is_js_shell_signal(fr)


def test_a_pdf_never_escalates():
    assert not ae._is_js_shell_signal(_FR(None, ctype="application/pdf"))


def test_none_is_not_evidence():
    assert not ae._is_js_shell_signal(None)


def test_the_shell_test_is_not_duplicated():
    """Two copies of 'what a JS shell looks like' drift, and the drift is invisible: the escalation
    is permanent, silent, and only shows up as a render queue that will not drain. So the predicate
    must DELEGATE to _needs_render rather than re-implement its thresholds."""
    import inspect

    src = inspect.getsource(ae._is_js_shell_signal)
    assert "_needs_render(fr)" in src, "the escalation test grew its own copy of the shell heuristic"
    for threshold in ("THIN_TEXT_CHARS", "_SPA_MARKERS", "visible_text", "extract_links"):
        assert threshold not in src, "%s is _needs_render's to own, not this predicate's" % threshold


def test_it_agrees_with_needs_render_on_successful_pages():
    """On a page that fetched fine, the two must give the same answer — that IS the contract."""
    for html in (_REAL_PAGE, _SHELL, "   ", "<html><body>tiny</body></html>"):
        fr = _FR(html)
        assert ae._is_js_shell_signal(fr) == ae._needs_render(fr), html[:40]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))

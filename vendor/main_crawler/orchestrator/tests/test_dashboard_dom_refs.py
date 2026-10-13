"""No JS may dereference a DOM element the markup does not contain.

MEASURED, and it is why this file exists. Removing the Quiet-alarms panel left renderAlarms()
doing `$("#alarms").innerHTML = ...` on a null. That threw, and everything called AFTER it in the
same render pass silently never ran — taking the HARDWARE tiles off the page. The dashboard showed
no error: just an empty panel where the machine stats used to be.

`node --check` cannot see this; it is a runtime null, not a syntax error. The earlier test only
checked that the REMOVED ids were absent from the markup, which they were — the bug was in the code
that still looked for them.
"""
from __future__ import annotations

import re
from pathlib import Path

from mallory_orchestrator import api

HTML = (Path(api.__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
IDS = set(re.findall(r'id="([A-Za-z0-9_-]+)"', HTML))


def _enclosing(text: str, pos: int) -> str:
    """Source of the `function ...` block containing pos — enough to spot an early return."""
    start = text.rfind("\nfunction ", 0, pos)
    if start == -1:
        start = max(0, pos - 4000)
    end = text.find("\nfunction ", pos)
    return text[start:end if end != -1 else len(text)]


def test_no_unguarded_dereference_of_a_missing_element():
    """A function that outlives its panel is allowed to exist — renderAlarms' checks are cheap and
    still wanted. It simply has to bail out when its element is gone, instead of throwing and
    taking the rest of the render pass with it."""
    bad = []
    for m in re.finditer(r'\$\("#([A-Za-z0-9_-]+)"\)\s*\.', HTML):
        name = m.group(1)
        if name in IDS:
            continue
        if 'if(!$("#%s")) return;' % name in _enclosing(HTML, m.start()):
            continue
        bad.append((HTML[:m.start()].count("\n") + 1, name))
    assert not bad, (
        "unguarded: these throw at runtime and kill every render that follows them in the same "
        "pass, with nothing on screen to say so — %r" % bad)


def test_renderAlarms_bails_out_when_its_panel_is_absent():
    body = HTML.split("function renderAlarms()")[1].split("\nfunction ")[0]
    assert 'if(!$("#alarms")) return;' in body


def test_the_removed_panels_are_really_gone():
    for gone in ("alarms", "autopilot", "btnApNow", "perSiteDone"):
        assert gone not in IDS, "%s was supposed to be removed" % gone


def test_renderAlarms_survives_its_panel_being_absent():
    """It still runs — its checks are cheap and wanted — so it must tolerate having no panel."""
    body = HTML.split("function renderAlarms()")[1].split("\nfunction ")[0]
    assert 'if(!$("#alarms")) return;' in body


def test_hardware_is_rendered_after_alarms_in_the_same_pass():
    """Pins the actual failure shape: these two are in one statement sequence, so a throw in the
    first removes the second from the page. If they are ever separated this test is free; while
    they are together, the guard above is load-bearing."""
    assert "renderAlarms();" in HTML and "renderHardware(" in HTML

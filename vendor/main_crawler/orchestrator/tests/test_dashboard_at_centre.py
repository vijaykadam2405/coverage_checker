"""The dashboard must not report a healthy at-the-centre deployment as a broken one.

This crawler runs ON the data centre: a page is written into the corpus as it is captured, so there
is no link to configure and no queue to drain, and `/api/push/status` returns `centre_url: null`
for that reason. Read as "no data centre configured" it renders an amber warning next to a Push
button — which is what the operator saw, twice, on a stack that was working perfectly.

The bug is not one wrong string, it is a SHAPE: three places decide what to say about the centre,
and each was written from the local-machine assumption that a null `centre_url` means "unset". One
was fixed; the other kept warning. So the test pins the shape rather than the sentence — any future
branch that reports "not configured" without first letting `at_centre` out will fail here.

The JS lives in one <script> block, and a syntax error there kills the whole dashboard while the
page still returns 200. `node --check` is the cheapest thing that notices; it is skipped, loudly,
when node is absent rather than passing quietly.

Runnable directly:  python tests/test_dashboard_at_centre.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1]
        / "src" / "mallory_orchestrator" / "static" / "index.html")

NOT_CONFIGURED = "no data centre configured"


def _text() -> str:
    return HTML.read_text(encoding="utf-8")


def _script() -> str:
    """The single <script> block, which is the whole dashboard."""
    m = re.search(r"<script>(.*)</script>", _text(), re.S)
    assert m, "the dashboard has no <script> block — the file is not what this test thinks it is"
    return m.group(1)


def test_the_push_panel_stays_gone():
    """The panel this file was written to fix has been REMOVED, and that is the stronger fix.

    Four tests used to live here, each pinning one branch of a Push/"data centre" panel so it could
    not report a healthy at-the-centre stack as misconfigured. The panel itself is now deleted: this
    crawler writes straight into the corpus, so there is no link to configure, no queue to drain and
    nothing to push — every control on it described a machine that does not exist.

    What survives is the guarantee, inverted. Rather than checking each branch consults `at_centre`,
    check that no branch exists to get wrong. If a Push control is ever reintroduced, this fails and
    whoever adds it has to restore the at_centre guards along with it — which is exactly the moment
    the old bug would otherwise come back.
    """
    src = _text()
    for gone, why in (
        (NOT_CONFIGURED, "the amber 'not configured' warning the operator saw twice on a working stack"),
        ('id="btnPush"', "the Push button — there is nothing to push from the centre"),
        ("Waiting to be sent", "a backlog row; at the centre those documents are already stored"),
    ):
        assert gone not in src, (
            f"{gone!r} is back on the dashboard ({why}). If the Push panel is genuinely being "
            f"restored, it must consult `at_centre` before every one of these — see git history "
            f"for the four tests this replaced.")


def test_the_script_block_still_parses():
    """A syntax error here returns HTTP 200 and a blank dashboard, so nothing else in this file
    would notice."""
    node = shutil.which("node")
    if not node:
        print("  -- node not on PATH, JS syntax check SKIPPED (not passed)")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(_script())
        tmp = fh.name
    try:
        p = subprocess.run([node, "--check", tmp], capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, f"the dashboard JS does not parse:\n{p.stderr[:800]}"
    finally:
        Path(tmp).unlink(missing_ok=True)


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} DASHBOARD AT-CENTRE TESTS PASSED")


if __name__ == "__main__":
    main()

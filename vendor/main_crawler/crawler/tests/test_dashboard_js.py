"""The :8099 dashboard is one big JavaScript block living inside a Python string literal.

That makes a whole class of edit silently catastrophic: the HTML is a PLAIN (non-raw) triple-quoted
string, so a backslash written for JavaScript is eaten by Python first. Writing `'...?\n\n'` in the
source ships a real newline into the browser, which ends the JS string literal, which is a syntax
error, which kills the ENTIRE script block -- every tab, every button on the page stops working.
Nothing else catches it: Python still imports, the route still returns 200, and the page still looks
right until you click something.

Runnable directly:  python tests/test_dashboard_js.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler_api import dashboard  # noqa: E402

_SRC = Path(dashboard.__file__).with_suffix(".py")


def _html_literal() -> str:
    """The one big non-raw triple-quoted string that is the whole page."""
    src = _SRC.read_text(encoding="utf-8")
    start = src.index('return """<!DOCTYPE html>')
    return src[start:src.index('</body></html>"""', start)]


def t_every_backslash_in_the_page_literal_is_doubled() -> None:
    """A lone backslash means Python consumed an escape that JavaScript needed.

    Checked on the SOURCE rather than the rendered page, because by render time the evidence is
    gone: `\\n` has already become an invisible newline.
    """
    body = _html_literal()
    lone = []
    i = 0
    while i < len(body):
        if body[i] == "\\":
            if i + 1 < len(body) and body[i + 1] == "\\":
                i += 2                       # a doubled backslash is the correct form
                continue
            line = body.count("\n", 0, i) + 1
            lone.append((line, body[max(0, i - 40):i + 20].replace("\n", "|")))
        i += 1
    assert not lone, (
        "single backslash in the dashboard's page literal -- Python will eat it and the JS escape "
        "will not survive. Double it.\n"
        + "\n".join(f"  rel line {ln}: ...{ctx}..." for ln, ctx in lone))


def t_no_string_literal_runs_off_the_end_of_its_line() -> None:
    """The symptom the above prevents, asserted on the rendered output.

    Walks the emitted script tracking quote state so a real newline inside a '...' or "..." string
    is caught. Backtick templates legally span lines and are skipped; so are comments and regex
    literals (a regex may contain a bare quote -- `/[&<>"']/g` is in this very page).
    """
    js = re.findall(r"<script[^>]*>(.*?)</script>", dashboard.render(), re.S)[0]
    i, line, n = 0, 1, len(js)
    prev = ""                                # last significant char, to tell regex from division
    while i < n:
        c = js[i]
        if c == "\n":
            line += 1; i += 1; continue
        if c in " \t":
            i += 1; continue
        if c == "/" and i + 1 < n and js[i + 1] == "/":
            while i < n and js[i] != "\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and js[i + 1] == "*":
            i += 2
            while i + 1 < n and not (js[i] == "*" and js[i + 1] == "/"):
                line += js[i] == "\n"
                i += 1
            i += 2; continue
        # A '/' after a value is division; after an operator/punctuation it opens a regex.
        if c == "/" and prev not in ")]}" and not (prev.isalnum() or prev in "_$"):
            i += 1
            while i < n and js[i] != "/":
                if js[i] == "\\":
                    i += 1
                elif js[i] == "[":                      # a class may hold an unescaped '/'
                    while i < n and js[i] != "]":
                        i += 1
                elif js[i] == "\n":
                    break
                i += 1
            i += 1; prev = "/"; continue
        if c == "`":
            i += 1
            while i < n and js[i] != "`":
                if js[i] == "\\":
                    i += 1
                else:
                    line += js[i] == "\n"
                i += 1
            i += 1; prev = "`"; continue
        if c in "'\"":
            quote, opened = c, line
            i += 1
            while i < n and js[i] != quote:
                assert js[i] != "\n", (
                    f"unterminated JS string on line {opened} of the dashboard script -- a "
                    f"backslash escape was almost certainly eaten by Python. This takes the whole "
                    f"script block down: {js[i - 60:i].splitlines()[-1] if i >= 60 else ''!r}")
                if js[i] == "\\":
                    i += 1
                i += 1
            i += 1; prev = quote; continue
        prev = c
        i += 1


def t_the_fetched_to_kept_gap_is_shown_and_itemised() -> None:
    """"fetched 178 / kept 98" is not 80 pages lost, but the row showed no way to know that.

    The gap is exactly three counters — the keep-gate dropped it, it was unchanged since the last
    crawl, or its content was already captured under another URL this run — and the API has always
    returned all three. They just were not rendered, so the operator read an unexplained loss. This
    pins that every one of them reaches the row, and that the arithmetic actually closes.
    """
    page = dashboard.render()
    for counter in ("dropped_by_gate", "skipped_unchanged", "skipped_duplicate"):
        assert counter in page, f"{counter} is returned by the API but never rendered"
    assert "not stored" in page, "the fetched->kept gap has no chip"
    for why in ("off-topic", "unchanged", "duplicate"):
        assert why in page, f"the chip does not say {why!r} — a bare number explains nothing"
    # And the three counters are still the ONLY things between fetched and kept, so the chip cannot
    # silently understate the gap. Asserted against the engine, not restated here: any new
    # `ctx.<something> += 1` early-return added to that funnel fails this until the chip covers it.
    src = (Path(dashboard.__file__).resolve().parents[1]
           / "crawler" / "async_engine.py").read_text(encoding="utf-8")
    body = src[src.index("    async def _finalize("):]
    body = body[:body.index("ctx.kept += 1")]
    bumped = set(re.findall(r"ctx\.(\w+) \+= 1", body))
    assert bumped == {"skipped_unchanged", "skipped_duplicate"}, (
        f"_finalize gained/lost a drop reason: {sorted(bumped)} — the 'not stored' chip in "
        f"dashboard.py must account for it or the row hides pages again. (dropped_by_gate is the "
        f"third reason and is bumped earlier, in _process, so it is not expected here.)")


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} DASHBOARD-JS TESTS PASSED")


def test_dashboard_js_suite() -> None:
    main()


if __name__ == "__main__":
    main()

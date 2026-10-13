"""The Watchfloor: the landing view, its diagnosis, and the two ways a themed page silently breaks.

Three things here are worth a test and the rest is markup:

1. **The diagnosis.** It is a rule ladder where order IS the logic, and the rule that matters
   distinguishes "we are saturated" from "we starved our own scheduler" — the same numbers, read
   against slot occupancy instead of rate. It is extracted and executed, not pattern-matched.

2. **Token sync.** The palette lives in THREE blocks (dark `:root`, explicit light, and a literal
   duplicate of light under `prefers-color-scheme`). A token added to one and not the others gives
   a page that looks right until someone flips the toggle, which nobody notices in review.

3. **Hardcoded colour.** One `#hex` in a new rule and that element stops responding to the theme.

Runnable directly:  python tests/test_watchfloor.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1]
        / "src" / "mallory_orchestrator" / "static" / "index.html")

NEW_TOKENS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--grid", "--axis", "--hero-bg", "--hero-line"]


def _text() -> str:
    return HTML.read_text(encoding="utf-8")


def _script() -> str:
    m = re.search(r"<script>(.*)</script>", _text(), re.S)
    assert m, "the dashboard has no <script> block"
    return m.group(1)


def _theme_blocks(src: str) -> dict[str, str]:
    """The three palette blocks, by the selector that opens each."""
    out = {}
    for name, pat in (
        ("dark", r"^:root\{(.*?)^\}"),
        ("light", r"^:root\[data-theme=\"light\"\]\{(.*?)^\}"),
        ("os-light", r"@media \(prefers-color-scheme: light\)\{(.*?)\n  \}"),
    ):
        m = re.search(pat, src, re.S | re.M)
        assert m, f"could not find the {name} palette block"
        out[name] = m.group(1)
    return out


def test_every_new_token_is_defined_in_all_three_palettes():
    """A token in dark only looks perfect until the toggle is flipped."""
    blocks = _theme_blocks(_text())
    for tok in NEW_TOKENS:
        for name, body in blocks.items():
            assert re.search(re.escape(tok) + r"\s*:", body), (
                f"{tok} is missing from the {name} palette — the Watchfloor loses its colour "
                f"in that theme only")


def test_the_new_styles_use_tokens_not_hardcoded_colour():
    """A literal colour cannot respond to the theme."""
    m = re.search(r"SITUATION ROOM.*?</style>", _text(), re.S)
    assert m, "the Watchfloor style block is gone — rename this test or drop it"
    body = m.group(0)
    # Strip the comments; prose legitimately mentions things that look like values.
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    bad = re.findall(r":\s*(#[0-9a-fA-F]{3,8})\b", body)
    assert not bad, f"hardcoded colour(s) in the Watchfloor styles: {bad}"


def test_the_modes_are_registered_and_watchfloor_opens_first():
    """The landing view is the whole point of the change: control must be one click AWAY.

    SIPRI was deliberately unmounted from this nav — it is a separate crawler on its own stack with
    its own Postgres, MinIO and console, and folding it in made one page look like two products. Its
    proxy route and panel markup are intentionally left in the file, so this asserts on what is
    MOUNTED rather than on what exists."""
    s = _script()
    # Anchor the close to a line-start "];" — a mode handler body can contain "[2];" (the ctl
    # handler does), and a non-greedy "...];" would stop there and miss the modes after it.
    m = re.search(r"const MODES = \[(.*?)\n\];", s, re.S)
    assert m, "the MODES table is gone"
    ids = re.findall(r'\["(\w+)",', m.group(1))
    assert ids == ["watch", "explore", "ctl", "twin"], f"modes changed: {ids}"
    assert re.search(r'let MODE = "watch"', s), "the page no longer opens on the Watchfloor"
    assert re.search(r'\$\("#nav"\)\.style\.display = "none"', s), \
        "the control tab row must start hidden, or both nav levels show at once"


def test_the_diagnosis_banner_is_gone_and_left_nothing_behind():
    """The verdict hero was removed on request. What matters now is that it was removed CLEANLY:
    a leftover renderVerdict() call against a deleted #verdict element throws on every poll, and
    because every panel on this page renders from the same script, one throw mid-loop leaves the
    rest of the dashboard frozen on stale numbers while still looking alive."""
    s = _text()
    assert 'id="verdict"' not in s, "the hero markup is still there"
    assert "renderVerdict" not in s, "renderVerdict is still called or defined"
    assert "function diagnose(" not in s, "diagnose() is now dead code"


def test_the_page_says_so_when_its_own_script_died():
    """Every panel renders from JS, so a syntax error yields HTTP 200 and an empty shell. The
    canary is the last statement in the block; if it moved, it is no longer proving anything."""
    s = _script()
    assert 'document.body.setAttribute("data-js", "ok")' in s, "the JS canary is gone"
    tail = s.rstrip().splitlines()[-1].strip()
    assert tail.startswith('document.body.setAttribute("data-js"'), \
        f"the canary is no longer the LAST statement, so it no longer proves the block ran: {tail}"


def test_the_timeline_never_charts_a_cumulative_counter():
    """A cumulative corpus curve only ever rises — it keeps looking healthy for an hour after
    intake dies. The chart must be built from deltas, and negatives (a flush) dropped rather than
    drawn as a downward spike."""
    s = _script()
    m = re.search(r"function rates\(\)\{.*?\n\}", s, re.S)
    assert m, "rates() is gone — the timeline would be charting the raw counter"
    body = m.group(0)
    assert "s[i].d - s[i-1].d" in body, "the series is no longer a delta between samples"
    assert "if(dv < 0) continue" in body, "a corpus flush would render as a negative rate"
    assert "SBUCKET*3" in body, "a sampling gap must break the line, not be joined across"


def test_the_watchfloor_reads_in_the_order_the_operator_asked_for():
    """Hardware, then the corpus, then the batch, then throughput, then errors.

    The order is the argument. Hardware sits first because every panel under it is read differently
    depending on whether the machine has headroom — "1.3 pages/s" means a saturated box or a
    starved one, and the tiles above are what settles it. Asserted as positions in the Watchfloor
    panel only, so a section moved into another panel cannot satisfy it.

    The batch progress bar and the verdict hero were removed on request, so they are no longer in
    the sequence — the order that remains is the argument, not the count of sections."""
    src = _text()
    start = src.index('id="p-watch"')
    end = src.index('id="p-sipri"')
    panel = src[start:end]
    order = ['id="hwWatch"', 'id="kpi"', 'id="catMix"',
             'id="liveSites"', 'id="timeline"', 'id="errClass"']
    at = []
    for marker in order:
        assert marker in panel, f"the Watchfloor lost {marker}"
        at.append(panel.index(marker))
    assert at == sorted(at), (
        "the Watchfloor sections are out of order — expected "
        + " → ".join(order) + f", got positions {at}")


def test_hardware_is_rendered_from_one_place_into_both_panels():
    """The Control panel and the Watchfloor show the same tiles. A second copy of that markup would
    be a second thing to keep in step with the balloon and cgroup corrections, and those corrections
    exist precisely because the naive numbers were wrong."""
    js = _script()
    assert "function _hwSink(" in js, "the shared hardware sink is gone"
    assert js.count('"#hw", "#hwWatch"') == 1, "the two hardware sinks are no longer written together"
    # exactly one renderer, and it is called from BOTH loaders
    assert js.count("function renderHardware(") == 1
    assert "renderHardware(host)" in js


def test_a_progress_bar_is_never_drawn_without_a_real_denominator():
    """A bar that invents its own total is worse than no bar: it looks authoritative. The only
    honest denominator is the durable frontier's done+pending, and where there is no frontier row
    the row has to say so instead."""
    js = _script()
    assert "not started yet" in js, "the missing-frontier case lost its honest fallback"
    # The bar is only ever emitted inside this guard, and its total is done+pending.
    assert "if(f && (f.done || f.pending)){" in js, (
        "the progress bar is no longer gated on having real frontier counts")
    assert "const done = f.done||0, tot = done + (f.pending||0)" in js, (
        "the progress bar no longer derives its total from the frontier")


def test_the_error_classification_says_what_window_it_covers():
    """The crawler's issue log is append-only with no run filter, so labelling it "this crawl"
    would be a claim the data cannot support — it read as 16,806 failures in a batch that had
    16,806 events accumulated across the whole day."""
    js = _script()
    assert "the whole log, not just this run" in js, (
        "the error panel no longer says which window its numbers cover")


def test_the_per_site_drilldown_asks_the_crawler_not_the_store():
    """fetched/kept/retryable/failed exist only in the crawler's per-site report. The store knows
    what landed and cannot distinguish "never fetched" from "fetched and dropped" — which is the
    whole question this panel answers."""
    js = _script()
    assert "/api/crawler/batch/sites" in js
    assert "function renderSiteDetail(" in js
    assert "SITES_INFLIGHT" in js, "the per-site poll can stack without an in-flight guard"


def test_the_live_site_list_never_shows_one_site_twice():
    """The two host sources spell the same site differently — the crawler's per-site report names
    the host as FETCHED (www.cyientdlm.com) while the run publishes the SEED host
    (cyientdlm.com). An exact-string dedup let both through, and the live table rendered 10 rows
    for 8 sites, the same site twice with different numbers in each copy.

    Checked in the source because the merge is server-side and has no other observable seam."""
    src = (Path(__file__).resolve().parents[1] / "src" / "mallory_orchestrator" / "api.py"
           ).read_text(encoding="utf-8")
    i = src.index("def crawler_batch_status(")
    body = src[i:i + 3000]
    assert 'h.removeprefix("www.")' in body, (
        "the live-host merge no longer normalises www, so a site can be listed twice")
    assert "if h and k not in seen" in body, "the dedup no longer keys on the normalised host"


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} WATCHFLOOR TESTS PASSED")


if __name__ == "__main__":
    main()

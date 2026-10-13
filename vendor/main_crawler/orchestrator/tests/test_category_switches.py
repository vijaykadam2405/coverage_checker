"""The stop/start-by-category panel must list EVERY category, not just the ones with jobs today.

The panel is a control, not a report. Deriving its rows purely from `ScheduledJob.job_type` meant a
category only became switchable after it already had jobs — measured on a live 4-job scheduler,
which offered switches for `news` and `profile` and nothing else, so there was no way to stop
`tender` before generating tender jobs.

Runnable directly:  python tests/test_category_switches.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator import api, categories, importer  # noqa: E402


def test_every_source_category_falls_into_a_switchable_bucket():
    """The panel now switches OPERATOR BUCKETS (Manufacture / News / Tender / Think tank / Gov
    portal), not the crawler's job types. It had to: News, Gov portal and Think tank all generate
    job_type "news", so no job-type switch could ever express "hold the ministries, keep the trade
    press" — flipping one flipped all three.

    That moves the drift risk. It used to be "the crawler added a job type and the panel has no
    switch for it"; a new job type is now harmless, because the switch is derived from the SOURCE.
    The thing that can go wrong instead is a new CATEGORY in the importer with no bucket — every
    site imported under it would then crawl with no switch that can hold it. That is what this
    pins, against the importer's own vocabulary rather than a copy of it."""
    # `unknown` is exempt BY DESIGN and only that one: it is the importer's honest "nobody has
    # classified this site" value, and forcing it into a bucket would invent a fact about the site.
    # It is still switchable — it lands in the `uncatalogued` row, which has its own switch.
    unswitchable = [c for c in importer.CATEGORIES
                    if c not in categories.CAT_TO_BUCKET and c != "unknown"]
    assert not unswitchable, (f"importer offers {sorted(unswitchable)} but no bucket claims them — "
                              f"sites imported under those categories would have no stop switch")
    assert categories.bucket_of_job({"source_type": "unknown"}) == categories.UNCATALOGUED
    assert categories.UNCATALOGUED not in categories.BUCKETS, (
        "uncatalogued must stay a visible overflow row, not a sixth bucket in the vocabulary")


def test_a_new_crawler_job_type_no_longer_needs_a_switch_of_its_own():
    """The old pin required one switch per crawler JobType. Kept as a live check that the mapping
    still covers what the generators actually emit — `spec` (legacy jobgen, manufacturer_ir) and
    `patent_aux` have no bucket of their own and do not need one: those jobs carry source_type, so
    the Manufacture switch already governs them."""
    root = Path(__file__).resolve().parents[2]
    for sub in ("cralwer", "crawler"):
        src = root / sub / "crawler" / "models.py"
        if src.exists():
            break
    else:
        # A real skip, not a silent return. pytest COLLECTS this file, so `return` reported a
        # green pass — meaning a checkout without the crawler tree looked like the drift pin had
        # run and found nothing wrong.
        try:
            import pytest
            pytest.skip("crawler/models.py not found beside the orchestrator")
        except ImportError:                    # standalone `python tests/…` run, no pytest
            print("  ~ skipped vocabulary pin (crawler/models.py not found)")
            return
    m = re.search(r"^JobType\s*=\s*Literal\[(.+?)\]", src.read_text(encoding="utf-8"), re.M | re.S)
    assert m, "could not find JobType in crawler/models.py — has it been renamed?"
    from_crawler = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    assert from_crawler, "regex matched no job types — this test would pass vacuously"
    # A `spec` job for a manufacturer is governed by the Manufacture switch, because the switch
    # reads source_type. Prove that rather than asserting a job_type→bucket relation that no longer
    # exists.
    assert categories.bucket_of_job(
        {"job_type": "spec", "source_type": "manufacturer_ir"}) == "manufacture"


def test_a_job_is_never_silently_dropped_into_the_wrong_bucket():
    """bucket_of_job is what both dispatch gates now filter on. If it guessed, an unclassified job
    would inherit a switch the operator never meant to point at it — held when they stopped an
    unrelated category, or running when they thought they had stopped everything."""
    # Stamped source_type wins: this is what jobgen._stamp writes onto every generated job.
    assert categories.bucket_of_job({"source_type": "manufacturer_ir"}) == "manufacture"
    assert categories.bucket_of_job({"source_type": "gov_primary"}) == "gov_portal"
    assert categories.bucket_of_job({"source_type": "defence_org"}) == "think_tank"
    # Falls back to the catalog by source_id, www-insensitively.
    cat = {"bhel.com": "manufacture"}
    assert categories.bucket_of_job({"source_id": "www.bhel.com"}, cat) == "manufacture"
    # ...and refuses to guess otherwise.
    assert categories.bucket_of_job({"source_id": "nobody-knows.com"}, cat) == categories.UNCATALOGUED
    assert categories.bucket_of_job({"job_type": "news"}) == categories.UNCATALOGUED
    assert categories.bucket_of_job({}) == categories.UNCATALOGUED
    assert categories.bucket_of_job(None) == categories.UNCATALOGUED


def test_the_three_news_buckets_are_separately_switchable():
    """The reason this rework exists. All three generate job_type "news"; the switches must not."""
    assert categories.CAT_TO_JOBTYPE["trade_press"] == "news"
    assert categories.CAT_TO_JOBTYPE["gov_primary"] == "news"
    assert categories.CAT_TO_JOBTYPE["defence_org"] == "news"
    buckets = {categories.bucket_of_job({"source_type": c})
               for c in ("trade_press", "gov_primary", "defence_org")}
    assert buckets == {"news", "gov_portal", "think_tank"}, buckets


def test_every_category_is_listed_even_with_no_jobs(monkeypatch):
    """A fresh install has zero scheduled jobs and must still offer all five switches."""
    class _EmptyDB:
        def scalars(self, *_a, **_k):
            class _R:
                def all(self_inner):
                    return []
            return _R()

    # monkeypatch, NOT a bare assignment: `api.scheduler` is the scheduler MODULE, so rebinding
    # it here leaks into every later test in the process.
    monkeypatch.setattr(api.scheduler, "disabled_categories", lambda db: set())
    rows = api._category_rows(_EmptyDB())
    got = {r["category"] for r in rows}
    assert got == set(api.JOB_CATEGORIES), got
    for r in rows:
        # Honest zeros, not invented numbers — a category with no jobs says so.
        assert r["total"] == 0 and r["enabled_jobs"] == 0 and r["due_now"] == 0, r
        assert r["enabled"] is True, "a category nobody switched off is running"


def test_a_disabled_category_reads_as_stopped(monkeypatch):
    """The off switch has to survive a round trip, including for a category with no jobs — that is
    the whole point of listing it before it has any."""
    class _EmptyDB:
        def scalars(self, *_a, **_k):
            class _R:
                def all(self_inner):
                    return []
            return _R()

    monkeypatch.setattr(api.scheduler, "disabled_categories", lambda db: {"tender"})
    rows = {r["category"]: r for r in api._category_rows(_EmptyDB())}
    assert rows["tender"]["enabled"] is False
    assert rows["news"]["enabled"] is True
    # ...and an unknown category invented by a hand-crafted request is still refused.
    assert "not_a_category" not in rows


def test_every_source_category_reads_as_one_of_the_five_buckets():
    """The dashboard says "category" in five places and used to mean two different things: the Job
    generator offered News / Manufacture / Tender / Gov portal / Think tank, while the catalog, the
    logs and the import preview printed the raw taxonomy — so `trade_press` and `News` were the same
    row seen from two screens. ui_category() is the one place that decides, and it is derived from
    the Job generator's own filter map so the label can never name a bucket the filter would miss."""
    five = {"News", "Manufacture", "Tender", "Gov portal", "Think tank"}
    for raw in importer.CATEGORIES:
        label = api.ui_category(raw)
        assert label, f"{raw} rendered as an empty category"
        # Everything the Job generator can SELECT must render as the bucket it selects into.
        if raw in api._CAT_TO_BUCKET:
            assert label in five, f"{raw} -> {label!r}, which is not one of the five buckets"
            key = api._CAT_TO_BUCKET[raw]
            assert raw in api._UI_CATEGORIES[key][0], f"{raw} labelled {label} but not filtered into {key}"

    # An unclassified source keeps its own name. Forcing it into a bucket would be inventing a fact
    # about a site nobody has categorised — the same rule the Live tiles follow for unmeasured
    # numbers, and the reason "unknown" must not quietly become "News".
    assert api.ui_category("unknown") == "unknown"
    assert api.ui_category("uncatalogued") == "uncatalogued"
    assert api.ui_category(None) == ""
    assert api.ui_category("") == ""


def test_the_dashboard_shows_the_label_not_the_raw_name():
    """Server-side labelling is worthless if the page still prints `r.category`."""
    html = (Path(api.__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    assert html.count("category_label") >= 5, "some category column still renders the raw taxonomy"
    assert ">Defence org<" not in html, "the import default still offers a sixth, off-vocabulary bucket"
    for label in ("News", "Manufacture", "Tender", "Gov portal", "Think tank"):
        assert f">{label}</option>" in html, f"the import default lost the {label} bucket"


def test_the_dashboard_script_block_still_parses():
    """The whole page is ONE 79k-character <script>. A stray brace or an unescaped backtick anywhere
    in it kills every handler on the page — and the server still returns 200, so the failure looks
    like "the buttons stopped working" rather than a syntax error. Cheap to catch, invisible if not.
    Skipped (not failed) where node is unavailable: a missing tool is not a broken dashboard."""
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    if not node:
        print("  -- node not found, JS parse check skipped")
        return
    html = (Path(api.__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    blocks = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    assert blocks, "the dashboard has no script block at all"
    with tempfile.TemporaryDirectory() as d:
        for i, b in enumerate(blocks):
            p = Path(d) / f"b{i}.js"
            p.write_text(b, encoding="utf-8")
            r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
            assert r.returncode == 0, f"script block {i} does not parse:\n{r.stderr[:800]}"


if __name__ == "__main__":
    # Discovered, not listed: the hand-written tuple silently stopped running tests that were added
    # after it, and renaming one turned the whole file into a NameError at import.
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} CATEGORY-SWITCH TESTS PASSED")

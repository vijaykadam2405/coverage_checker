"""C2 archive tier — the selection logic, offline (never touches archive.org).

The tests that matter most are the ones guarding SILENT WRONG DATA: a C2 run that emits the wrong
snapshots still reports "sent: N" and looks like a success.
"""
import pytest

from crawler import c2
from crawler.c2 import Snapshot, _pdf_at, _select, _split, _window
from crawler.models import Job


def _job(**kw):
    base = dict(job_id="a", job_type="profile", seed_urls=["https://sipri.org/"], max_pages=40)
    return Job(**{**base, **kw})


def _snap(ts, digest, url="http://sipri.org/a"):
    return Snapshot(urlkey="org,sipri)/a", ts=ts, original=url, digest=digest)


# --- the window -----------------------------------------------------------
def test_single_date_is_as_of_the_END_of_that_day():
    to, frm = _window(_job(archive_date="2024-07-01"))
    assert to == "20240701235959" and frm is None       # frm None == "as-of" mode


def test_range_spans_both_endpoints_inclusive():
    to, frm = _window(_job(archive_date="2024-07-01", archive_to="2024-07-23"))
    assert frm == "20240701000000" and to == "20240723235959"


# --- as-of: nearest capture AT OR BEFORE the date -------------------------
def test_as_of_picks_the_nearest_capture_at_or_before():
    snaps = [_snap("20240601000000", "A"), _snap("20240628120000", "B")]
    assert [s.ts for s in _select(snaps, None, "20240701235959")] == ["20240628120000"]


def test_as_of_returns_a_page_never_touched_since_2023():
    # THE reason we never send from= to CDX. A page last captured in 2023 and never updated IS its
    # own 1-July state; a from=20240701 filter returns zero rows and it would vanish from the
    # baseline while the run reported success.
    assert len(_select([_snap("20230105000000", "A")], None, "20240701235959")) == 1


def test_as_of_returns_nothing_when_the_archive_has_nothing():
    assert _select([], None, "20240701235959") == []


# --- range: baseline + ONLY changed --------------------------------------
def test_range_is_baseline_plus_only_changed():
    # The user's ask, literally: "1 july all + [what] changed till 23".
    snaps = [_snap("20240610000000", "A"),   # before the window -> the 1-July state (baseline)
             _snap("20240703000000", "A"),   # unchanged -> NOT a version
             _snap("20240709000000", "B"),   # changed
             _snap("20240712000000", "B"),   # unchanged -> NOT a version
             _snap("20240718000000", "C")]   # changed
    picks = _select(snaps, "20240701000000", "20240723235959")
    assert [(s.ts, s.digest) for s in picks] == [
        ("20240610000000", "A"), ("20240709000000", "B"), ("20240718000000", "C")]


def test_range_emits_baseline_only_when_nothing_changed():
    snaps = [_snap("20240610000000", "A"), _snap("20240705000000", "A")]
    assert [s.ts for s in _select(snaps, "20240701000000", "20240723235959")] == ["20240610000000"]


def test_page_first_captured_inside_the_window_uses_its_first_capture_as_baseline():
    snaps = [_snap("20240705000000", "A"), _snap("20240711000000", "B")]
    assert [s.ts for s in _select(snaps, "20240701000000", "20240723235959")] == \
        ["20240705000000", "20240711000000"]


def test_a_revert_is_a_real_change_and_must_emit():
    # A->B->A is three real states. Comparing to the PREVIOUS KEPT digest (not a global seen-set)
    # is what preserves the third; a global set would silently eat it.
    snaps = [_snap("20240702000000", "A"), _snap("20240710000000", "B"), _snap("20240720000000", "A")]
    assert len(_select(snaps, "20240701000000", "20240723235959")) == 3


def test_baseline_is_the_latest_capture_before_the_window_not_the_earliest():
    snaps = [_snap("20200101000000", "OLD"), _snap("20240630235900", "NEW"), _snap("20240715000000", "X")]
    picks = _select(snaps, "20240701000000", "20240723235959")
    assert [s.digest for s in picks] == ["NEW", "X"]


# --- the locator: the trailing-slash trap --------------------------------
def test_locator_keeps_the_trailing_slash():
    # canonicalize_url() eats it (".../id_/http://www.sipri.org/" -> "...sipri.org"), which is why
    # C2 builds its own locator and never routes through Fetcher.fetch/fetch_asset.
    s = _snap("20150404072517", "A", url="http://www.sipri.org/")
    assert s.locator == "https://web.archive.org/web/20150404072517id_/http://www.sipri.org/"


def test_locator_survives_canonicalize_untouched():
    from crawler.canonicalize import canonicalize_url
    s = _snap("20150404072517", "A", url="http://www.sipri.org/")
    assert canonicalize_url(s.locator) != s.locator     # the trap is real...
    assert s.locator.endswith("/")                      # ...and locator does not go through it


def test_archived_at_is_iso():
    assert _snap("20240701123456", "A").archived_at == "2024-07-01T12:34:56Z"


# --- splitting the one scan ----------------------------------------------
def _row(urlkey, ts, url, digest, mime="text/html", status="200"):
    return [urlkey, ts, url, digest, mime, status]


def test_split_groups_by_urlkey_and_sorts_by_ts():
    rows = [_row("k1", "20240710000000", "http://x/a", "B"),
            _row("k1", "20240701000000", "http://x/a", "A"),   # server order NOT trusted
            _row("k2", "20240705000000", "http://x/b", "C")]
    pages, _ = _split(rows)
    assert [s.ts for s in pages["k1"]] == ["20240701000000", "20240710000000"]
    assert list(pages) == ["k1", "k2"]


def test_split_separates_pdfs_and_keeps_them_out_of_pages():
    rows = [_row("k1", "20240701000000", "http://x/a", "A"),
            _row("k2", "20240702000000", "http://x/f.pdf", "P", mime="application/pdf")]
    pages, pdfs = _split(rows)
    assert "k2" not in pages                                   # a pdf is an attachment, not a page
    assert pdfs == {"http://x/f.pdf": ["20240702000000"]}


def test_split_drops_archived_error_pages():
    rows = [_row("k1", "20240701000000", "http://x/a", "A", status="404"),
            _row("k2", "20240701000000", "http://x/b", "B", status="503")]
    pages, _ = _split(rows)
    assert pages == {}


def test_split_keeps_a_revisit_row_whose_statuscode_is_a_dash():
    # A content REVERT is a warc/revisit row carrying statuscode '-'. filter=statuscode:200 would
    # delete it; that is why we negate 4xx/5xx instead.
    pages, _ = _split([_row("k1", "20240701000000", "http://x/a", "A", mime="warc/revisit", status="-")])
    assert len(pages["k1"]) == 1


# --- PDFs resolved from the index, at-or-before -------------------------
def test_pdf_resolves_to_the_nearest_capture_at_or_before_the_page():
    pdfs = {"http://x/f.pdf": ["20240101000000", "20240705000000", "20260101000000"]}
    loc = _pdf_at(pdfs, "http://x/f.pdf", "20240710000000")
    assert loc == "https://web.archive.org/web/20240705000000id_/http://x/f.pdf"


def test_pdf_never_imports_a_future_capture_into_a_dated_document():
    # The whole reason we join against the index instead of trusting /web/{ts}id_/ to 302 to
    # "nearest": the redirect picks nearest in EITHER direction and would put a 2026 PDF in a
    # July-2024 document.
    pdfs = {"http://x/f.pdf": ["20260101000000"]}
    assert _pdf_at(pdfs, "http://x/f.pdf", "20240710000000") is None


def test_pdf_missing_from_the_archive_is_skipped_not_fetched_live():
    assert _pdf_at({}, "http://x/gone.pdf", "20240710000000") is None


# --- the budget bug: a range must not emit baselines only ---------------
def test_budget_does_not_starve_changes(monkeypatch):
    """REGRESSION: a global picks.sort(key=ts) before the max_pages cap emits every baseline and
    ZERO changes (baselines are by construction the oldest rows) — silently deleting the entire
    range feature while reporting success. Budget must be spent per-URL, in url order."""
    urls = [f"http://x/{i}" for i in range(10)]
    rows = []
    for i, u in enumerate(urls):
        rows += [_row(f"k{i:02d}", "20240610000000", u, "A"),      # baseline
                 _row(f"k{i:02d}", "20240709000000", u, "B"),      # change
                 _row(f"k{i:02d}", "20240718000000", u, "C")]      # change
    monkeypatch.setattr(c2, "_rows", lambda *a, **k: (rows, False, False))

    emitted = []

    def fake_emit(ctx, client, snap, pdfs, delay):
        emitted.append((snap.original, snap.digest))
        return True

    monkeypatch.setattr(c2, "_emit", fake_emit)
    ctx = _ctx(_job(archive_date="2024-07-01", archive_to="2024-07-23", max_pages=6))
    c2._run_job(ctx, client=None, delay_s=0)

    assert len(emitted) == 6                       # max_pages counts SNAPSHOTS (documents)
    # 6 snapshots = the first TWO urls fully (baseline+2 changes each) — not 6 baselines.
    assert emitted == [("http://x/0", "A"), ("http://x/0", "B"), ("http://x/0", "C"),
                       ("http://x/1", "A"), ("http://x/1", "B"), ("http://x/1", "C")]
    assert {d for _, d in emitted} == {"A", "B", "C"}   # changes present, not baselines only


def test_truncated_scan_drops_the_url_whose_history_may_be_cut(monkeypatch):
    """CDX sorts by (urlkey, timestamp), so the url we stop on can have HALF its captures — which
    silently yields a wrong 'nearest capture' and a bogus baseline. It sorts last; drop it."""
    rows = [_row("k00", "20240610000000", "http://x/a", "A"),
            _row("k01", "20240610000000", "http://x/b", "B")]      # last urlkey: possibly cut
    monkeypatch.setattr(c2, "_rows", lambda *a, **k: (rows, False, True))   # truncated
    emitted = []
    monkeypatch.setattr(c2, "_emit", lambda ctx, cl, s, p, d: emitted.append(s.original) or True)
    c2._run_job(_ctx(_job(archive_date="2024-07-01", max_pages=40)), client=None, delay_s=0)
    assert emitted == ["http://x/a"], "the possibly-cut last url must not be emitted"


def test_untruncated_scan_keeps_every_url(monkeypatch):
    rows = [_row("k00", "20240610000000", "http://x/a", "A"),
            _row("k01", "20240610000000", "http://x/b", "B")]
    monkeypatch.setattr(c2, "_rows", lambda *a, **k: (rows, False, False))  # complete
    emitted = []
    monkeypatch.setattr(c2, "_emit", lambda ctx, cl, s, p, d: emitted.append(s.original) or True)
    c2._run_job(_ctx(_job(archive_date="2024-07-01", max_pages=40)), client=None, delay_s=0)
    assert emitted == ["http://x/a", "http://x/b"]


def test_a_single_url_scan_is_never_emptied_by_the_cut(monkeypatch):
    rows = [_row("k00", "20240610000000", "http://x/a", "A")]
    monkeypatch.setattr(c2, "_rows", lambda *a, **k: (rows, False, True))
    emitted = []
    monkeypatch.setattr(c2, "_emit", lambda ctx, cl, s, p, d: emitted.append(s.original) or True)
    c2._run_job(_ctx(_job(archive_date="2024-07-01", max_pages=40)), client=None, delay_s=0)
    assert emitted == ["http://x/a"]


def test_incomplete_scan_is_reported_not_passed_off_as_success(monkeypatch):
    monkeypatch.setattr(c2, "_rows", lambda *a, **k: ([], False, True))   # truncated
    ctx = _ctx(_job(archive_date="2024-07-01"))
    c2._run_job(ctx, client=None, delay_s=0)
    assert ctx.c2_incomplete is True


def test_complete_scan_is_not_flagged(monkeypatch):
    monkeypatch.setattr(c2, "_rows", lambda *a, **k: ([], False, False))
    ctx = _ctx(_job(archive_date="2024-07-01"))
    c2._run_job(ctx, client=None, delay_s=0)
    assert ctx.c2_incomplete is False


# --- C2 left the C1/C3 ladder: exclusivity is STRUCTURAL ------------------
def test_the_live_engine_has_no_path_to_the_archive():
    """C2 is no longer a fallback rung. If someone re-adds an archive call to the live engine, a C1
    crawl silently starts serving undated archived copies as though they were live pages — exactly
    the quiet wrongness this split exists to remove. Checks compiled CONSTANTS, not source text, so
    a comment mentioning archive.org can't trip it and can't hide it either."""
    import crawler.async_engine as ae

    def _codes(obj, prefix=""):
        """Every code object reachable from `obj`, including CLASS METHODS and NESTED functions.

        The original walked `dir(module)` and read `__code__` — but a class has no `__code__`, so
        AsyncEngine's methods were never examined and neither were any inner functions. Measured:
        it inspected 57 module-level helpers and zero engine methods, and would have passed with
        "web.archive.org" sitting inside AsyncEngine._fetch. That is the exact regression the test
        names in its own docstring.
        """
        code = getattr(obj, "__code__", None) or getattr(getattr(obj, "__func__", None), "__code__", None)
        if code is not None:
            yield prefix, code
            for const in code.co_consts:                       # nested funcs / comprehensions
                if hasattr(const, "co_consts"):
                    yield prefix + ".<nested>", const
        elif isinstance(obj, type):
            for attr in vars(obj).values():
                yield from _codes(attr, prefix)

    seen, hits, scanned = set(), [], 0
    for name in dir(ae):
        member = getattr(ae, name, None)
        for where, code in _codes(member, name):
            if id(code) in seen:
                continue
            seen.add(id(code))
            scanned += 1
            for const in code.co_consts:
                if isinstance(const, str) and "web.archive.org" in const:
                    hits.append(where)
    # Without these two the test passes when the scan finds nothing to look at — which is exactly
    # how it passed while inspecting none of the engine.
    assert scanned > 100, f"the scan only reached {scanned} code objects — it is not seeing the engine"
    engine_methods = sum(1 for _ in _codes(ae.AsyncEngine, "AsyncEngine"))
    assert engine_methods > 20, \
        f"only {engine_methods} AsyncEngine code objects scanned — the class walk is broken"
    assert not hits, f"the live engine reaches archive.org again in: {hits}"


def test_c2_is_the_only_module_that_talks_to_the_archive():
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "crawler"
    owners = sorted(p.name for p in src.glob("*.py") if "web.archive.org" in p.read_text(encoding="utf-8"))
    assert owners == ["c2.py"], f"archive access leaked outside c2.py: {owners}"


def _ctx(job):
    import types
    from crawler.async_engine import JobCtx
    ctx = JobCtx.__new__(JobCtx)
    ctx.job = job
    ctx.done = False
    ctx.budget_used = 0
    ctx.history = None
    ctx.errors_by_reason = {}
    ctx.errors = 0
    return ctx

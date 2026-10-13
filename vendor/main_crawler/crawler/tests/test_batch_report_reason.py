"""Clicking a reason chip has to show THAT reason's URLs, even when it is rare.

The report's URL list is the first N non-kept rows in database order, and the reason histogram is a
complete count over all of them. Those two facts together produce the bug: zf.com reported

    host_busy - 143      needs_network_path - 4      consent_wall - 2

and every URL in the 60-row sample the panel received was host_busy, so clicking `consent_wall - 2`
opened an empty table. The count was right and the list was right; the FILTER was in the wrong
place — after the cap instead of before it.

Runnable directly:  python tests/test_batch_report_reason.py
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RUN = "run-1"
# One dominant reason ahead of the rare ones, exactly as the live row looked. `cap` below is smaller
# than the number of host_busy rows, which is what pushes the rare reasons out.
ROWS = ([("https://www.zf.com/hb/%d" % i, 429, "host_busy") for i in range(143)]
        + [("https://www.zf.com/np/%d" % i, 403, "needs_network_path") for i in range(4)]
        + [("https://www.zf.com/cw/%d" % i, 200, "consent_wall") for i in range(2)]
        + [("https://www.zf.com/ok/%d" % i, 200, None) for i in range(221)])


def _db(d: Path) -> Path:
    p = d / "crawl_history.sqlite"
    con = sqlite3.connect(p)
    con.execute("""CREATE TABLE crawl_pages (canonical_url TEXT, last_status INT,
                   error_category TEXT, fail_count INT, failed_at TEXT, error_detail TEXT,
                   crawl_run_id TEXT)""")
    con.executemany("INSERT INTO crawl_pages VALUES (?,?,?,1,'2026-07-30','',?)",
                    [(u, s, c, RUN) for u, s, c in ROWS])
    con.commit()
    con.close()
    return p


def _report(**kw):
    import importlib
    import os
    d = Path(tempfile.mkdtemp(prefix="rep_"))
    _db(d)
    os.environ["CRAWLER_DATA_DIR"] = str(d)
    from crawler import config
    importlib.reload(config)
    from crawler_api import app as capp
    importlib.reload(capp)
    return capp.batch_report(run_id=RUN, host="zf.com", **kw)


def test_the_histogram_counts_every_reason():
    r = _report(urls=500)
    site = r["sites"][0]
    assert site["reasons"] == {"host_busy": 143, "needs_network_path": 4, "consent_wall": 2}
    assert site["kept"] == 221 and site["retryable"] + site["terminal"] == 149


def test_a_rare_reason_is_LOST_when_the_cap_bites_and_no_reason_is_asked_for():
    """Not a bug — this is the unfiltered view, and it documents WHY the parameter has to exist."""
    r = _report(urls=60)
    urls = r["sites"][0]["urls"]
    assert len(urls) == 60
    assert not [u for u in urls if u["reason"] == "consent_wall"], (
        "the fixture no longer reproduces the ordering that caused the bug")


def test_asking_for_the_rare_reason_returns_it_even_under_the_same_cap():
    """The fix: filter before the cap, so the cap applies to the rows actually wanted."""
    r = _report(urls=60, reason="consent_wall")
    urls = r["sites"][0]["urls"]
    assert len(urls) == 2, urls
    assert {u["reason"] for u in urls} == {"consent_wall"}
    assert sorted(u["url"] for u in urls) == ["https://www.zf.com/cw/0", "https://www.zf.com/cw/1"]


def test_filtering_does_NOT_rewrite_the_counts_beside_it():
    """The histogram must describe the whole site whatever is being looked at, or clicking a chip
    would silently change the numbers the chips are made of."""
    r = _report(urls=60, reason="consent_wall")
    site = r["sites"][0]
    assert site["reasons"] == {"host_busy": 143, "needs_network_path": 4, "consent_wall": 2}
    assert site["kept"] == 221


def test_an_unknown_reason_returns_no_urls_rather_than_all_of_them():
    """Failing open here would show 60 unrelated URLs under a label saying they are something else."""
    r = _report(urls=60, reason="no_such_reason")
    assert r["sites"][0]["urls"] == []


def test_no_reason_still_returns_the_mixed_sample():
    r = _report(urls=500)
    assert len({u["reason"] for u in r["sites"][0]["urls"]}) == 3


def test_the_dashboard_asks_the_crawler_instead_of_filtering_what_it_has():
    """The unwired-fix check. Filtering client-side over a capped list is the original bug."""
    html = (Path(__file__).resolve().parents[2] / "orchestrator" / "src" / "mallory_orchestrator"
            / "static" / "index.html").read_text(encoding="utf-8")
    assert '"&reason=" + encodeURIComponent(cause)' in html, (
        "the drill-down does not ask the crawler for the chosen reason")
    assert "allUrls.filter(u => (u.reason" not in html, (
        "the old client-side filter over a capped sample is still there")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} BATCH-REPORT-REASON TESTS PASSED")

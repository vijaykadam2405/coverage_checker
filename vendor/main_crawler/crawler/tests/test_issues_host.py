"""Asking the issues log about a host must find that host's issues.

The site row on the Logs tab said `abeking.com  UNRESOLVED 11` in red. Clicking "why?" opened a
panel that said "nothing unresolved for this host". Both numbers came from this module: the 11 from
`summary()['by_host']`, which the dashboard normalises by stripping `www.`, and the rows from
`recent(host=...)`, which matched the stored hostname exactly. abeking.com redirects to www, so
every issue is filed under `www.abeking.com` and the two disagreed.

Measured on the live crawl: `/v1/issues?host=www.abeking.com` returned 14 issues,
`/v1/issues?host=abeking.com` returned 0, while the summary reported 11 against `abeking.com`.

That is worse than either number being wrong on its own. A panel that contradicts the count next to
it teaches an operator to stop believing the dashboard.

Runnable directly:  python tests/test_issues_host.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from crawler.issues import IssueLog


def _log() -> IssueLog:
    """A fresh log on its own file — never the crawl's real data/issues.jsonl."""
    tmp = Path(tempfile.mkdtemp()) / "issues.jsonl"
    return IssueLog(path=tmp)


def test_a_host_is_found_by_the_name_the_dashboard_shows():
    """THE bug. Issues filed under www., asked for without it."""
    lg = _log()
    for i in range(11):
        lg.record(f"https://www.abeking.com/schiff/{i}", "soft_404")

    assert len(lg.recent(host="abeking.com")) == 11, (
        "asking by registered domain found nothing, but the count beside it says 11 — "
        "this is the row that said UNRESOLVED 11 next to 'nothing unresolved for this host'")
    assert len(lg.recent(host="www.abeking.com")) == 11, "the stored spelling must still work"
    assert len(lg.recent(host="ABEKING.COM")) == 11, "host matching is case-insensitive"


def test_the_summary_and_the_rows_agree_for_every_host():
    """The property that actually matters, stated once: for any host the summary counts, asking for
    that host must return exactly that many rows. A future normalisation that fixes one side and
    not the other fails here."""
    lg = _log()
    lg.record("https://www.abeking.com/a", "soft_404")
    lg.record("https://www.abeking.com/b", "soft_404")
    lg.record("https://aecc.cn/x", "timeout")
    lg.record("https://www.aerobits.pl/y", "needs_render")

    for host, n in lg.summary()["by_host"].items():
        rows = lg.recent(host=host, limit=1000)
        assert len(rows) == n, (f"summary says {host} has {n} unresolved, asking for it returns "
                                f"{len(rows)} — the two halves of the panel disagree")
        # …and by the name the dashboard actually renders, which is the summary key with www. gone.
        shown = host.removeprefix("www.")
        assert len(lg.recent(host=shown, limit=1000)) == n, (
            f"the panel opens on '{shown}' and gets {len(lg.recent(host=shown, limit=1000))} "
            f"rows for a count of {n}")


def test_a_different_host_is_not_swept_in():
    """Normalising must not make the filter promiscuous: stripping a prefix is not a substring
    match, and `notabeking.com` is a different site."""
    lg = _log()
    lg.record("https://www.abeking.com/a", "soft_404")
    lg.record("https://notabeking.com/b", "soft_404")
    lg.record("https://abeking.com.evil.test/c", "soft_404")

    rows = lg.recent(host="abeking.com")
    assert len(rows) == 1, [r["url"] for r in rows]
    assert rows[0]["url"] == "https://www.abeking.com/a", rows


def test_no_host_filter_still_returns_everything():
    lg = _log()
    lg.record("https://www.abeking.com/a", "soft_404")
    lg.record("https://aecc.cn/b", "timeout")
    assert len(lg.recent()) == 2
    assert len(lg.recent(cause="timeout")) == 1, "the cause filter is unaffected"


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} ISSUES-HOST TESTS PASSED")


if __name__ == "__main__":
    main()

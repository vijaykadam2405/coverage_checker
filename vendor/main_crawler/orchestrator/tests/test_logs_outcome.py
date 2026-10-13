"""The Logs 'complete' tag must survive a refresh.

The green "complete" outcome used to be driven only by the crawler's IN-MEMORY batch report, so the
moment the next scheduled run replaced that report the tag flipped to "stored, not verified" — the
user saw it vanish on refresh. _row_unresolved falls back to the DURABLE issues log so a finished
host stays "complete", while still refusing to fabricate a clean 0 when nothing measured the host.

Runnable directly:  python tests/test_logs_outcome.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator.api import _row_unresolved  # noqa: E402


def t_in_memory_report_wins_and_sums_retryable_plus_terminal() -> None:
    assert _row_unresolved(True, 2, 3, 100, True, 0) == (5, False)
    assert _row_unresolved(True, 0, 0, 100, True, 99) == (0, False)  # report 0 beats a stale log


def t_durable_zero_is_a_real_complete_after_the_report_is_gone() -> None:
    # report gone, store has pages, issues log reachable and empty for this host → complete, durably
    assert _row_unresolved(False, 0, 0, 459, True, 0) == (0, True)


def t_durable_nonzero_is_partial() -> None:
    assert _row_unresolved(False, 0, 0, 120, True, 7) == (7, True)


def t_no_stored_pages_stays_unknown_not_complete() -> None:
    # a host with no stored pages was never proven crawled — must not read as a clean 0
    assert _row_unresolved(False, 0, 0, 0, True, 0) == (None, False)


def t_unreachable_issues_log_never_fabricates_a_clean_zero() -> None:
    # the log is down: even with stored pages we cannot claim complete — unknown, shown as '?'
    assert _row_unresolved(False, 0, 0, 500, False, 0) == (None, False)


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} LOGS-OUTCOME TESTS PASSED")


if __name__ == "__main__":
    main()

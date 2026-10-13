"""An INFO log the operator cannot see is not a log.

MEASURED 2026-08-15. `docker logs mallory-crawler-orchestrator-1` returned FOUR lines — all of them
uvicorn's own startup banner — for the entire life of the deployment:

    INFO:     Started server process [7]
    INFO:     Waiting for application startup.
    INFO:     Application startup complete.
    INFO:     Uvicorn running on http://0.0.0.0:8090

uvicorn configures its own loggers and leaves the ROOT logger untouched. A module logger built with
logging.getLogger("mallory.refill") therefore had no handler and fell through to
logging.lastResort, which emits WARNING and above and silently drops everything below. Every
log.info() in the scheduler, the refill watcher, the autopilot and the mailer went nowhere.

The cost was not cosmetic. The auto-refill watcher spent days at capacity resuming nothing while
the fleet drained 12 -> 9 -> 7, and the single signal that would have said so was its own log line.
Finding it needed a live call to refill_once() from inside the container.

Runnable directly:  python tests/test_application_logs_are_visible.py
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

MAIN = SRC / "mallory_orchestrator" / "main.py"


def t_main_configures_the_root_logger() -> None:
    """Pinned against the source: importing main for real would start the app. Comments are
    stripped first — this file explains the bug at length and must not match its own prose."""
    src = re.sub(r"#.*", "", MAIN.read_text(encoding="utf-8"))
    assert re.search(r"logging\.basicConfig\s*\(", src), (
        "main.py never calls logging.basicConfig — every log.info in the package is dropped by "
        "logging.lastResort, which is exactly the state that hid the refill watcher for days")
    assert re.search(r"ORCH_LOG_LEVEL", src), (
        "the level should be settable without a rebuild; ORCH_LOG_LEVEL is the documented knob")


def t_an_info_record_actually_reaches_a_handler() -> None:
    """The behavioural half. A source pin proves basicConfig is CALLED; this proves the result is a
    root logger that admits INFO, which is the property that was missing."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    try:
        root.handlers = []
        root.setLevel(logging.WARNING)
        logging.basicConfig(level="INFO",
                            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        log = logging.getLogger("mallory.refill")
        assert log.isEnabledFor(logging.INFO), "INFO is still being dropped after basicConfig"
        assert root.handlers, "basicConfig installed no handler"
    finally:
        root.handlers, root.level = saved_handlers, saved_level


def t_the_watcher_logs_being_capped_at_warning_not_info() -> None:
    """Belt and braces. Even if the level were ever turned back down, 'I am doing nothing' must
    still be audible — so that specific line is WARNING, not INFO."""
    src = (SRC / "mallory_orchestrator" / "refill.py").read_text(encoding="utf-8")
    m = re.search(r"log\.(info|warning|error)\(\s*[\"']auto-refill AT CAPACITY", src)
    assert m, "the at-capacity line is missing — a watcher that does nothing must say so"
    assert m.group(1) == "warning", (
        "at-capacity is logged at %s; it must be warning, because it is the one message that "
        "explains a fleet quietly draining" % m.group(1))


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print("  ok  %s" % fn.__name__)
    print("ALL %d LOG-VISIBILITY TESTS PASSED" % len(fns))


def test_log_visibility_suite() -> None:
    main()


if __name__ == "__main__":
    main()

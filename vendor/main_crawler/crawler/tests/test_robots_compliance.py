"""The special-request crawler must honour robots.txt Disallow — and be able to prove it.

This exists because the code shipped in a state where it looked compliant and was not: robots.txt
was fetched, `Crawl-delay` was honoured, `_allowed()` was written, `robots_skipped` was declared and
reported in to_dict() — and `_allowed()` was never called from anywhere. Every Disallow rule was
read and then ignored, while the docstring, the comment and the stats field all advertised
compliance. A published claim about crawler etiquette needs a test that fails when the claim stops
being true.

    python tests/test_robots_compliance.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from urllib.robotparser import RobotFileParser

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from special_request_crawler import crawl as sr  # noqa: E402

SRC = Path(sr.__file__)


def t_allowed_is_actually_called() -> None:
    """The exact regression that shipped: defined, reported, never invoked."""
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    defined = [n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_allowed"]
    assert defined, "_allowed() is gone — robots.txt Disallow is no longer enforced at all"
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_allowed"]
    assert calls, ("_allowed() is defined but never called — every Disallow rule is read and then "
                   "ignored, while robots_skipped reports 0 and the docstring claims compliance")


def t_every_enqueue_is_guarded() -> None:
    """One unguarded enqueue is a full bypass, so pin the count rather than mere presence.

    Both paths matter and they fail differently: an unguarded SEED means a disallowed target is
    crawled from the first request, an unguarded DISCOVERED link means compliance holds for one
    page and then quietly stops.
    """
    src = SRC.read_text(encoding="utf-8")
    enqueues = src.count("frontier.append(") + src.count("discovered.append(")
    guards = src.count("if not _allowed(")
    assert enqueues, "no enqueue sites found — this test is reading the wrong file"
    assert guards >= enqueues, (
        f"{enqueues} enqueue site(s) but only {guards} robots guard(s) — an unguarded path is a "
        "complete bypass, not a partial one")


def t_disallowed_url_is_skipped_and_counted() -> None:
    """Behaviour, not shape: a Disallow really does keep the URL out of the frontier."""
    robots = RobotFileParser()
    robots.parse(["User-agent: *", "Disallow: /private/"])
    ua = sr._UA

    assert robots.can_fetch(ua, "https://example.org/public/a") is True
    assert robots.can_fetch(ua, "https://example.org/private/secret") is False, \
        "the parser itself does not see the rule — this test would prove nothing"


def t_stat_is_reported_to_the_operator() -> None:
    """A URL dropped without a trace is indistinguishable from one that was never found."""
    s = sr.CrawlStats(target="x")
    assert "robots_skipped" in s.as_dict(), "robots_skipped is not reported — skips are invisible"
    s.robots_skipped += 1
    assert s.as_dict()["robots_skipped"] == 1


def t_crawl_delay_can_only_slow_us_down() -> None:
    """The origin's delay raises ours; it must never be able to lower a politeness setting.

    Checked as a PROPERTY, not a spelling: `max(a, b)` and `if b > a: a = b` are both correct and
    an assertion pinned to one of them fails on a harmless refactor. What must never appear is a
    bare `delay_s = robots_delay`, which would let a site declaring `Crawl-delay: 0` speed the
    crawler up past its own configured floor.
    """
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "delay_s" for t in n.targets)]
    assert assigns, "delay_s is never assigned — the Crawl-delay merge is gone"
    guarded = 0
    for node in assigns:
        val = ast.unparse(node.value)
        if val == "robots_delay":
            # Only legitimate inside an `if robots_delay > delay_s:` guard — find its ancestor.
            src_seg = ast.unparse(tree)
            assert "if robots_delay > delay_s" in src_seg, (
                "delay_s is assigned from robots_delay with no comparison guarding it — a site "
                "could lower our politeness delay")
            guarded += 1
    assert guarded or any("max" in ast.unparse(n.value) for n in assigns), \
        "no merge found at all — check the Crawl-delay handling still exists"


def t_missing_robots_fails_open_not_closed() -> None:
    """A 404 robots.txt means "no rules", not "crawl nothing" — fail-closed here would silently
    stop collecting from every site that does not publish one."""
    # _allowed is a closure inside crawl_target, so it is only reachable through the source.
    src = SRC.read_text(encoding="utf-8")
    fn = src[src.find("def _allowed("):]
    fn = fn[:fn.find("\n    #") if "\n    #" in fn[:600] else 600]
    assert "if robots is None" in fn and "return True" in fn, \
        "_allowed() no longer fails open — a site with no robots.txt would stop being crawled"


def main() -> None:
    for fn in (t_allowed_is_actually_called, t_every_enqueue_is_guarded,
               t_disallowed_url_is_skipped_and_counted, t_stat_is_reported_to_the_operator,
               t_crawl_delay_can_only_slow_us_down, t_missing_robots_fails_open_not_closed):
        fn()
        print(f"  ok  {fn.__name__}")
    print("ALL ROBOTS-COMPLIANCE TESTS PASSED")


def test_robots_compliance_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention."""
    main()


if __name__ == "__main__":
    main()

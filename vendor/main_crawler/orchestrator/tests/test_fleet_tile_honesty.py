"""The fleet tile must distinguish ANSWERING from WORKING.

Measured 2026-07-30: the tile read a confident green "12/12 · instances answering" while SIX
instances held no jobs, no frontier, and were fetching nothing. Both numbers were true — an idle
crawler answers /v1/metrics perfectly — and the tile still told the operator the opposite of what
they needed to know. They had to ask "check whether all the 12 service properly working" because the
dashboard would not.
"""
from __future__ import annotations

import re
from pathlib import Path

from mallory_orchestrator import api

HTML = Path(api.__file__).resolve().parent / "static" / "index.html"


def _rows(n_up: int, n_running: int, n_down: int = 0):
    """N instance rows in crawler_client.fanout's shape."""
    rows = []
    for i in range(n_up):
        rows.append({"instance": i, "ok": True, "data": {
            "batch": {"running": i < n_running, "total": 5 if i < n_running else 0, "done": 0},
            "progress": {"pages": 10, "running": i < n_running, "totals": {}},
            "lanes": {}, "adaptive": {}, "pool": {},
        }})
    for j in range(n_down):
        rows.append({"instance": n_up + j, "ok": False, "error": "no answer"})
    return rows


def test_crawling_counts_only_instances_with_a_running_batch():
    out = api._merge_metrics(_rows(n_up=12, n_running=6))
    inst = out["instances"]
    assert inst["up"] == 12, "all twelve answered"
    assert inst["crawling"] == 6, f"six are working, got {inst['crawling']}"
    assert inst["idle"] == 6, f"six are idle, got {inst['idle']}"


def test_a_fully_busy_fleet_reports_no_idle():
    inst = api._merge_metrics(_rows(n_up=12, n_running=12))["instances"]
    assert (inst["crawling"], inst["idle"]) == (12, 0)


def test_a_fully_idle_fleet_is_not_reported_as_healthy():
    """The worst case: every instance answers, none is crawling."""
    inst = api._merge_metrics(_rows(n_up=12, n_running=0))["instances"]
    assert inst["up"] == 12
    assert inst["crawling"] == 0
    assert inst["idle"] == 12


def test_a_down_instance_is_not_counted_as_crawling():
    inst = api._merge_metrics(_rows(n_up=8, n_running=8, n_down=4))["instances"]
    assert inst["up"] == 8 and inst["down"] == 4 and inst["total"] == 12
    assert inst["crawling"] == 8, "a down instance cannot be crawling"
    assert inst["idle"] == 0, "idle counts ANSWERING-but-not-working, not down"


def test_the_tile_renders_the_crawling_count_not_the_up_count():
    """Guards against the API reporting it and the tile still showing `up` — the shape of every
    unwired fix in this codebase."""
    html = HTML.read_text(encoding="utf-8")
    m = re.search(r'tile\("Crawler fleet"[^\n]*\n?[^\n]*', html)
    assert m, "the Crawler fleet tile is gone"
    # The window spans the whole IIFE that builds this tile — from the `down` branch through the
    # cap/idle branches. Sized generously on purpose: a fixed 1,500 chars silently excluded the
    # headline the moment the block grew, and the test then failed for the wrong reason.
    start = html.index('tile("Crawler fleet"') - 1200
    block = html[start: html.rindex('tile("Crawler fleet"') + 1500]
    assert "h.instances.crawling" in block, "the tile never reads `crawling`"
    assert "${busy}/${tot}" in block, "the headline still shows the answering count, not the working one"
    assert "idle with no work" in block, "an idle instance is not called out to the operator"


def test_the_tile_says_so_when_the_api_cannot_report_work():
    """An older API has no `crawling` key. Falling back to a silent green would reintroduce the
    original lie in a new place."""
    html = HTML.read_text(encoding="utf-8")
    assert "work not measured" in html, \
        "a missing `crawling` must be stated, not smoothed over into an implied all-clear"

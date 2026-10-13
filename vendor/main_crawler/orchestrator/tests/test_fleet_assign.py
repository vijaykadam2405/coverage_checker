"""The "check the crawlers and assign work" button.

Three separate pieces already existed — a health check, a refill, a rebalance — and NONE of them was
reachable from the dashboard, so the answer to "instance 4 is idle" was a shell session. These tests
are about the two ways one button over three actions goes wrong:

  * doing them in the wrong order (rebalancing an instance that only needed resuming), and
  * reporting success when nothing actually changed.

The last test is the one that keeps being needed in this codebase: an endpoint that works and a
button that never calls it.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from mallory_orchestrator import api


class _Src:
    def __init__(self, domain, seed_urls=None):
        self.domain = domain
        self.category = "manufacturer_ir"
        # Mirrors the real Source column. None = never probed, so the job builder falls back to the
        # conventional https://<apex>/ — which is what every case in this file assumes.
        self.seed_urls = seed_urls


class _FakeDb:
    """What step 3 reads. `never` = domains the corpus has no pages for, `tried` = domains with a
    job_runs row. Everything is owned by `owner` (shard.owner_of is stubbed to say so)."""

    def __init__(self, never, owner, tried=()):
        self._never = list(never)
        self._tried = list(tried)
        self._owner = owner
        import mallory_orchestrator.shard as _shard
        _shard.owner_of = lambda db, d, n: owner          # noqa: ARG005

    def execute(self, _stmt):
        # job_runs.domain, distinct -- what the fleet has ALREADY TRIED.
        return [(d,) for d in self._tried]

    def scalars(self, _stmt):
        class R:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows
        return R([_Src(d) for d in self._never] + [_Src("already-ran.com")])


def _fleet(specs):
    """specs: list of (crawling, pending). Index = instance number."""
    return [{"instance": i, "ok": True, "data": {
        "batch": {"running": c, "total": 3 if c else 0},
        "lanes": {"frontier": p, "render_q": 0},
    }} for i, (c, p) in enumerate(specs)]


@pytest.fixture()
def wired(monkeypatch):
    """Point the fleet at a scripted set of instances; capture what gets called."""
    calls = {"refill": [], "rebalance": 0}

    def _go(rows_before, rows_after=None, refill_result=None, rebalance=None):
        seq = [rows_before, rows_after if rows_after is not None else rows_before]

        class C:
            def fanout(self, fn, *a, **kw):
                return seq[0] if len(seq) > 1 and not calls["refill"] else seq[-1]

            def get_metrics(self, *a, **kw):
                return {}

            def pool(self):
                return ["http://x"] * len(rows_before)

        monkeypatch.setattr(api, "crawler_client", C())
        # The dispatch policy (test_dispatch_policy.py) is read before the never-crawled dispatch;
        # open here, because _FakeDb has no Setting store and these cases are about the three steps.
        monkeypatch.setattr(api.scheduler, "lane_open", lambda db, lane: True)

        class R:
            @staticmethod
            def refill_once(max_jobs=200, force=False):
                calls["refill"].append({"force": force})
                return refill_result or {"checked": len(rows_before), "idle": 0, "resumed": 0,
                                         "instances": [], "errors": []}

        # BOTH, and the package attribute is the one that matters. `from . import refill` reads
        # the attribute off the already-imported package and only falls back to an import when it
        # is missing — so patching sys.modules alone passes when this file runs on its own and is
        # silently ignored once any earlier test in the suite has imported refill. That is a test
        # that reports green while exercising the real module.
        import sys

        import mallory_orchestrator
        monkeypatch.setitem(sys.modules, "mallory_orchestrator.refill", R)
        monkeypatch.setattr(mallory_orchestrator, "refill", R, raising=False)

        def _reb(db=None):
            calls["rebalance"] += 1
            if isinstance(rebalance, Exception):
                raise rebalance
            return rebalance or {"moved": 0}

        monkeypatch.setattr(api, "crawler_fleet_rebalance", _reb)
        return calls

    return _go


def test_an_idle_instance_holding_work_is_resumed_not_rebalanced():
    """The cheap fix first. Rebalancing costs a whole stop/resume cycle and would not have helped."""
    calls = {"n": 0}
    # Straight assertion on the ordering rule the endpoint encodes, kept independent of the
    # monkeypatch plumbing: rebalance is only reached when someone is idle with an EMPTY frontier.
    src = (Path(api.__file__)).read_text(encoding="utf-8")
    body = src.split("def crawler_fleet_assign(")[1].split("\n@router")[0]
    assert 'before.get("idle_empty")' in body, "rebalance is not gated on idle-with-nothing"
    assert body.index("refill_once") < body.index("crawler_fleet_rebalance"), \
        "rebalance runs before refill — that wastes a cycle on a problem resume fixes"
    assert calls["n"] == 0


def test_refill_is_forced_so_a_stale_stop_flag_does_not_block_the_button(wired):
    """refill skips stop_pending on the TIMER to protect a deliberate stop. Pressing the button is
    the operator deciding to override exactly that — the commonest cause of a stuck idle instance."""
    calls = wired(_fleet([(True, 100), (False, 5188)]))
    api.crawler_fleet_assign(db=None)
    assert calls["refill"] == [{"force": True}], calls["refill"]


def test_the_master_switch_still_wins(wired):
    """force overrides a stale per-instance flag, NOT the operator's halt. Undoing that quietly is
    the "something restarted my crawl" bug this codebase has already had twice."""
    wired(_fleet([(False, 900)]), refill_result={"halted": True, "checked": 1, "resumed": 0})
    out = api.crawler_fleet_assign(db=None)
    assert out["ok"] is False and out["halted"] is True
    assert "master switch" in out["summary"]


def test_nothing_to_do_says_so_rather_than_claiming_success(wired):
    wired(_fleet([(True, 500)] * 4))
    out = api.crawler_fleet_assign(db=None)
    assert out["resumed"] == 0 and out["moved"] == 0
    assert "already had work" in out["summary"]


def test_an_instance_still_idle_afterwards_gets_never_crawled_work(wired, monkeypatch):
    """The gap that made the first version of this button useless.

    An instance owning no unfinished domain cannot be helped by a resume, and a rebalance only moves
    OWNERSHIP — the domains it gains are finished ones whose next cadence tick was measured six days
    out. So it stayed idle for six days and the button said so politely. Real work exists: 213 of
    807 catalogued sites had never been crawled at all.
    """
    wired(_fleet([(True, 900), (False, 0)]))
    seen = {}
    monkeypatch.setattr(api.orchestrate, "manual_status", lambda: {"running": False})
    monkeypatch.setattr(api.scheduler, "master_enabled", lambda db: True)
    monkeypatch.setattr(api, "_ingest_get",
                        lambda p, timeout=0: [{"source_id": "already-ran.com", "pages": 12}])

    class T:
        def __init__(self, target=None, args=(), daemon=None):
            seen["args"] = args

        def start(self):
            seen["started"] = True

    monkeypatch.setattr(api, "Thread", T)
    out = api.crawler_fleet_assign(db=_FakeDb(never=["fresh-a.com", "fresh-b.com"], owner=1))
    assert seen.get("started") is True, "never-crawled work was found but never dispatched"
    assert out["started_sites"] == 2
    assert "dispatched 2 never-crawled site(s) to instance(s) 1" in out["summary"], out["summary"]
    # The job has to be the shape the crawler actually accepts. jobgen.generate returns NOTHING for
    # these sources (it needs seed_urls, which 0 of 807 rows have), so building it here is the whole
    # point — and a job without seed_urls would dispatch and crawl nothing at all.
    jobs = seen["args"][0]
    assert [j["seed_urls"] for j in jobs] == [["https://fresh-a.com/"], ["https://fresh-b.com/"]]
    assert all(j["source_id"] and j["job_type"] and j["same_domain_only"] for j in jobs)


def test_a_finished_domain_is_NOT_re_dispatched_just_to_fill_a_slot(wired, monkeypatch):
    """Busywork is worse than idleness: it costs real fetches and buries the cadence signal."""
    wired(_fleet([(True, 900), (False, 0)]))
    monkeypatch.setattr(api.orchestrate, "manual_status", lambda: {"running": False})
    monkeypatch.setattr(api.scheduler, "master_enabled", lambda db: True)
    monkeypatch.setattr(api, "_ingest_get",
                        lambda p, timeout=0: [{"source_id": "already-ran.com", "pages": 12}])
    out = api.crawler_fleet_assign(db=_FakeDb(never=[], owner=1))   # every domain already ran
    assert out["started_sites"] == 0
    assert "uncrawled and untried" in out["summary"], out["summary"]


def test_the_master_switch_blocks_the_new_dispatch_too(wired, monkeypatch):
    wired(_fleet([(True, 900), (False, 0)]))
    monkeypatch.setattr(api.orchestrate, "manual_status", lambda: {"running": False})
    monkeypatch.setattr(api.scheduler, "master_enabled", lambda db: False)
    out = api.crawler_fleet_assign(db=_FakeDb(never=["fresh.com"], owner=1))
    assert out["started_sites"] == 0
    assert "master switch is off" in out["summary"]


def test_a_refused_rebalance_is_reported_not_raised(wired, monkeypatch):
    """rebalance 503s when an instance is unreachable — correct, but it must not turn the whole
    button into a red error when the resume half may well have worked."""
    wired(_fleet([(True, 900), (False, 0)]),
          rebalance=HTTPException(503, "1 instance(s) unreachable"))
    monkeypatch.setattr(api.orchestrate, "manual_status", lambda: {"running": True})
    out = api.crawler_fleet_assign(db=None)
    assert out["ok"] is True
    assert "rebalance refused" in out["summary"]


def test_an_unreachable_fleet_is_not_reported_as_success(wired):
    wired([{"instance": 0, "ok": False, "error": "no answer"}])
    out = api.crawler_fleet_assign(db=None)
    assert out["ok"] is False
    assert "no crawler instance answered" in out["summary"]


def test_the_button_exists_and_calls_the_endpoint():
    """The recurring failure in this codebase: the endpoint is right and nothing reaches it.
    /api/crawler/fleet/refill and /rebalance both shipped working and unreachable."""
    html = (Path(api.__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="btnAssign"' in html, "no button"
    assert '$("#btnAssign").onclick' in html, "the button has no handler"
    assert "/api/crawler/fleet/assign" in html, "the handler does not call the endpoint"
    # It has to render the per-instance result, or it is a spinner that tells the operator nothing.
    handler = html.split('$("#btnAssign").onclick')[1][:2600]
    assert "per_instance" in handler, "the result does not show what each instance is doing"
    assert "summary" in handler, "the result does not show what was actually done"


def test_the_endpoint_is_registered():
    # The router carries the /api prefix, so this is the path the button actually POSTs to.
    paths = {r.path for r in api.router.routes}
    assert "/api/crawler/fleet/assign" in paths, sorted(p for p in paths if "fleet" in p)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))


def test_a_site_already_TRIED_is_not_dispatched_again(wired, monkeypatch):
    """The 29 sites handed to instances 3 and 8 came back DNS-dead, refused or geo-blocked —
    kept=0 on every one. Without a memory of the attempt the button re-dispatches that same dead
    list on every press, reports "dispatched 29" each time, and the instances go straight back to
    idle. No pages in the corpus is not the same fact as never tried."""
    wired(_fleet([(True, 900), (False, 0)]))
    monkeypatch.setattr(api.orchestrate, "manual_status", lambda: {"running": False})
    monkeypatch.setattr(api.scheduler, "master_enabled", lambda db: True)
    monkeypatch.setattr(api, "_ingest_get", lambda p, timeout=0: [])   # corpus has nothing
    out = api.crawler_fleet_assign(
        db=_FakeDb(never=["dead-a.com", "dead-b.com"], owner=1,
                   tried=["dead-a.com", "dead-b.com", "already-ran.com"]))
    assert out["started_sites"] == 0, "a site that was already tried was dispatched again"
    assert "uncrawled and untried" in out["summary"], out["summary"]


def test_the_site_drilldown_falls_back_when_the_shard_owner_has_nothing(monkeypatch):
    """After move_frontier, a host's HISTORY and its FRONTIER can live on different instances.

    The drill-down asks only the shard owner (a deliberate optimisation: 30.3s -> 0.7s across twelve
    instances). That assumes a host lives on exactly one container, which move_frontier breaks —
    pending URLs move, rows already crawled stay under the old instance's run id. Measured after a
    237-job rebalance: owner_of(saab.com) = instance 1 with 0 rows while instance 4 held the whole
    report, and the panel claimed there was no report for a site with 24 unresolved URLs.
    """
    calls = {"one": 0, "fan": 0}
    REPORT = {"sites": [{"host": "www.saab.com", "kept": 1, "retryable": 24, "terminal": 0,
                         "reasons": {"http_404": 21, "render_crash": 3}, "urls": []}],
              "run_ids": ["r1"], "totals": {}}

    class C:
        def pool(self):
            return ["http://a", "http://b"]

        def fanout_one(self, i, base, fn, **kw):
            calls["one"] += 1
            return {"instance": i, "ok": True, "data": {"sites": []}}     # owner holds nothing

        def fanout(self, fn, **kw):
            calls["fan"] += 1
            return [{"instance": 1, "ok": True, "data": REPORT}]

        def batch_report(self, *a, **kw):
            return {}

    monkeypatch.setattr(api, "crawler_client", C())
    import mallory_orchestrator.shard as _shard
    monkeypatch.setattr(_shard, "owner_of", lambda db, d, n: 0, raising=False)

    out = api.crawler_batch_sites(site="saab.com", db=None)
    assert calls["one"] == 1, "the fast single-instance path was skipped"
    assert calls["fan"] == 1, "it did not fall back when the owner returned nothing"
    assert [s["host"] for s in out["sites"]] == ["www.saab.com"]
    assert out["sites"][0]["retryable"] == 24

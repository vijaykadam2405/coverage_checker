"""The invariants exist because every fault they guard PASSED ITS UNIT TESTS.

That is the whole argument for this module, and it is the thing these tests have to protect. A
check that quietly returns True when it could not measure anything reproduces the exact failure it
was written to catch, so most of what follows is about the not-measured path.
"""
from __future__ import annotations

import pytest

from mallory_orchestrator import selfcheck as sc


class _DB:
    pass


# ── 1. silent success on the write path ───────────────────────────────────────────────────────

def test_an_accepted_document_that_never_lands_is_a_FAILURE(monkeypatch):
    """THE fault, live: POST /ingest/v1/page answered 200 {"status":"accepted"} and the row never
    reached Postgres, while the fleet's own writes landed and every read tile stayed green."""
    from mallory_orchestrator import api
    monkeypatch.setattr(api, "_ingest_post", lambda p, b, **k: {"status": "accepted"})
    monkeypatch.setattr(api, "_ingest_get", lambda p, **k: [])        # nothing ever appears
    monkeypatch.setattr(sc.time, "sleep", lambda *_a: None)
    c = sc.check_write_path()
    assert c.ok is False
    assert "never reached the store" in c.detail


def test_a_document_that_reads_back_passes(monkeypatch):
    from mallory_orchestrator import api
    monkeypatch.setattr(api, "_ingest_post", lambda p, b, **k: {"status": "accepted"})
    monkeypatch.setattr(api, "_ingest_get",
                        lambda p, **k: [{"source_id": sc.CANARY_HOST, "pages": 1}])
    monkeypatch.setattr(sc.time, "sleep", lambda *_a: None)
    assert sc.check_write_path().ok is True


def test_an_unreachable_ingest_is_NOT_MEASURED_not_a_pass(monkeypatch):
    """The distinction the whole module rests on. 'I could not check' must never render as 'fine' —
    a green tick nobody computed is how several of these hid for months."""
    from mallory_orchestrator import api

    def _boom(*_a, **_k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(api, "_ingest_post", _boom)
    c = sc.check_write_path()
    assert c.ok is None and "could not reach" in c.detail


# ── 2. the tier check must be able to read its own data source ────────────────────────────────

def test_tier_counts_are_read_from_the_REAL_payload_shape(monkeypatch):
    """The exact body /api/live/types returns, verbatim from production. This check read `n`/`count`
    while the API returns `pages`, so every row evaluated to zero and it reported "only 0 page(s)
    carry a tier label" while the corpus held 384k labelled pages — silent success reproduced
    inside the module written to catch silent success."""
    from mallory_orchestrator import api
    monkeypatch.setattr(api, "_ingest_get", lambda p, **k: {"served_by": [
        {"served_by": "c1_live", "pages": 378857, "html_bytes": 79163766173},
        {"served_by": "c3_camofox", "pages": 4941, "html_bytes": 1044164461},
        {"served_by": "c4_host_solver", "pages": 330, "html_bytes": 80768737},
    ]})
    c = sc.check_tiers_are_reached()
    assert c.ok is True, c.detail
    assert c.evidence["c4_host_solver"] == 330
    assert "c4_host_solver" in c.detail


# ── the C4 solver: off, dead, or on only some of the fleet ────────────────────────────────────

def _tiers(monkeypatch, *per_instance):
    """per_instance: one c4 dict (or None to omit the key entirely) per instance."""
    from mallory_orchestrator import crawler_client
    rows = [{"instance": i, "ok": True,
             "data": ({"c1": {"up": True}} if c4 is None else {"c1": {"up": True}, "c4": c4})}
            for i, c4 in enumerate(per_instance)]
    monkeypatch.setattr(crawler_client, "fanout", lambda *a, **k: rows)


UP = {"enabled": True, "up": True, "detail": "browser idle · 0 host(s) solved"}
DEAD = {"enabled": True, "up": False, "detail": "unreachable"}
OFF = {"enabled": False, "up": False, "detail": "disabled"}


def test_a_configured_but_unreachable_solver_is_a_FAILURE(monkeypatch):
    """The whole reason this check exists: hostsolver.solve() swallows the error on purpose, so a
    dead C4 looks exactly like a crawl that met no walls. It died three times in one day unnoticed."""
    monkeypatch.setattr(sc.time, "sleep", lambda *_a: None)
    _tiers(monkeypatch, DEAD, DEAD)
    c = sc.check_c4_is_uniform_across_the_fleet()
    assert c.ok is False and "silently skipped" in c.detail


def test_a_fleet_wide_blip_is_RECONFIRMED_before_being_called_an_outage(monkeypatch):
    """Class 5 — one probe, a verdict — committed by this very check on its second live run. Every
    crawler caches /v1/tiers for 30s, so the first fanout after a restart fires twelve simultaneous
    probes down ONE ssh tunnel; all twelve timed out and the check declared C4 dead while a curl
    from inside a container answered fine. Twelve services do not die in the same instant."""
    from mallory_orchestrator import crawler_client
    calls = {"n": 0}

    def _fanout(*_a, **_k):
        calls["n"] += 1
        c4 = DEAD if calls["n"] == 1 else UP          # the herd clears on the second look
        return [{"instance": i, "ok": True, "data": {"c4": c4}} for i in range(3)]
    monkeypatch.setattr(crawler_client, "fanout", _fanout)
    monkeypatch.setattr(sc.time, "sleep", lambda *_a: None)
    c = sc.check_c4_is_uniform_across_the_fleet()
    assert calls["n"] == 2, "declared an outage without looking twice"
    assert c.ok is True


def test_a_PARTIAL_outage_is_not_re_probed(monkeypatch):
    """Only an all-at-once failure is herd-shaped. One instance down among healthy siblings is a
    real, specific fault and must be reported on the first look, not softened by a retry."""
    from mallory_orchestrator import crawler_client
    calls = {"n": 0}

    def _fanout(*_a, **_k):
        calls["n"] += 1
        return [{"instance": 0, "ok": True, "data": {"c4": UP}},
                {"instance": 1, "ok": True, "data": {"c4": DEAD}}]
    monkeypatch.setattr(crawler_client, "fanout", _fanout)
    monkeypatch.setattr(sc.time, "sleep", lambda *_a: None)
    c = sc.check_c4_is_uniform_across_the_fleet()
    assert calls["n"] == 1 and c.ok is False and c.evidence["down"] == [1]


def test_c4_on_SOME_instances_is_a_failure(monkeypatch):
    """The partial fleet. The env var is per-container, so recreating eleven instances while the
    twelfth is mid-batch leaves C4 on eleven — and whether a walled site gets fetched then depends
    on which shard owns it. The same shape as twelve containers silently running a MIX of images."""
    _tiers(monkeypatch, UP, UP, OFF)
    c = sc.check_c4_is_uniform_across_the_fleet()
    assert c.ok is False and c.evidence["off"] == [2] and "which shard owns it" in c.detail


def test_c4_up_everywhere_passes(monkeypatch):
    _tiers(monkeypatch, UP, UP, UP)
    c = sc.check_c4_is_uniform_across_the_fleet()
    assert c.ok is True and c.evidence["on"] == [0, 1, 2]


def test_one_unreadable_instance_defeats_the_UNIFORMITY_claim(monkeypatch):
    """Caught on this check's own first live run. Eleven instances answered "C4 up" and the twelfth
    was mid-batch on an older image with no c4 key — and the check reported "all 11 instance(s)"
    and went green on a fleet of TWELVE. Uniformity is the claim, so the instance we could not read
    is exactly the one that might break it."""
    _tiers(monkeypatch, UP, UP, None)
    c = sc.check_c4_is_uniform_across_the_fleet()
    assert c.ok is None, "green on a fleet it could not fully read"
    assert c.evidence["unreadable"] == 1 and "cannot confirm" in c.detail


def test_c4_switched_off_everywhere_is_NOT_MEASURED_not_a_failure(monkeypatch):
    """Off fleet-wide is a deployment choice, not a fault — interactive walls are recorded and
    skipped by design. Alarming on it would train people to ignore the panel."""
    _tiers(monkeypatch, OFF, OFF)
    assert sc.check_c4_is_uniform_across_the_fleet().ok is None


def test_an_image_without_the_c4_probe_is_UNKNOWN_not_off(monkeypatch):
    """An older image has no c4 key. Reading a missing field as a value is exactly how the
    wedged-instance check produced its first false positive — absent is unknown, never zero."""
    _tiers(monkeypatch, None, None)
    c = sc.check_c4_is_uniform_across_the_fleet()
    assert c.ok is None and c.evidence["unreadable"] == 2


# ── 3. a cap applied before the key it should respect ─────────────────────────────────────────

def _sched(monkeypatch, free, due_domains):
    # Another suite installs a _FakeScheduler into sys.modules and never restores it, so both
    # `from . import scheduler` and importlib hand back the fake — and the check resolves the
    # module at CALL time, so patching the fake changes nothing. Drop it, load the real module,
    # and let monkeypatch put the fake back afterwards so that suite still passes.
    import importlib
    import sys
    sys.modules.pop("mallory_orchestrator.scheduler", None)
    real = importlib.import_module("mallory_orchestrator.scheduler")
    monkeypatch.setitem(sys.modules, "mallory_orchestrator.scheduler", real)
    crawler_client = importlib.import_module("mallory_orchestrator.crawler_client")
    scheduler = real
    shard = importlib.import_module("mallory_orchestrator.shard")

    class _J:
        def __init__(self, d):
            self.job = {"seed_urls": ["https://%s/" % d]}
    monkeypatch.setattr(scheduler, "free_instances", lambda *a, **k: free, raising=False)
    monkeypatch.setattr(scheduler, "due_jobs", lambda *a, **k: [_J(d) for d in due_domains], raising=False)
    monkeypatch.setattr(scheduler, "utcnow", lambda: "NOW", raising=False)
    monkeypatch.setattr(crawler_client, "pool", lambda: ["b"] * 12, raising=False)
    monkeypatch.setattr(shard, "instance_for",
                        lambda db, seed, n: 0 if "stranded" in seed else 7, raising=False)


def test_due_work_owned_by_an_IDLE_instance_is_a_failure(monkeypatch):
    """Measured twice on two axes: the due list cut by priority before owners were matched, then
    the free list cut by index before owners were matched. Both showed a full queue and idle
    containers, and neither raised anything."""
    _sched(monkeypatch, free=[0, 1], due_domains=["stranded.com", "busy.com"])
    c = sc.check_no_idle_instance_owns_due_work(_DB())
    assert c.ok is False and c.evidence["stranded"] == 1


def test_work_concentrated_on_busy_instances_is_fine(monkeypatch):
    """The healthy shape — domains are assign-once sharded, so a free instance owning nothing is
    correct and must not be reported as a fault."""
    _sched(monkeypatch, free=[0, 1], due_domains=["busy.com"])
    import importlib
    shard = importlib.import_module("mallory_orchestrator.shard")
    monkeypatch.setattr(shard, "instance_for", lambda db, seed, n: 7, raising=False)
    assert sc.check_no_idle_instance_owns_due_work(_DB()).ok is True


# ── 4. a frozen copy that stopped agreeing with its source ────────────────────────────────────

class _Src:
    def __init__(self, domain, seed):
        self.domain, self.seed_urls = domain, ([seed] if seed else None)


class _Job:
    def __init__(self, name, seed, enabled=True):
        self.name, self.job, self.enabled = name, {"seed_urls": [seed]}, enabled


class _SeedDB:
    def __init__(self, srcs, jobs):
        self._s, self._j = srcs, jobs

    def scalars(self, stmt):
        from mallory_orchestrator.models import ScheduledJob
        want = stmt.column_descriptions[0]["entity"]
        rows = self._j if want is ScheduledJob else self._s
        return type("_S", (), {"all": lambda _x, r=rows: list(r)})()


def test_a_schedule_crawling_a_different_url_than_the_catalogue_is_drift():
    """maxar.com was re-pointed at vantor.com in the catalogue and the schedule kept crawling the
    dead apex — the copy is what the crawl actually uses."""
    db = _SeedDB([_Src("maxar.com", "https://vantor.com/")],
                 [_Job("maxar.com", "https://maxar.com/")])
    c = sc.check_schedule_seeds_match_catalogue(db)
    assert c.ok is False and c.evidence["total"] == 1


def test_agreeing_copies_pass():
    db = _SeedDB([_Src("maxar.com", "https://vantor.com/")],
                 [_Job("maxar.com", "https://vantor.com/")])
    assert sc.check_schedule_seeds_match_catalogue(db).ok is True


def test_a_disabled_schedule_is_not_drift():
    db = _SeedDB([_Src("x.com", "https://new.x.com/")],
                 [_Job("x.com", "https://x.com/", enabled=False)])
    assert sc.check_schedule_seeds_match_catalogue(db).ok is True


# ── 8. an instance holding a running flag with no work ────────────────────────────────────────

def test_running_with_an_empty_queue_is_flagged(monkeypatch):
    """It cost an hour of a twelve-instance fleet and reads as normal cap behaviour on every
    dashboard: '2 crawling, 10 idle'."""
    from mallory_orchestrator import crawler_client
    monkeypatch.setattr(crawler_client, "fanout", lambda *a, **k: [
        {"instance": 6, "ok": True, "data": {"running": True, "hosts": [], "current_job": None}},
        {"instance": 1, "ok": True, "data": {"running": True, "hosts": ["x.com"],
                                             "current_job": "j"}},
    ])
    c = sc.check_no_wedged_instance()
    assert c.ok is False and c.evidence["suspect"] == [6]


def test_a_busy_instance_with_a_real_frontier_is_fine(monkeypatch):
    from mallory_orchestrator import crawler_client
    monkeypatch.setattr(crawler_client, "fanout", lambda *a, **k: [
        {"instance": 1, "ok": True, "data": {"running": True, "hosts": ["amentum.com"],
                                             "current_job": "restore_x"}}])
    assert sc.check_no_wedged_instance().ok is True


def test_an_ABSENT_field_is_unknown_not_empty(monkeypatch):
    """My own false positive, live: the status payload carries `hosts`/`current_job`, not
    `pending`/`current_domains`. Reading the missing names as empty flagged three instances that
    were busily crawling amentum.com, tycos.com and new.diehl.com."""
    from mallory_orchestrator import crawler_client
    monkeypatch.setattr(crawler_client, "fanout", lambda *a, **k: [
        {"instance": 5, "ok": True, "data": {"running": True, "pending": None}}])
    c = sc.check_no_wedged_instance()
    assert c.ok is None and "cannot tell" in c.detail


def test_an_unreachable_fleet_is_not_measured(monkeypatch):
    from mallory_orchestrator import crawler_client

    def _boom(*_a, **_k):
        raise RuntimeError("down")
    monkeypatch.setattr(crawler_client, "fanout", _boom)
    assert sc.check_no_wedged_instance().ok is None


# ── the runner ────────────────────────────────────────────────────────────────────────────────

def test_run_all_never_raises(monkeypatch):
    """It runs on the autopilot timer. A self-check that dies is worse than none."""
    for fn in sc.CHECKS:
        monkeypatch.setattr(sc, fn.__name__, lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    out = sc.run_all(_DB())
    assert isinstance(out, dict) and "checks" in out


def test_not_measured_is_reported_separately_from_failure(monkeypatch):
    """Conflating the two is the bug: a check that could not run must not be counted as passing,
    and must not be alarmed as failing either."""
    monkeypatch.setattr(sc, "CHECKS", (lambda _db: sc.Check("a", "k", None, "dunno"),
                                       lambda _db: sc.Check("b", "k", True, "fine")))
    out = sc.run_all(_DB())
    assert out["failed"] == 0 and out["not_measured"] == 1 and out["ok"] is True
    assert "not measured" in out["summary"]


def test_the_autopilot_runs_the_invariants():
    from pathlib import Path
    from mallory_orchestrator import autopilot
    body = Path(autopilot.__file__).read_text(encoding="utf-8").split("def run_once")[1]
    assert "selfcheck_run" in body, "the invariants are a page nobody opens"


def test_the_endpoint_is_registered():
    from mallory_orchestrator import api
    assert "/api/selfcheck" in {r.path for r in api.router.routes}


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))


# ── the canary must not become a source ───────────────────────────────────────────────────────

def test_the_canary_url_is_STABLE_so_it_cannot_accumulate(monkeypatch):
    """It used a fresh URL per tick while the docstring claimed it removed them. On a 15-minute
    autopilot that is one junk corpus row every tick — measured at 39 pages / 26,520 chars, filed
    under an uncatalogued host and shown on the operator's dashboard beside two real sites."""
    from mallory_orchestrator import api
    seen = []
    monkeypatch.setattr(api, "_ingest_post",
                        lambda p, b, **k: seen.append(b["document"]["url"]) or {"status": "accepted"})
    monkeypatch.setattr(api, "_ingest_get",
                        lambda p, **k: [{"source_id": sc.CANARY_HOST, "pages": 1}])
    monkeypatch.setattr(sc.time, "sleep", lambda *_a: None)
    sc.check_write_path()
    sc.check_write_path()
    assert len(set(seen)) == 1, "two ticks must write the SAME url, not two corpus rows"


def test_two_ticks_still_write_DIFFERENT_content(monkeypatch):
    """Stable URL must not mean a no-op write — a store that silently stopped accepting has to
    still be caught, which needs the row genuinely re-written each time."""
    from mallory_orchestrator import api
    hashes = []
    monkeypatch.setattr(api, "_ingest_post",
                        lambda p, b, **k: hashes.append(b["document"]["content_hash"]) or {"status": "ok"})
    monkeypatch.setattr(api, "_ingest_get",
                        lambda p, **k: [{"source_id": sc.CANARY_HOST, "pages": 1}])
    monkeypatch.setattr(sc.time, "sleep", lambda *_a: None)
    sc.check_write_path()
    sc.check_write_path()
    assert len(set(hashes)) == 2


def test_the_canary_is_filtered_out_of_every_sources_listing(monkeypatch):
    """It is a health probe, not a defence manufacturer. Filtered at the single call all five
    sources call sites share, so they cannot drift apart."""
    from mallory_orchestrator import api

    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return [{"source_id": "bae.com", "pages": 10},
                    {"source_id": sc.CANARY_HOST, "pages": 39}]
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R())
    rows = api._ingest_get("/api/live/sources?limit=5000")
    assert [r["source_id"] for r in rows] == ["bae.com"]


def test_other_ingest_paths_are_untouched(monkeypatch):
    """The filter is scoped to sources listings — it must not silently reshape other payloads."""
    from mallory_orchestrator import api

    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"types": [1, 2, 3]}
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R())
    assert api._ingest_get("/api/live/types") == {"types": [1, 2, 3]}


def test_the_canary_filter_does_not_BLIND_the_write_path_check(monkeypatch):
    """A real integration bug, caught only by not stubbing.

    check_write_path reads its canary back THROUGH _ingest_get. Filtering the canary
    unconditionally made `landed` permanently False, so the silent-success detector would raise a
    false silent-success alarm every 15 minutes. Every existing test passed, because they all
    monkeypatch _ingest_get and never exercise the filter.

    So this one stubs only HTTP and lets the real filter run.
    """
    from mallory_orchestrator import api

    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return [{"source_id": "bae.com", "pages": 10},
                    {"source_id": sc.CANARY_HOST, "pages": 1}]
    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R())
    monkeypatch.setattr(api, "_ingest_post", lambda p, b, **k: {"status": "accepted"})
    monkeypatch.setattr(sc.time, "sleep", lambda *_a: None)

    # the listing a dashboard asks for: canary hidden
    assert [r["source_id"] for r in api._ingest_get("/api/live/sources?limit=5000")] == ["bae.com"]
    # the read-back the check asks for: canary VISIBLE, because it named it
    c = sc.check_write_path()
    assert c.ok is True, "the filter blinded the check that depends on seeing the canary"

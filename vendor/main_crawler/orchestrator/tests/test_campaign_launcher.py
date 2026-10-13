"""The campaign launcher: list -> plan -> approve -> run, on top of CAMPAIGN mode.

The reference is the hand run of 2026-09-19 (comp_campaign.py + dc_dispatch.py): 49 competitor
domains, the audit's tier map, phase 1 whole-site breadth + phase 2 dated newsroom, dispatched as
background batches to the instances owning each domain and polled. The golden file
fixtures/campaign_reference_jobs.json is the 84 fleet jobs that run emitted; the launcher must
produce them byte-for-byte from the same list and options, or it is a different crawl wearing
the same name.

Beyond the golden: tiering from a probe (walls, shells, dead hosts, geo-bans), the options (scope,
PDFs), honest estimates, and the run model — a plan dispatches NOTHING; Go switches the mode and
dispatches to the campaign lane; Stop hard-stops, stays stopped, and the mode returns to normal
with the campaign's frontiers parked.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from mallory_orchestrator import api, campaigns, daily, orchestrate, policy, refill, scheduler  # noqa: E402
from mallory_orchestrator.models import Base, Campaign, DailyUrl, DomainShard, ScheduleLog  # noqa: E402

GOLDEN = Path(__file__).parent / "fixtures" / "campaign_reference_jobs.json"
NOW = dt.datetime(2026, 9, 20, 12, 0, 0)
N = 4

# comp_campaign.py DOMAINS, values only (KNDS FR/DE share knds.com).
REFERENCE_DOMAINS = [
    "aweil.in", "knds.com", "baesystems.com", "leonardo.com", "rheinmetall.com",
    "hanwhaaerospace.com", "elbitsystems.com", "gd.com", "norinco.com", "tataadvancedsystems.com",
    "mahindra.com", "larsentoubro.com", "patriagroup.com", "supacat.com", "otokar.com.tr",
    "paramountgroup.com", "roshel.com", "idvgroup.com", "iwi.net", "rostec.ru",
    "munitionsindia.in", "bdl-india.in", "hsw.pl", "oshkoshdefense.com", "poongsan.co.kr",
    "nammo.com", "sigsauer.com", "sssdefence.com", "rafael.co.il", "kongsberg.com",
    "mbda-systems.com", "iai.co.il", "l3harris.com", "northropgrumman.com", "lockheedmartin.com",
    "rtx.com", "saab.com", "thyssenkrupp.com", "naval-group.com", "hii.com",
    "babcockinternational.com", "diehl.com", "avinc.com", "anduril.com", "uvisionuav.com",
    "adanidefence.com", "brahmos.com", "pelgel.com", "solargroup.com",
]

RICH = "<html><head><title>x</title></head><body>" + "<p>defence systems and platforms</p>" * 40 + "</body></html>"


def _live(dom):
    return {"status": 200, "html": RICH, "error": None, "final_url": "https://%s/" % dom}


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def SL(monkeypatch, tmp_path):
    eng = create_engine("sqlite:///%s" % (tmp_path / "t.db"),
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    SL = sessionmaker(bind=eng, expire_on_commit=False)
    import mallory_orchestrator.db as _dbmod
    monkeypatch.setattr(_dbmod, "SessionLocal", SL)
    monkeypatch.setattr(scheduler, "SessionLocal", SL)
    monkeypatch.setattr(daily, "SessionLocal", SL)
    monkeypatch.setattr(scheduler, "MAX_BUSY_INSTANCES", 0)
    orchestrate._MANUAL.update(running=False, total=0, done=0, halted=None, error=None, sites=[],
                               lane="manual")
    campaigns._set_active(id=None, phase=None, phases=0, started_at=None, detail=None,
                          instances={}, stop=None)
    return SL


@pytest.fixture()
def db(SL):
    with SL() as s:
        policy.start(s)
        yield s


class _Fleet:
    """N instances. `running` may be a bool or a callable(base) so a test can hold an instance
    busy until something happens. Records every batch, stop and resume."""

    def __init__(self, n=N):
        self.bases = ["http://c%d:8099" % i for i in range(n)]
        self.running = False
        self.jobs = [{"job_id": "daily_news0_example", "pending": 10, "has_config": True},
                     {"job_id": "compbreadth_acme_com", "pending": 900, "has_config": True}]
        self.stops: list[str] = []
        self.batches: list[tuple[str, list[str], bool]] = []
        self.resumed: list[tuple[str, list[str]]] = []

    def _running(self, base):
        return self.running(base) if callable(self.running) else self.running

    def pool(self):
        return list(self.bases)

    def batch_status(self, base=None):
        return {"running": self._running(base), "stop_pending": False}

    def dispatch_batch(self, payload, timeout=None, base=None):
        self.batches.append((base, [j["job_id"] for j in payload["jobs"]], bool(payload.get("background"))))
        return {"started": True, "jobs": len(payload["jobs"])}

    def resumable(self, base=None, limit=1000):
        return {"jobs": [dict(j) for j in self.jobs]}

    def resume(self, ids, background=True, forward_to_ingest=True, base=None):
        self.resumed.append((base, list(ids)))
        return {"resumed": list(ids), "started": True}

    def stop_crawl(self, base=None):
        self.stops.append(base)
        return {"stopping": True}

    def get_metrics(self, base=None):
        return {"lanes": {}, "batch": {"running": self._running(base), "total": 0}}

    def fanout(self, fn, *a, **kw):
        return [{"instance": i, "base": b, "ok": True, "data": fn(*a, **kw, base=b)}
                for i, b in enumerate(self.bases)]


@pytest.fixture()
def fleet(monkeypatch):
    f = _Fleet()
    for mod in (api, refill, daily, orchestrate, scheduler, campaigns):
        monkeypatch.setattr(mod, "crawler_client", f)
    monkeypatch.setenv("CRAWLER_DAILY_INSTANCES", "0,1")
    monkeypatch.setenv("CRAWLER_DAILY_EXCLUSIVE", "0")
    monkeypatch.setattr(campaigns, "http_probe", _live)
    monkeypatch.setattr(campaigns, "corpus_pages", lambda: {"acme.com": 120, "news0.example": 30})
    return f


class _SyncThread:
    """Runs the campaign inline, so a test sees the finished state on return."""

    def __init__(self, target=None, args=(), daemon=None, name=None):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


@pytest.fixture()
def client(db, fleet, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[api.get_db] = lambda: db
    monkeypatch.setattr(campaigns, "Thread", _SyncThread)
    return TestClient(app)


def _events(db):
    return [r.event for r in db.query(ScheduleLog).all()]


# ── intake ────────────────────────────────────────────────────────────────────

def test_parse_sites_takes_lines_urls_and_comments_and_dedupes():
    got = campaigns.parse_sites("baesystems.com\nhttps://www.rheinmetall.com/news/ # newsroom\n"
                                "BAESystems.com, saab.com;  # dupe\n\nnot a domain\n")
    assert [g["domain"] for g in got] == ["baesystems.com", "rheinmetall.com", "saab.com"]
    assert got[1]["seed"] == "https://www.rheinmetall.com/news/", "a pasted URL is kept as the seed"
    assert got[0]["seed"] == "https://baesystems.com/"


def test_options_default_to_the_owners_window_and_refuse_nonsense():
    o = campaigns.normalise_options(None, today=dt.date(2026, 9, 20))
    assert o == {"window_from": "2026-08-01", "window_to": "2026-09-20", "scope": "whole_site", "pdfs": True}
    with pytest.raises(ValueError):
        campaigns.normalise_options({"window_from": "Aug 1"})
    with pytest.raises(ValueError):
        campaigns.normalise_options({"window_from": "2026-09-30", "window_to": "2026-09-01"})
    with pytest.raises(ValueError):
        campaigns.normalise_options({"scope": "everything"})


# ── tiering ───────────────────────────────────────────────────────────────────

def test_known_verdicts_win_and_are_never_reprobed():
    calls = []
    probe = lambda dom: calls.append(dom) or _live(dom)  # noqa: E731
    assert campaigns.detect_tier("baesystems.com", probe)[0:2] == ("C4", "audit")
    assert campaigns.detect_tier("supacat.com", probe)[0] == "C2"
    assert campaigns.detect_tier("northropgrumman.com", probe)[0] == "FLAG"
    assert calls == [], "a classified site was probed"
    assert campaigns.detect_tier("saab.com", probe) == ("C1", "probe", campaigns.classify_probe(_live("saab.com"))[1])
    assert calls == ["saab.com"]


def test_the_crawlers_own_tier_lists_are_honoured(monkeypatch):
    monkeypatch.setenv("CRAWLER_C4_HOSTS", "walled.example, other.example")
    monkeypatch.setenv("CRAWLER_C3_HOSTS", "shell.example")
    assert campaigns.detect_tier("www.walled.example", lambda d: _live(d))[0:2] == ("C4", "env")
    assert campaigns.detect_tier("shell.example", lambda d: _live(d))[0:2] == ("C3", "env")


@pytest.mark.parametrize("res, tier", [
    ({"status": 403, "html": "<html><script src='/_Incapsula_Resource?x'></script></html>", "error": None}, "C4"),
    ({"status": 503, "html": "<title>Just a moment...</title><script>cf-chl-x</script>", "error": None}, "C4"),
    ({"status": 403, "html": "<html><head><title>Access Denied</title></head><body>You don't have permission. Reference #18.x errors.edgesuite.net</body></html>", "error": None}, "FLAG"),
    ({"status": 403, "html": "<html><body>Forbidden</body></html>", "error": None}, "C3"),
    ({"status": 200, "html": "<html><body><div id=app></div><script src=a.js></script><script src=b.js></script><script src=c.js></script></body></html>", "error": None}, "C3"),
    ({"status": None, "html": "", "error": "ConnectError: [Errno 11001] getaddrinfo failed"}, "C2"),
    ({"status": 404, "html": "<html><body>not here</body></html>", "error": None}, "C2"),
    ({"status": 200, "html": RICH, "error": None}, "C1"),
])
def test_a_probe_is_classified_the_way_the_ladder_would_climb(res, tier):
    assert campaigns.classify_probe(res)[0] == tier, campaigns.classify_probe(res)


# ── the golden: the hand-built campaign, reproduced ───────────────────────────

def test_the_reference_campaign_reproduces_the_hand_built_jobs(db):
    """comp_campaign.py emitted 84 fleet jobs (phase 1 + phase 2 for every C1/C3 site). The same
    49 domains and the same options must yield exactly those jobs. C4 sites, the C2 site and the
    FLAG site were handled OUTSIDE the fleet by hand; here they are in the plan too (host solver,
    archive, excluded), and are filtered out of the comparison by tier."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    plan = campaigns.build_plan(REFERENCE_DOMAINS, {"window_from": "2026-08-01", "window_to": "2026-09-19"},
                                probe=_live, corpus={}, instances=12)
    fleet_tier = {r["domain"]: r["tier"] for r in plan["sites"]}
    mine = [j for ph in plan["phases"] for j in ph["jobs"] if fleet_tier[j["source_id"]] in ("C1", "C3")]
    key = lambda j: j["job_id"]  # noqa: E731
    assert sorted(mine, key=key) == sorted(golden, key=key)
    assert len(golden) == 84
    # And the rest of the hand run, now in the plan rather than in a shell script:
    assert plan["totals"]["by_tier"] == {"C1": 40, "C3": 2, "C4": 5, "C2": 1, "FLAG": 1}
    assert [x["domain"] for x in plan["excluded"]] == ["northropgrumman.com"]
    arch = [j for ph in plan["phases"] for j in ph["jobs"] if j["job_id"].startswith("comparchive_")]
    assert len(arch) == 1 and arch[0]["source_id"] == "supacat.com"
    assert arch[0]["archive_date"] == "2026-08-01" and arch[0]["archive_to"] == "2026-09-19"
    # SigSauer stays bounded, as the operator decided; every other breadth job is unlimited.
    caps = {j["source_id"]: j["max_pages"] for ph in plan["phases"] for j in ph["jobs"]
            if j["job_id"].startswith("compbreadth_")}
    assert caps["sigsauer.com"] == 5000 and all(v == 0 for d, v in caps.items() if d != "sigsauer.com")
    # A walled newsroom is NOT rendered by CamoFox (it stalls on the Imperva wait-wall).
    news = {j["source_id"]: j for ph in plan["phases"] for j in ph["jobs"] if j["job_id"].startswith("compnews_")}
    assert news["baesystems.com"]["render_js"] is False and news["saab.com"]["render_js"] is True
    assert plan["totals"]["jobs"] == 84 + 5 * 2 + 1


def test_scope_and_pdfs_shape_the_jobs():
    plan = campaigns.build_plan(["saab.com"], {"scope": "news_only", "pdfs": False},
                                probe=_live, corpus={}, instances=2)
    jobs = [j for ph in plan["phases"] for j in ph["jobs"]]
    assert [j["job_id"] for j in jobs] == ["compnews_saab_com"], "news_only must drop the breadth phase"
    assert jobs[0]["capture"] == ["html", "text"] and "max_pdfs_per_page" not in jobs[0]
    assert [ph["phase"] for ph in plan["phases"]] == [2]


def test_estimates_say_where_they_came_from_and_never_invent():
    measured = {"saab.com": {"fetched": 4000, "duration_s": 7200.0, "at": None}}
    plan = campaigns.build_plan(["saab.com", "hii.com", "nammo.com"], None, probe=_live,
                                corpus={"hii.com": 800}, measured=measured, instances=12)
    by = {r["domain"]: r for r in plan["sites"]}
    assert by["saab.com"]["est_pages"] == 4000 and "last crawl" in by["saab.com"]["est_basis"]
    assert by["hii.com"]["est_pages"] == 800 and "corpus" in by["hii.com"]["est_basis"]
    assert by["nammo.com"]["est_pages"] is None and by["nammo.com"]["est_basis"] == "not measured"
    assert plan["totals"]["est_pages"] == 4800 and plan["totals"]["sites_unmeasured"] == 1
    assert plan["eta"]["hours"] == round(4800 / (0.3 * 3) / 3600, 1) and "ASSUMED" in plan["eta"]["basis"]
    plan = campaigns.build_plan(["nammo.com"], None, probe=_live, corpus={}, measured={}, instances=1)
    assert plan["eta"]["hours"] is None and "no ETA" in plan["eta"]["basis"]
    # A store that did not answer is said out loud, never presented as "zero pages held".
    assert campaigns._estimate("nammo.com", None, {}) == (None, "not measured (store unreachable)")


def test_an_empty_list_is_refused():
    with pytest.raises(ValueError):
        campaigns.build_plan("# nothing here\n", None, probe=_live, corpus={}, instances=1)


# ── the run model: plan -> approve -> run ─────────────────────────────────────

def test_a_plan_is_saved_and_dispatches_nothing(client, db, fleet):
    r = client.post("/api/campaigns/plan", json={"name": "comp", "sites": "acme.com\nbaesystems.com",
                                                 "options": {"window_from": "2026-08-01"}})
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["status"] == "draft" and c["totals"]["sites"] == 2 and c["totals"]["jobs"] == 4
    assert fleet.batches == [] and fleet.stops == []
    assert policy.mode(db) == "normal" and policy.running(db)
    assert db.get(Campaign, c["id"]).plan["sites"][1]["tier"] == "C4"
    lst = client.get("/api/campaigns").json()
    assert [x["id"] for x in lst["campaigns"]] == [c["id"]] and lst["defaults"]["window_from"] == "2026-08-01"


def test_a_saved_plan_can_be_tweaked_and_rebuilt(client, db, fleet):
    c = client.post("/api/campaigns/plan", json={"sites": "acme.com"}).json()
    r = client.put("/api/campaigns/%d" % c["id"], json={"name": "renamed", "options": {"scope": "news_only"}})
    assert r.status_code == 200 and r.json()["name"] == "renamed"
    assert r.json()["totals"]["jobs"] == 1 and r.json()["options"]["scope"] == "news_only"
    assert client.post("/api/campaigns/plan", json={"sites": "acme.com", "options": {"scope": "all"}}).status_code == 400
    assert client.delete("/api/campaigns/%d" % c["id"]).json() == {"deleted": c["id"]}
    assert client.get("/api/campaigns/%d" % c["id"]).status_code == 404


def test_go_switches_to_campaign_mode_and_dispatches_the_plan_to_the_owners(client, db, fleet):
    """Go = approve. Mode -> campaign (normal work stopped, frontiers kept), Start, phase 1 to the
    instances that own the domains as background batches, then phase 2; idle -> finished -> back
    to normal mode with the daily domains pinned."""
    db.add(DailyUrl(url="https://news0.example/", domain="news0.example", enabled=True, next_run=NOW, created_at=NOW))
    db.add(DomainShard(domain="news0.example", instance=3))      # drifted off the lane during the campaign
    db.commit()
    c = client.post("/api/campaigns/plan", json={"name": "comp", "sites": "acme.com\nsaab.com"}).json()
    r = client.post("/api/campaigns/%d/go" % c["id"])
    assert r.status_code == 200, r.text
    assert r.json()["mode_switched"] is True and len(fleet.stops) == N, "normal work in flight was not stopped"
    # Two phases, each sent as ONE background batch per owning instance, in phase order.
    phases = [sorted(ids) for _, ids, bg in fleet.batches]
    assert all(bg for _, _, bg in fleet.batches), "campaign batches must be background: the run is polled"
    sent = [j for ids in phases for j in ids]
    assert sorted(j for j in sent if j.startswith("compbreadth_")) == ["compbreadth_acme_com", "compbreadth_saab_com"]
    assert sorted(j for j in sent if j.startswith("compnews_")) == ["compnews_acme_com", "compnews_saab_com"]
    first_news = min(i for i, (_, ids, _) in enumerate(fleet.batches) if any(j.startswith("compnews_") for j in ids))
    last_breadth = max(i for i, (_, ids, _) in enumerate(fleet.batches) if any(j.startswith("compbreadth_") for j in ids))
    assert last_breadth < first_news, "phase 2 went out before phase 1 finished"
    # Routed by shard: a domain's two jobs land on the SAME instance.
    owner = {}
    for base, ids, _ in fleet.batches:
        for j in ids:
            owner.setdefault(j.split("_", 1)[1], set()).add(base)
    assert all(len(v) == 1 for v in owner.values()), owner
    # The fake fleet is idle, so the campaign finished inline: clean normal state, pipeline running.
    # (The run closed the row in ITS session; this shared session must drop its identity map.)
    db.expire_all()
    got = client.get("/api/campaigns/%d" % c["id"]).json()
    assert got["status"] == "finished" and got["runs"] == 1, got["last_detail"]
    assert policy.mode(db) == "normal" and policy.running(db)
    db.expire_all()
    assert db.get(DomainShard, "news0.example").instance in (0, 1), "the daily domain was not re-pinned"
    assert "campaign_start" in _events(db) and "campaign_finished" in _events(db)
    assert campaigns.active() is None
    # Progress reads REAL counts: pages at launch vs now, per site.
    pr = got["progress"]
    assert {s["domain"]: s["state"] for s in pr["sites"]} == {"acme.com": "done", "saab.com": "done"}
    assert {s["domain"]: s["pages_start"] for s in pr["sites"]} == {"acme.com": 120, "saab.com": None}
    # Re-run from the saved plan.
    assert client.post("/api/campaigns/%d/go" % c["id"]).status_code == 200
    db.expire_all()
    assert client.get("/api/campaigns/%d" % c["id"]).json()["runs"] == 2


def test_one_campaign_at_a_time_and_not_under_maintenance(client, db, fleet, monkeypatch):
    c = client.post("/api/campaigns/plan", json={"sites": "acme.com"}).json()
    campaigns._set_active(id=999, phase=1)
    assert client.post("/api/campaigns/%d/go" % c["id"]).status_code == 409
    campaigns._set_active(id=None)
    scheduler.set_maintenance(db, True)
    assert client.post("/api/campaigns/%d/go" % c["id"]).status_code == 409
    scheduler.set_maintenance(db, False)
    # A fleet nobody can reach is refused at Go, not left "running" waiting for an owner.
    monkeypatch.setattr(fleet, "fanout", lambda fn, *a, **k: [{"instance": 0, "ok": False, "error": "down"}])
    r = client.post("/api/campaigns/%d/go" % c["id"])
    assert r.status_code == 409 and "no crawler instance answered" in r.json()["detail"]
    assert fleet.batches == [] and policy.mode(db) == "normal"
    assert db.get(Campaign, c["id"]).status == "draft"


def test_stop_hard_stops_a_running_campaign_and_stays_stopped(client, db, fleet, SL):
    """The instances stay busy for as long as the campaign is allowed to run; Stop closes the
    gate, the thread exits without dispatching phase 2, the pipeline is STOPPED, the mode is back
    to normal, and nothing revives the campaign's frontiers afterwards."""
    def busy_while_running(base):
        with SL() as s:                                   # its own session: this runs on the poll thread
            return policy.running(s)

    fleet.running = busy_while_running
    c = client.post("/api/campaigns/plan", json={"sites": "acme.com\nsaab.com"}).json()
    camp = db.get(Campaign, c["id"])
    t = threading.Thread(target=campaigns.launch, args=(db, camp),
                         kwargs={"stop_fleet": api._stop_fleet, "poll_s": 0.02, "thread": False}, daemon=True)
    t.start()
    for _ in range(200):                                  # until phase 1 is out
        if fleet.batches and campaigns.active() and campaigns.active()["phase"] == 1:
            break
        threading.Event().wait(0.02)
    assert campaigns.active()["id"] == c["id"] and policy.mode(db) == "campaign"
    r = client.post("/api/control/stop")
    assert r.status_code == 200 and r.json()["campaign"]["status"] == "stopped" and r.json()["mode"] == "normal"
    t.join(timeout=5)
    assert not t.is_alive(), "the campaign thread did not see the closed gate"
    assert all(not j.startswith("compnews_") for _, ids, _ in fleet.batches for j in ids), "phase 2 went out after Stop"
    db.expire_all()
    assert db.get(Campaign, c["id"]).status == "stopped"
    assert policy.stopped(db) and policy.mode(db) == "normal" and campaigns.active() is None
    assert "campaign_stopped" in _events(db)
    # STAYS stopped: with every toggle on, nothing resumes the campaign's frontier ...
    for name in policy.AUTOS:
        policy.set_auto(db, name, True)
    fleet.running = False
    assert refill.tick() is None and fleet.resumed == []
    # ... and after Start, in normal mode, the campaign's frontier is PARKED, the daily one resumes.
    policy.start(db)
    out = refill.tick()
    assert out["parked"] == N and all(ids == ["daily_news0_example"] for _, ids in fleet.resumed), fleet.resumed


def test_a_ghost_campaign_is_closed_at_boot(db, fleet):
    camp = Campaign(name="ghost", status="running", options={}, sites=[], plan={"phases": []},
                    created_at=NOW, updated_at=NOW, runs=1)
    db.add(camp)
    db.commit()
    policy.set_mode(db, "campaign")
    assert campaigns.recover(db) == [camp.id]
    db.expire_all()
    assert camp.status == "stopped" and "restarted" in camp.last_detail and policy.mode(db) == "normal"


def test_switching_the_mode_back_to_normal_ends_the_campaign(client, db, fleet):
    c = client.post("/api/campaigns/plan", json={"sites": "acme.com"}).json()
    campaigns._set_active(id=c["id"], phase=1, instances={})
    db.get(Campaign, c["id"]).status = "running"
    db.commit()
    policy.set_mode(db, "campaign")
    r = client.post("/api/control/mode", json={"mode": "normal"})
    assert r.json()["campaign"]["status"] == "stopped" and campaigns.active() is None
    assert client.get("/api/control/state").json()["campaign"] is None

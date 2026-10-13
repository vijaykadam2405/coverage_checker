"""Live invariants — the faults that unit tests cannot see, checked against the running system.

WHY THIS EXISTS. Every fault in this project's history passed its unit tests. They were not logic
bugs in a function; they were a working system quietly doing nothing, and the only way to catch that
is to assert against production state. Read back over the record, the same shapes keep recurring:

  1  SILENT SUCCESS      an operation reports ok and changes nothing.
                         `dispatched 1,457 missing URL(s)` that dispatched none; a resume the
                         crawler refused without raising; `cycle ok · fetched=0`; a seed recheck
                         whose target set was always empty; and — found live and still open —
                         POST /ingest/v1/page returning 200 while the row never reaches Postgres.

  2  UNREACHABLE POWER   a capability that is shipped, correct, and never invoked. curl_cffi
                         real-Chrome impersonation sat behind `status in (401, 403)` for months;
                         widening it changed nothing because a TLS refusal has no status at all.

  3  CAP BEFORE MATCH    a limit applied before the key it should respect. The due list truncated
                         by priority before owners were matched; then the free-instance list
                         trimmed by index before owners were matched. Same bug, both axes.

  4  FROZEN COPY DRIFT   a duplicate of the truth that stops agreeing with it. ScheduledJob.job
                         carries a COPY of the seed; correcting the catalogue alone fixed nothing.

  5  ONE PROBE, A VERDICT  174 live sites called dead; baesystems.com written off as permanently
                         walled on a block that was transient and self-inflicted.

  6  MONOLINGUAL BAR     an English keyword gate silently discarding Russian, Chinese and Japanese
                         manufacturers whose pages are perfectly on-topic.

  7  A COUNT IS NOT WORK 24,244 queued URLs that were one menu permuting itself; "564 complete"
                         where 232 were.

  8  UNBOUNDED WAIT      an instance holding a running flag with no work while the box sits idle.

Each check below targets one of those classes with evidence, not opinion. A check that cannot be
evaluated returns `ok=None` ("not measured") rather than passing — a green tick nobody computed is
how several of these hid in the first place.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

CANARY_HOST = "selfcheck-canary.invalid"
# Below this many tier-labelled pages the distribution says nothing: `served_by` was added after the
# first crawls, so a zero is far more likely to mean "unlabelled" than "never fired".
MIN_LABELLED_FOR_TIER_CHECK = 500


@dataclass
class Check:
    name: str
    klass: str                       # which recurring failure class this guards
    ok: bool | None                  # None = could not be measured; never silently green
    detail: str = ""
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"name": self.name, "class": self.klass, "ok": self.ok,
                "detail": self.detail, "evidence": self.evidence}


# ── 1. silent success on the write path ───────────────────────────────────────────────────────

def check_write_path(_db=None) -> Check:
    """Does an accepted document actually reach the store?

    THE fault this exists for, caught live: POST /ingest/v1/page answered
    `200 {"status":"accepted"}` and the row never appeared in Postgres, while the fleet's own
    writes landed normally. Every counter and every dashboard tile looked healthy. A crawl can run
    for hours into a store that is not recording it, and nothing upstream can tell.

    Writes a canary and reads it back through the same store the dashboard reads.

    THE CANARY URL IS STABLE, and that is not a detail. This first used a fresh URL per tick, and
    the docstring claimed it "removes it" — which it never did. On the autopilot's 15-minute timer
    that left one new corpus row every tick: 39 pages and 26,520 chars of nothing, filed under a
    host nobody catalogued, showing up on the operator's own dashboard in the uncatalogued-hosts
    line beside two real sites. A health check must not be a writer to the thing it measures.

    One fixed URL means `documents_current` holds exactly ONE canary row forever, however long the
    autopilot runs. The content_hash still varies per tick, so the row is genuinely re-written each
    time and a store that silently stopped accepting writes is still caught — which is the whole
    point of the check. And the host is filtered out of the sources listing (see api._live_sources),
    so it stops being a line on a dashboard about defence manufacturers.
    """
    from . import api
    # Random, not time-derived. time.time() has ~15ms resolution on Windows, so two checks in quick
    # succession produced an IDENTICAL content_hash — and an identical hash is exactly what a store
    # that stopped accepting writes also produces, so the check could have passed on a stale row.
    # Ticks are 15 minutes apart in production, but a health check must not rest on that.
    doc_id = "selfcheck_" + hashlib.sha1(secrets.token_bytes(16)).hexdigest()[:20]
    text = "selfcheck canary " * 40
    doc = {"url": "https://%s/canary" % CANARY_HOST, "content_hash": doc_id,
           "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "source_id": CANARY_HOST, "title": "selfcheck", "language": "en",
           "main_text": text, "html": "", "attachments": [], "depth": 0, "parent_url": None,
           "served_by": "selfcheck", "document_id": doc_id}
    try:
        posted = api._ingest_post("/ingest/v1/page", {"document": doc})
    except Exception as e:  # noqa: BLE001
        return Check("write path", "silent success", None,
                     "could not reach the ingest API: %s" % str(e)[:90])
    if not posted:
        return Check("write path", "silent success", None, "ingest returned nothing")

    landed = None
    for _ in range(6):                      # the insert is not synchronous with the response
        time.sleep(0.5)
        try:
            rows = api._ingest_get("/api/live/sources?limit=5000&q=" + CANARY_HOST)
            landed = any(str(r.get("source_id", "")).lower() == CANARY_HOST for r in rows or [])
        except Exception:                   # noqa: BLE001
            landed = None
            break
        if landed:
            break
    if landed is None:
        return Check("write path", "silent success", None, "could not read the store back")
    if not landed:
        return Check("write path", "silent success", False,
                     "ingest ACCEPTED a document and it never reached the store — "
                     "crawls are being silently discarded",
                     {"document_id": doc_id, "accepted": True, "stored": False})
    return Check("write path", "silent success", True, "a document written now reads back",
                 {"document_id": doc_id})


# ── 2. a shipped capability that is never invoked ─────────────────────────────────────────────

def check_tiers_are_reached(_db=None, window_days: int = 7) -> Check:
    """Is every fetch tier actually being used?

    curl_cffi impersonation was shipped, correct, and unreachable for months: first gated to
    401/403, then missed entirely because a TLS refusal carries no status. Nothing alerted, because
    a tier that never fires looks exactly like a tier that is never needed.
    """
    from . import api
    try:
        served = (api._ingest_get("/api/live/types") or {}).get("served_by") or []
    except Exception as e:  # noqa: BLE001
        return Check("tiers reached", "unreachable power", None, str(e)[:90])
    # `pages` FIRST, because that is what /api/live/types actually returns. Measured live: the
    # corpus held c1_live 378,857 · c3_camofox 4,941 · c4_host_solver 330 and this check reported
    # "only 0 page(s) carry a tier label" for weeks, because it read `n`/`count` and every row
    # therefore evaluated to zero. It then took the not-measured branch and reported itself as
    # honestly-unknown — which is the SILENT SUCCESS class reproduced inside the very module
    # written to catch it. A checker that cannot read its own data source is not a checker.
    def _n(r) -> int:
        for k in ("pages", "n", "count"):
            if r.get(k) is not None:
                return int(r[k])
        return 0

    counts = {str(r.get("served_by") or r.get("tier") or "?"): _n(r)
              for r in served} if isinstance(served, list) else dict(served)
    counts.pop("?", None)
    if not counts:
        return Check("tiers reached", "unreachable power", None, "the store reported no tier counts")

    # served_by is SPARSE: it was added after the first crawls, so most rows carry no label and a
    # tier reading zero usually means "unlabelled", not "never fired". Demanding a specific tier
    # here produced a false alarm on its first live run — which is the same sin this module exists
    # to prevent, in the opposite direction. Report what is labelled, and say plainly when the
    # labels are too thin to conclude anything.
    labelled = sum(counts.values())
    if labelled < MIN_LABELLED_FOR_TIER_CHECK:
        return Check("tiers reached", "unreachable power", None,
                     "only %d page(s) carry a tier label — too few to tell an unused tier from an "
                     "unlabelled one" % labelled, counts)
    live = [t for t, n in counts.items() if n > 0]
    return Check("tiers reached", "unreachable power", len(live) > 1,
                 "%d tier(s) have served pages: %s" % (len(live), ", ".join(sorted(live)))
                 if len(live) > 1 else
                 "only ONE tier has ever served a page (%s) — the escalation ladder is not being "
                 "used" % (live[0] if live else "none"),
                 counts)


def check_c4_is_uniform_across_the_fleet(_db=None) -> Check:
    """Does EVERY instance have C4, and does C4 answer?

    Two faults in one check, because they are the same fault seen from two angles.

    DEAD C4 IS INVISIBLE. C4 is the only rung that clears a Cloudflare-interactive wall, and it is
    the only one that cannot live on the crawl box — measured 2026-08-02, Chrome in a container on
    that hypervisor reports `ANGLE (Mesa, llvmpipe)` even with /dev/dri passed through and vmwgfx
    forced, because the VM exposes no 3D at all. So it is a network hop to a workstation, and
    `hostsolver.solve()` swallows a failed hop on purpose ("record the wall and move on"). A dead
    C4 therefore looks exactly like a crawl that met no walls: the pages are simply never fetched.
    It died three times in one day before anything said so.

    A PARTLY-CONFIGURED FLEET IS WORSE THAN NONE. The env var is per-container, so a fleet
    recreated while one instance was mid-batch has eleven instances with C4 and one without — and
    whether a walled site gets fetched then depends on which shard happens to own it. This project
    has already shipped that shape: twelve instances built from the same Dockerfile silently ran a
    MIX of old and new code, and the only symptom was a filter that "didn't work". So this asks
    every instance, not one, and disagreement is itself the finding.

    It deliberately does NOT solve a real URL — that drives a browser through a third-party
    challenge for ~30s and this runs every 15 minutes. The crawler's own /v1/tiers C4 probe already
    asserts IDENTITY (host_solver's /health uniquely returns browser_open/solved_hosts), which is
    what stops a different service on the same port reading as success. Liveness plus identity is
    the most that is honest for free, and the detail says so.
    """
    from . import crawler_client

    def _sample():
        rows = crawler_client.fanout(crawler_client.get_tiers)
        on, off, down, unreadable = [], [], [], 0
        for r in rows:
            d = r.get("data") if r.get("ok") else None
            c4 = d.get("c4") if isinstance(d, dict) else None
            if not isinstance(c4, dict):
                # An older image has no c4 key at all. ABSENT IS UNKNOWN, NEVER OFF — reading a
                # missing field as a value is precisely how the wedged-instance check produced its
                # first false positive.
                unreadable += 1
                continue
            (on if c4.get("enabled") else off).append(r.get("instance"))
            if c4.get("enabled") and not c4.get("up"):
                down.append((r.get("instance"), str(c4.get("detail") or "")[:40]))
        return on, off, down, unreadable

    try:
        on, off, down, unreadable = _sample()
        # ONE PROBE IS NOT A VERDICT — failure class 5, and this check committed it on its second
        # live run. Every crawler caches /v1/tiers for 30s, so the first fanout after a restart
        # finds twelve cold caches and fires twelve simultaneous probes down a SINGLE ssh tunnel;
        # C4 timed out on all twelve and the check called the tier dead while a hand-run curl from
        # a container answered fine. Twelve independent services do not fail in the same instant —
        # that shape is a herd, not an outage. So confirm it once, unhurried, before saying so.
        if down and len(down) == len(on):
            time.sleep(5)
            on, off, down, unreadable = _sample()
    except Exception as e:  # noqa: BLE001
        return Check("c4 uniform + reachable", "silent success", None, str(e)[:90])

    if unreadable and not (on or off):
        return Check("c4 uniform + reachable", "silent success", None,
                     "%d instance(s) report no c4 tier — image predates the C4 probe, cannot tell"
                     % unreadable, {"unreadable": unreadable})
    if not on and not down:
        return Check("c4 uniform + reachable", "silent success", None,
                     "C4 is off on all %d readable instance(s) — interactive walls are recorded "
                     "and skipped by design" % len(off), {"off": off})
    if off:
        return Check("c4 uniform + reachable", "silent success", False,
                     "C4 is configured on %d instance(s) but NOT on %s — whether a walled site is "
                     "fetched now depends on which shard owns it"
                     % (len(on), ", ".join(str(i) for i in off)),
                     {"on": on, "off": off, "unreadable": unreadable})
    if down:
        return Check("c4 uniform + reachable", "silent success", False,
                     "C4 is configured fleet-wide but DOWN on %s — every interactive wall is being "
                     "silently skipped" % ", ".join("%s (%s)" % (i, d) for i, d in down),
                     {"on": on, "down": [i for i, _ in down]})
    if unreadable:
        # UNIFORMITY IS THE CLAIM, so an instance we could not read defeats it — it is exactly the
        # one that might be the odd one out. Caught on this check's own first live run: eleven
        # instances answered "C4 up" and the twelfth was mid-batch on an older image with no c4
        # key, and the check said "all 11 instance(s)" and went green on a fleet of twelve. A count
        # that quietly redefines "all" as "all the ones that answered" is the same sin as reading
        # an absent field as zero.
        return Check("c4 uniform + reachable", "silent success", None,
                     "C4 answers on %d instance(s) but %d could not be read (down, or an image "
                     "predating the C4 probe) — cannot confirm the fleet is uniform"
                     % (len(on), unreadable), {"on": on, "unreadable": unreadable})
    return Check("c4 uniform + reachable", "silent success", True,
                 "C4 is configured and answering on all %d instance(s); liveness and identity only "
                 "— no challenge was actually solved" % len(on),
                 {"on": on, "unreadable": 0})


# ── 3. a cap applied before the key it should respect ─────────────────────────────────────────

def check_no_idle_instance_owns_due_work(db) -> Check:
    """An idle instance owning due work means the cap trimmed the wrong end of the list.

    Measured twice, on two different axes: the due list cut by priority before owners were matched,
    then the free-instance list cut by index before owners were matched. Both produced the same
    picture — a full queue and idle containers — and neither raised anything.
    """
    from . import crawler_client, scheduler, shard
    try:
        free = set(scheduler.free_instances())
        n = len(crawler_client.pool())
        due = scheduler.due_jobs(db, scheduler.utcnow())
    except Exception as e:  # noqa: BLE001
        return Check("dispatch routing", "cap before match", None, str(e)[:90])
    if not free or not due:
        return Check("dispatch routing", "cap before match", True,
                     "nothing free, or nothing due — no routing to check")
    stranded = 0
    for sj in due:
        seeds = (sj.job or {}).get("seed_urls") or []
        try:
            if shard.instance_for(db, seeds[0] if seeds else "", n) in free:
                stranded += 1
        except Exception:  # noqa: BLE001
            continue
    return Check("dispatch routing", "cap before match", stranded == 0,
                 ("free instances own no due work" if stranded == 0 else
                  "%d due job(s) are owned by an IDLE instance and are not being dispatched"
                  % stranded),
                 {"free_instances": sorted(free), "due": len(due), "stranded": stranded})


# ── 4. a frozen copy that stopped agreeing with its source ────────────────────────────────────

def check_schedule_seeds_match_catalogue(db) -> Check:
    """ScheduledJob.job holds a COPY of the seed, and the copy is what the crawl uses.

    Correcting Source.seed_urls alone fixed the never-crawled dispatch path and left the scheduled
    path pointed at the dead URL for ever. Nothing compares the two.
    """
    from sqlalchemy import select
    from .models import ScheduledJob, Source
    try:
        srcs = {s.domain.lower().removeprefix("www."): s
                for s in db.scalars(select(Source)).all() if s.domain}
        drift = []
        for j in db.scalars(select(ScheduledJob)).all():
            if not j.enabled:
                continue
            key = j.name.lower().removeprefix("www.")
            s = srcs.get(key)
            if s is None or not s.seed_urls:
                continue
            job_seed = ((j.job or {}).get("seed_urls") or [""])[0]
            if job_seed.rstrip("/") != s.seed_urls[0].rstrip("/"):
                drift.append({"domain": key, "catalogue": s.seed_urls[0], "schedule": job_seed})
    except Exception as e:  # noqa: BLE001
        return Check("seed copies agree", "frozen copy drift", None, str(e)[:90])
    return Check("seed copies agree", "frozen copy drift", not drift,
                 ("every schedule uses its catalogue seed" if not drift else
                  "%d schedule(s) crawl a different URL than the catalogue says" % len(drift)),
                 {"drift": drift[:10], "total": len(drift)})


# ── 8. an instance holding a running flag with no work ────────────────────────────────────────

def check_no_wedged_instance(_db=None) -> Check:
    """running=True + pending=0 + an idle box = a teardown that never finished.

    It cost an hour of a twelve-instance fleet, and it reads exactly like normal cap behaviour on
    every dashboard: "2 crawling, 10 idle". stop_crawl does not clear it; the container must be
    restarted.
    """
    from . import crawler_client
    try:
        rows = crawler_client.fanout(crawler_client.batch_status)
    except Exception as e:  # noqa: BLE001
        return Check("no wedged instance", "unbounded wait", None, str(e)[:90])
    # /v1/crawl/batch/status reports `hosts` and `current_job` — NOT `pending`/`current_domains`,
    # which belong to the fleet-health rollup. The first version of this check read those two names,
    # found them absent, and read absent as empty: it flagged three instances that were busily
    # crawling amentum.com, tycos.com and new.diehl.com. An ABSENT field is unknown, never zero —
    # the same not-measured discipline the rest of this module is built on.
    suspect, unreadable = [], 0
    for r in rows:
        d = r.get("data") if r.get("ok") else None
        if not isinstance(d, dict):
            continue
        if not d.get("running"):
            continue
        if "hosts" not in d and "current_job" not in d:
            unreadable += 1
            continue
        if not (d.get("hosts") or []) and not d.get("current_job"):
            suspect.append(r.get("instance"))
    if unreadable and not suspect:
        return Check("no wedged instance", "unbounded wait", None,
                     "%d running instance(s) report neither hosts nor a current job — cannot tell "
                     "wedged from busy" % unreadable, {"unreadable": unreadable})
    return Check("no wedged instance", "unbounded wait", not suspect,
                 ("no instance is running with an empty queue" if not suspect else
                  "instance(s) %s report running with NO host and no current job — likely wedged "
                  "in teardown; stop_crawl will not clear it, the container needs a restart"
                  % ", ".join(str(i) for i in suspect)),
                 {"suspect": suspect})


CHECKS = (check_write_path, check_tiers_are_reached, check_c4_is_uniform_across_the_fleet,
          check_no_idle_instance_owns_due_work, check_schedule_seeds_match_catalogue,
          check_no_wedged_instance)


def run_all(db) -> dict:
    """Every invariant. Never raises — a self-check that dies is worse than none."""
    out = []
    for fn in CHECKS:
        try:
            out.append(fn(db).as_dict())
        except Exception as e:  # noqa: BLE001
            log.exception("selfcheck %s blew up", fn.__name__)
            out.append(Check(fn.__name__, "?", None, "check itself failed: %s" % str(e)[:90]).as_dict())
    failed = [c for c in out if c["ok"] is False]
    unknown = [c for c in out if c["ok"] is None]
    return {"checks": out, "failed": len(failed), "not_measured": len(unknown),
            "ok": not failed,
            "summary": ("all %d invariants hold" % len(out) if not failed else
                        "FAILING: " + "; ".join(c["name"] for c in failed))
                       + ("" if not unknown else
                          " · %d not measured (%s)" % (len(unknown),
                                                       ", ".join(c["name"] for c in unknown)))}

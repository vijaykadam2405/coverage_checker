"""Digital twin of the crawl backend — a virtual copy you can watch and experiment on.

Two halves, and the split between them is the whole point of the module.

**The mirror** (`build_snapshot`) turns what the live stack reports into one graph: services,
the 12-instance fleet, the four-tier fetch ladder, the page pipeline with every drop edge, and
the two back-pressure mechanisms. It INVENTS NOTHING. Every field is either something a service
returned or absent — a missing counter stays missing so the panel can print "—" and the reason,
because a twin that fills gaps with zeros is a twin of a system that does not exist.

**The sandbox** (`simulate`) answers "what would happen if" without touching anything. It is a
pure function of its arguments: no DB session, no crawler client, no HTTP, no import of either.
That is enforced by a test, not by good intentions — the entire value of a sandbox is the
promise that running it cannot hurt production, and a promise nobody checks is a bug waiting to
be written by whoever edits this next.

WHAT THE MODEL KNOWS. Four ceilings, each a real constraint the operator has actually hit:

  httpx lane   instances x per-instance workers, over the httpx round trip
  render lane  the C3 semaphore, over the render round trip
  CPU          ~1.2 cores per concurrent render on a GPU-less box (measured, see
               [[throughput-equals-host-count]]) — the reason the render lane is scarce
  host         politeness: unique_hosts x per-host concurrency, over the mean round trip

Throughput is the smallest of the four and the bottleneck is which one that was. This is a
capacity model, not a simulator of individual pages: it says which wall you hit and roughly
where, and it is honest in `notes` about which of its inputs were measured and which assumed.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

# ── Measured constants ───────────────────────────────────────────────────────────────────────
# Not tuning knobs — each was measured on this hardware and is quoted with where it came from.

CORES_PER_RENDER = 1.2      # a concurrent CamoFox render on a GPU-less box
LANE_PER_INSTANCE = 24      # render_lane_cap 240 observed across 10 live instances
HTTPX_CORE_COST = 0.02      # a plain HTTP fetch is nearly free next to a render

DEFLECT_COST_S = 0.25       # a bounce is cheap, but it is NOT free — see _capacity

# Starting point for the sandbox. Chosen to sit near where the LIVE stack actually operates
# rather than at round numbers: a mostly-httpx daily crawl, per-host concurrency at the adaptive
# floor of 3, and a frontier piled onto a handful of hosts. At these inputs the model returns
# 3.57 pages/s and 7.6 deflections per fetch against a live measurement of 3.63 and 9.02 — close
# enough to trust the SHAPE of its answers, which is what the sandbox is for. `live_params`
# overrides everything it can actually read off the running system.
DEFAULTS = {
    "instances": 10, "render_share": 0.15, "host_concurrency": 3, "cpu_cores": 40,
    "cpu_target": 85, "unique_hosts": 10, "c3_up": True, "c4_up": True,
    "render_seconds": 9.0, "httpx_seconds": 1.8, "frontier": 0,
    "deflect_cost_s": DEFLECT_COST_S,
}


def effective_hosts(by_host: dict | None) -> float | None:
    """How many hosts the crawler is REALLY contending over, from the issue distribution.

    Counting distinct domains overstates it badly. Live, 25 hosts carried 82,210 host_busy issues
    — but one of them held 24% of them, and the inverse-Herfindahl effective count of that
    distribution is 10.2, not 25. Politeness is a per-host limit, so a frontier piled onto a few
    domains behaves like a handful of hosts no matter how many are nominally in the batch. This is
    the standard effective-number measure, (sum x)^2 / sum x^2, and it is why "crawl more domains"
    beats "add more instances" on this workload.
    """
    if not isinstance(by_host, dict):
        return None
    vals = [v for v in (_num(x) for x in by_host.values()) if v and v > 0]
    if not vals:
        return None
    s, s2 = sum(vals), sum(v * v for v in vals)
    return round(s * s / s2, 2) if s2 else None


def _num(v: Any) -> float | None:
    """A number, or None. Deliberately NOT `float(v or 0)` — that maps a missing counter and a
    real zero onto the same value, which is exactly the lie this dashboard forbids."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _get(d: Any, *path: str) -> Any:
    cur = d
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


# ── The mirror ───────────────────────────────────────────────────────────────────────────────

def build_snapshot(metrics: dict | None, tiers: dict | None, camofox: dict | None,
                   batch: dict | None, *, now: dt.datetime | None = None) -> dict:
    """Assemble the twin from what the live services returned.

    Pure: every input is already-fetched JSON, so the whole mirror is testable against a recorded
    payload with no network. Callers do the fetching and are free to hand us `None` for a service
    that was unreachable — that reads through as an absent section, never as an idle one.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    m = metrics if isinstance(metrics, dict) else {}
    reachable = bool(m) and m.get("reachable") is not False

    prog = m.get("progress") if isinstance(m.get("progress"), dict) else {}
    tot = prog.get("totals") if isinstance(prog.get("totals"), dict) else {}
    lanes = m.get("lanes") if isinstance(m.get("lanes"), dict) else {}
    adaptive = m.get("adaptive") if isinstance(m.get("adaptive"), dict) else {}
    inst = m.get("instances") if isinstance(m.get("instances"), dict) else {}
    issues = m.get("issues") if isinstance(m.get("issues"), dict) else {}
    pool = m.get("pool") if isinstance(m.get("pool"), dict) else {}

    fetched = _num(tot.get("fetched"))
    kept = _num(tot.get("kept"))
    sent = _num(tot.get("sent"))
    accepted = _num(tot.get("accepted"))
    unchanged = _num(tot.get("skipped_unchanged"))
    dupe = _num(tot.get("skipped_duplicate"))
    gated = _num(tot.get("dropped_by_gate"))
    defl_host = _num(tot.get("deflected_host"))
    defl_render = _num(tot.get("deflected_render"))

    per_fetch = (defl_host / fetched) if (defl_host is not None and fetched) else None

    snap: dict[str, Any] = {
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reachable": reachable,
        "fleet": _fleet(inst, batch),
        "ladder": _ladder(tiers, camofox),
        "pipeline": _pipeline(fetched, kept, sent, accepted, unchanged, dupe, gated),
        "lanes": {
            "render_lane_cap": _num(lanes.get("render_lane_cap")),
            "render_lane_used": _num(lanes.get("render_lane_used")),
            "render_q": _num(lanes.get("render_q")),
            "httpx_reserve": _num(lanes.get("httpx_reserve")),
            "frontier": _num(lanes.get("frontier")),
            "adaptive": {
                "enabled": adaptive.get("enabled"),
                "cpu": _num(adaptive.get("cpu")), "ram": _num(adaptive.get("ram")),
                "target": _num(adaptive.get("target")),
                "render_limit": _num(adaptive.get("render_limit")),
                "active": _num(adaptive.get("active")),
            } if adaptive else None,
            "pool": {
                "browsers": _num(pool.get("browsers")),
                "tabs": _num(pool.get("tabs")),
                "tabs_per_browser": _num(pool.get("tabs_per_browser")),
                "host_concurrency": pool.get("host_concurrency"),
                "engine": pool.get("engine"),
            } if pool else None,
        },
        "hardware": {
            "cpu_percent": _num(m.get("cpu_percent")), "cpu_count": _num(m.get("cpu_count")),
            "ram": m.get("ram") if isinstance(m.get("ram"), dict) else None,
            "disk": m.get("disk") if isinstance(m.get("disk"), dict) else None,
            "container_ram": (m.get("container_ram")
                              if isinstance(m.get("container_ram"), dict) else None),
        },
        "throughput": {
            "pages": _num(prog.get("pages")), "elapsed_s": _num(prog.get("elapsed_s")),
            "pages_per_sec": _num(prog.get("pages_per_sec")), "running": prog.get("running"),
        },
        "deflection": {
            "host": defl_host, "render": defl_render,
            "deferred_render": _num(tot.get("deferred_render")),
            "host_busy_gaveup": _num(tot.get("host_busy_gaveup")),
            "render_gaveup": _num(tot.get("render_gaveup")),
            "per_fetch": round(per_fetch, 2) if per_fetch is not None else None,
        },
        "issues": {
            "total": _num(issues.get("total")),
            "dropped_over_cap": _num(issues.get("dropped_over_cap")),
            "by_cause": issues.get("by_cause") if isinstance(issues.get("by_cause"), dict) else None,
            "by_host": issues.get("by_host") if isinstance(issues.get("by_host"), dict) else None,
        } if issues else None,
        "batch": _batch(m.get("batch"), batch),
        "services": _services(m, tiers, camofox),
    }
    snap["verdict"] = _verdict(snap)
    # The sandbox's starting point travels WITH the mirror, so the panel opens as a copy of
    # production instead of a blank form the operator has to guess their way into — and each
    # input says whether it was measured here or assumed, which is the difference between a
    # what-if and a fiction.
    snap["model_inputs"] = live_params(snap)
    return snap


def _fleet(inst: dict, batch: dict | None) -> dict:
    """Fleet health. `instances` is the crawler's own roll-up; the per-instance detail it carries
    varies by version, so anything absent stays absent rather than being back-filled with a
    plausible-looking row."""
    out: dict[str, Any] = {
        "total": _num(inst.get("total")), "up": _num(inst.get("up")),
        "down": _num(inst.get("down")), "crawling": _num(inst.get("crawling")),
        "idle": _num(inst.get("idle")), "cap": _num(inst.get("cap")),
        "down_detail": inst.get("down_detail") if isinstance(inst.get("down_detail"), list) else [],
    }
    rows = inst.get("detail") if isinstance(inst.get("detail"), list) else None
    down_ids = {d.get("instance") for d in out["down_detail"] if isinstance(d, dict)}
    total = int(out["total"]) if out["total"] else 0
    if rows is None and total:
        # No per-instance payload from this crawler build. Emit only what we can PROVE per index:
        # its container name (index N is crawler-api-<N+1>, a fixed mapping) and whether the
        # roll-up failed to reach it. Status is "unknown", never "crawling" — inventing
        # per-instance activity is how a dead instance ends up rendered as a busy one.
        rows = [{"id": i, "container": f"crawler-api-{i + 1}",
                 "status": "not_answering" if i in down_ids else "unknown",
                 "error": next((d.get("error") for d in out["down_detail"]
                                if isinstance(d, dict) and d.get("instance") == i), None)}
                for i in range(total)]
    out["instances"] = rows or []

    # "down" is the roll-up's word and it OVERSTATES the case. Measured 2026-09-10: the roll-up
    # reported 6 of 12 down while `docker ps` showed all twelve containers Up — every one of those
    # errors was "timed out" at load 72, i.e. the fan-out probe gave up on a busy instance. A
    # timed-out probe and a dead container need different responses (wait vs restart), so the twin
    # keeps them separate and never renders the first as the second.
    errs = [str(d.get("error") or "") for d in out["down_detail"] if isinstance(d, dict)]
    out["not_answering"] = out.pop("down", None)
    out["all_timeouts"] = bool(errs) and all("time" in e.lower() for e in errs)
    return out


def _ladder(tiers: dict | None, camofox: dict | None) -> dict:
    """The four fetch tiers. C3's row is enriched from the CamoFox probe, which knows things the
    tier check cannot: whether a browser is actually alive and how many tabs are open. An idle
    browser with zero tabs is the HEALTHY state while C1 serves the crawl — see the note the
    watchfloor already carries — so tabs_open==0 is passed through, not scored."""
    t = tiers if isinstance(tiers, dict) else {}
    out: dict[str, Any] = {}
    for key in ("c1", "c2", "c3", "c4"):
        row = t.get(key)
        out[key] = dict(row) if isinstance(row, dict) else None
    if isinstance(camofox, dict) and isinstance(out.get("c3"), dict):
        out["c3"]["camofox"] = {
            "browser_running": camofox.get("browser_running"),
            "browser_connected": camofox.get("browser_connected"),
            "browsers_live": _num(camofox.get("browsers_live")),
            "tabs_open": _num(camofox.get("tabs_open")),
            "healthy": camofox.get("healthy"),
            "url": camofox.get("url"),
        }
    if isinstance(t.get("checked_at"), str):
        out["checked_at"] = t["checked_at"]
        out["cached"] = t.get("cached")
    return out


def _pipeline(fetched, kept, sent, accepted, unchanged, dupe, gated) -> dict:
    """The page's journey as stages plus flows, with every drop edge named by its real cause.

    A flow whose counter is missing is OMITTED. The alternative — a zero-width edge — draws a
    path that says "nothing was dropped here" when the truth is "nobody counted".

    THE COUNTERS ARE SIBLINGS, NOT A TREE. `dropped_by_gate` is counted where the gate runs,
    which is BEFORE a page is ever "kept" — so gate drops hang off `fetch`, not off `parse`.
    Modelling the gate as downstream of `kept` is what made the numbers refuse to add up
    (1,049 kept against 345 gated + 1,046 sent, a 342-page impossibility). With the drops as
    siblings of `kept` the same live sample reconciles to a residual of 51 out of 4,090.

    That residual is REPORTED, not hidden. It is the pages that were fetched and then neither
    kept nor deliberately dropped — fetch failures — and rounding it away would quietly convert
    an error count into a rendering artefact.
    """
    stages = [
        {"id": "frontier", "label": "Frontier", "note": "URLs discovered and waiting"},
        {"id": "fetch", "label": "Fetch ladder", "note": "C1 httpx → C2 Wayback → C3 CamoFox → C4 solver"},
        {"id": "parse", "label": "Parse & extract", "note": "text, links, PDFs"},
        {"id": "ingest", "label": "Ingest API", "note": "validation + document_id"},
        {"id": "store", "label": "Postgres + MinIO", "note": "documents table and PDF blobs"},
    ]
    flows = []

    def add(src, dst, val, kind, label):
        # A real 0 IS drawn — "the gate dropped nothing" is a measurement. A negative is not: page
        # counters are monotonic, so a negative means the source is corrupt, and passing it through
        # would draw a negative-width band and silently poison every total computed from it. Corrupt
        # is treated like absent (omitted), which the panel already renders as "—".
        if val is not None and val >= 0:
            flows.append({"from": src, "to": dst, "value": val, "kind": kind, "label": label})

    add("frontier", "fetch", fetched, "pass", "fetched")
    # Everything below hangs off `fetch`: these are the four ways a fetched page ends.
    add("fetch", "parse", kept, "pass", "kept")
    add("fetch", "drop_unchanged", unchanged, "drop", "unchanged since last crawl")
    add("fetch", "drop_gate", gated, "drop", "off-topic — dropped by the keyword gate")
    add("fetch", "drop_dupe", dupe, "drop", "duplicate content hash")

    accounted = [v for v in (kept, unchanged, gated, dupe) if v is not None]
    residual = None
    if fetched is not None and accounted:
        residual = fetched - sum(accounted)
        if residual > 0:
            flows.append({"from": "fetch", "to": "drop_failed", "value": residual, "kind": "drop",
                          "label": "fetch failed — no tier returned a usable page"})

    add("parse", "ingest", sent, "pass", "sent to ingest")
    if kept is not None and sent is not None and kept > sent:
        flows.append({"from": "parse", "to": "drop_transport", "value": kept - sent, "kind": "drop",
                      "label": "kept but never sent — the POST to ingest did not complete"})

    add("ingest", "store", accepted, "pass", "accepted into Postgres")
    if sent is not None and accepted is not None and sent > accepted:
        flows.append({"from": "ingest", "to": "drop_reject", "value": sent - accepted,
                      "kind": "drop", "label": "not accepted (failed a validation rule, or a "
                                               "document_id already held that exact content)"})

    # Whether the arithmetic closes, stated rather than assumed. A negative residual means the
    # counters genuinely disagree — the twin says so instead of drawing a shape that hides it.
    recon = None
    if residual is not None:
        recon = {
            "stage": "fetch", "total": fetched, "accounted": sum(accounted),
            "residual": residual,
            "conserves": residual >= 0,
            "note": ("Counters reconcile — the residual is fetch failures."
                     if residual >= 0 else
                     "Counters DISAGREE: more pages are accounted for than were fetched. These "
                     "are independent tallies sampled at slightly different moments, so treat "
                     "small gaps as timing, not loss."),
        }
    return {"stages": stages, "flows": flows, "reconciliation": recon}


def _batch(bmetrics, bstatus) -> dict | None:
    b = bmetrics if isinstance(bmetrics, dict) else {}
    s = bstatus if isinstance(bstatus, dict) else {}
    if not b and not s:
        return None
    doms = s.get("current_domains") if isinstance(s.get("current_domains"), list) else None
    return {
        "running": s.get("running", b.get("running")),
        "total": _num(s.get("total")) if s.get("total") is not None else _num(b.get("total")),
        "done": _num(s.get("done")) if s.get("done") is not None else _num(b.get("done")),
        "parallel": _num(s.get("parallel")),
        "stopping": b.get("stopping"),
        "started_at": s.get("started_at"),
        "current_domains": doms,
        # batch.current does NOT name the running job when a batch holds several — it shows one
        # arbitrary member or "(async pool)". Passed through labelled, so the panel can say so
        # rather than presenting it as "the job that is running".
        "current_label": b.get("current"),
    }


def _services(m: dict, tiers: dict | None, camofox: dict | None) -> list[dict]:
    """The static topology, hydrated. Structure is fixed because the deployment is fixed: naming
    the boxes that exist is the twin's job even when one of them fails to answer."""
    inst = m.get("instances") if isinstance(m.get("instances"), dict) else {}
    up, total = _num(inst.get("up")), _num(inst.get("total"))
    t = tiers if isinstance(tiers, dict) else {}

    def tier_status(key):
        row = t.get(key)
        if not isinstance(row, dict):
            return "unknown"
        return "up" if row.get("up") else "down"

    svcs = [
        {"id": "orchestrator", "label": "Orchestrator", "kind": "control", "status": "up",
         "detail": "catalogue, scheduler, job generator — you are talking to it"},
        {"id": "fleet", "label": "Crawler fleet", "kind": "compute",
         "status": ("up" if up and total and up >= total else
                    "degraded" if up else "unknown"),
         "detail": (f"{int(up)} of {int(total)} instances answering" if up and total else None)},
        {"id": "c1", "label": "C1 httpx", "kind": "fetch", "status": tier_status("c1")},
        {"id": "c2", "label": "C2 Wayback", "kind": "fetch", "status": tier_status("c2")},
        {"id": "c3", "label": "C3 CamoFox", "kind": "fetch", "status": tier_status("c3"),
         "detail": (camofox or {}).get("url") if isinstance(camofox, dict) else None},
        {"id": "c4", "label": "C4 host solver", "kind": "fetch", "status": tier_status("c4"),
         "detail": "real-GPU browser on the workstation, over a tunnel"},
        {"id": "ingest", "label": "Ingest API", "kind": "service", "status": "unknown",
         "detail": "validates and writes every kept page"},
        {"id": "postgres", "label": "Postgres · documents", "kind": "store", "status": "unknown"},
        {"id": "minio", "label": "MinIO · PDF blobs", "kind": "store", "status": "unknown"},
    ]
    return svcs


def _verdict(s: dict) -> dict:
    """One sentence on the state of the crawl, and the reason for it.

    Ranked by what actually stops a crawl, not by what is loudest. Unreachable beats everything;
    a fleet with instances down beats a throughput complaint, because the missing instances are
    usually WHY throughput is down.
    """
    if not s.get("reachable"):
        return {"state": "down", "headline": "The crawler is not answering",
                "detail": "No metrics returned — the twin has nothing live to mirror."}

    tp = s.get("throughput") or {}
    pps, running = tp.get("pages_per_sec"), tp.get("running")
    fleet = s.get("fleet") or {}
    down, total = fleet.get("not_answering"), fleet.get("total")
    per_fetch = (s.get("deflection") or {}).get("per_fetch")

    if not running:
        return {"state": "idle", "headline": "No crawl running",
                "detail": "The fleet is up and holding; nothing is being fetched."}

    rate = f"{pps:.2f} pages/s" if pps is not None else "an unreported rate"
    crawling = fleet.get("crawling")
    where = f" across {int(crawling)} instances" if crawling else ""
    head = f"Crawling at {rate}{where}"

    if down:
        n = f"{int(down)} of {int(total)}" if total else f"{int(down)}"
        if fleet.get("all_timeouts"):
            return {"state": "degraded", "headline": head,
                    "detail": (f"{n} instances did not answer the probe — every one of them timed "
                               "out rather than refused, which under load usually means busy, not "
                               "dead. Check `docker ps` before restarting anything.")}
        return {"state": "degraded", "headline": head,
                "detail": (f"{n} instances are not answering — their frontiers are pinned to them "
                           "and stall until they return.")}
    if per_fetch is not None and per_fetch >= 3:
        return {"state": "strained", "headline": head,
                "detail": (f"Host politeness is the binding constraint — {per_fetch:.1f} "
                           "deflections per fetch. The loop spends most of its iterations "
                           "bouncing off hosts that are already busy.")}
    return {"state": "healthy", "headline": head,
            "detail": "No instance down and no constraint binding hard."}


# ── The sandbox ──────────────────────────────────────────────────────────────────────────────

def simulate(params: dict | None = None, baseline: dict | None = None) -> dict:
    """What-if against the capacity model. PURE — no I/O of any kind, by design and by test.

    `baseline` is an optional second parameter set (normally the live configuration, read off a
    snapshot) so the answer can be a COMPARISON. A bare "4.8 pages/s" tells an operator nothing;
    "+33%, and the bottleneck moves from the render lane to host politeness" is the answer to the
    question they actually asked.
    """
    p = dict(DEFAULTS)
    for k, v in (params or {}).items():
        if k in DEFAULTS:
            p[k] = v

    run = _capacity(p)
    out: dict[str, Any] = dict(run)
    out["sandbox"] = True
    out["inputs"] = p

    if baseline:
        b = dict(DEFAULTS)
        for k, v in baseline.items():
            if k in DEFAULTS:
                b[k] = v
        base = _capacity(b)
        out["baseline"] = base
        bp, rp = base["pages_per_sec"], run["pages_per_sec"]
        out["delta"] = {
            "pages_per_sec": round(rp - bp, 3),
            "pages_per_sec_pct": round((rp - bp) / bp * 100, 1) if bp else None,
            "bottleneck_moved": base["bottleneck"] != run["bottleneck"],
            "bottleneck_from": base["bottleneck"], "bottleneck_to": run["bottleneck"],
            "hours_to_drain": (round(run["hours_to_drain"] - base["hours_to_drain"], 2)
                               if run["hours_to_drain"] is not None
                               and base["hours_to_drain"] is not None else None),
        }
        if base["bottleneck"] == run["bottleneck"] and run["bottleneck"] != "none":
            out.setdefault("notes", []).insert(
                0, f"The bottleneck did not move — it is still {run['bottleneck']}. "
                   "Changing anything else buys nothing until that ceiling is raised.")
    return out


def _pf(p: dict, key: str, lo: float, hi: float | None = None) -> float:
    """One model input, coerced and clamped, never raising.

    A sandbox that throws on a junk input is a sandbox the operator learns not to touch. The API
    route is already shielded by pydantic, but `simulate` is a plain function and the next caller
    may not be — a stray string or None used to raise ValueError/TypeError out of the model. Bad
    input falls back to the DEFAULT for that key rather than to zero, because zero is itself a
    meaningful setting (zero instances) and would quietly model a different system.
    """
    v = p.get(key, DEFAULTS.get(key))
    try:
        f = float(v)
    except (TypeError, ValueError):
        f = float(DEFAULTS.get(key) or 0)
    if f != f:                                  # NaN
        f = float(DEFAULTS.get(key) or 0)
    f = max(lo, f)
    return min(hi, f) if hi is not None else f


def _capacity(p: dict) -> dict:
    """Four ceilings; throughput is the lowest and the bottleneck is which one that was."""
    instances = _pf(p, "instances", 0.0)
    share = _pf(p, "render_share", 0.0, 1.0)
    hostc = _pf(p, "host_concurrency", 0.0)
    cores = _pf(p, "cpu_cores", 0.0)
    target = _pf(p, "cpu_target", 0.0, 100.0) / 100.0
    hosts = _pf(p, "unique_hosts", 0.0)
    rsec = _pf(p, "render_seconds", 0.01)
    hsec = _pf(p, "httpx_seconds", 0.01)
    frontier = _pf(p, "frontier", 0.0)
    dcost = _pf(p, "deflect_cost_s", 0.0)
    c3, c4 = bool(p.get("c3_up", True)), bool(p.get("c4_up", True))

    notes: list[str] = []
    lane = instances * LANE_PER_INSTANCE
    cores_avail = cores * target

    # Render path. With no browser tier at all, rendered pages have no route: they fail rather
    # than queue, so they leave the throughput calculation entirely and the crawl loses that
    # share of the corpus. That is a coverage loss, not a speed-up, and the note says so.
    if not c3 and not c4:
        eff_share = 0.0
        if share > 0:
            notes.append(
                f"C3 and C4 are both down: {share * 100:.0f}% of pages have no render path and "
                "will FAIL, not queue. Throughput below counts only the pages httpx can serve — "
                "it looks higher precisely because the hard pages are being dropped.")
    else:
        eff_share = share
        if not c3 and c4:
            notes.append("C3 is down, so every render falls to C4 on the workstation — one "
                         "browser behind a tunnel, far below the fleet's render lane.")

    cpu_per_render_page = CORES_PER_RENDER * rsec       # core-seconds
    cpu_per_httpx_page = HTTPX_CORE_COST * hsec
    core_s_per_page = eff_share * cpu_per_render_page + (1 - eff_share) * cpu_per_httpx_page
    cpu_ceiling = cores_avail / core_s_per_page if core_s_per_page > 0 else float("inf")

    if eff_share > 0:
        concurrent_renders = min(lane, cores_avail / CORES_PER_RENDER)
        if not c3 and c4:
            concurrent_renders = min(concurrent_renders, 2.0)   # one host browser, ~2 tabs
        render_ceiling = (concurrent_renders / rsec) / eff_share
    else:
        render_ceiling = float("inf")

    httpx_ceiling = (lane / hsec) / (1 - eff_share) if eff_share < 1 else float("inf")

    mean_latency = eff_share * rsec + (1 - eff_share) * hsec
    host_ceiling = (hosts * hostc / mean_latency) if mean_latency > 0 else float("inf")

    # Deflection, and what it COSTS. Every worker that wants a host already at its concurrency
    # limit bounces the URL back to the frontier. The oversubscription ratio — what the fetch side
    # could offer versus what politeness admits — drives how often that happens, and it is why
    # deflections outnumber fetches ~9:1 on the live stack.
    #
    # The bounce is cheap but NOT free: it costs the worker a scheduler round trip it cannot spend
    # fetching. At nine bounces per fetch that overhead becomes the real limit while every raw
    # capacity ceiling still looks healthy — the livelock signature where the box sits near-idle
    # and throughput is on the floor, because the scheduler starved itself rather than the sites
    # being slow. A model without this term reports ceilings the live system never reaches and is
    # unable to explain why.
    offer_parts = [v for v in (cpu_ceiling, render_ceiling, httpx_ceiling) if v != float("inf")]
    offered = min(offer_parts) if offer_parts else 0.0
    deflect = max(0.0, offered / host_ceiling - 1.0) if host_ceiling > 0 else 0.0
    slot_ceiling = (lane / (mean_latency + deflect * dcost)) if lane > 0 else float("inf")

    ceilings = {"cpu": cpu_ceiling, "render_lane": render_ceiling, "httpx": httpx_ceiling,
                "host": host_ceiling, "deflection_overhead": slot_ceiling}
    finite = {k: v for k, v in ceilings.items() if v != float("inf")}
    if finite:
        bottleneck = min(finite, key=lambda k: finite[k])
        pps = finite[bottleneck]
    else:
        bottleneck, pps = "none", 0.0

    if bottleneck == "host":
        notes.append(
            f"Host politeness binds first ({hosts:.0f} hosts x {hostc:.0f} concurrent). Adding "
            "instances will not help — it raises what the fleet offers, not what the hosts "
            "accept, so the extra capacity turns into deflections. Crawl MORE DOMAINS at once, "
            "or raise per-host concurrency if those hosts tolerate it.")
    elif bottleneck == "render_lane":
        notes.append(
            f"The C3 render lane binds ({lane:.0f} slots at {rsec:.0f}s a render). More instances "
            "raise it proportionally; so does anything that cuts the share of pages needing a "
            "browser.")
    elif bottleneck == "cpu":
        notes.append(
            f"CPU binds at {cores_avail:.0f} usable cores — about {CORES_PER_RENDER} cores per "
            "concurrent render on a box with no GPU. Only fewer renders or more cores move this.")
    elif bottleneck == "httpx":
        notes.append("The httpx lane binds — unusual, and it means renders are rare enough that "
                     "plain fetches are the whole cost. More instances help directly.")
    elif bottleneck == "deflection_overhead":
        notes.append(
            f"Neither the hardware nor the hosts bind — the SCHEDULER does. At ~{deflect:.1f} "
            f"bounces per fetch, workers spend {deflect * dcost:.1f}s of every "
            f"{mean_latency + deflect * dcost:.1f}s re-queueing URLs instead of fetching them. "
            "The box will look near-idle while throughput sits on the floor. Spreading the "
            "frontier over more hosts fixes this; adding instances makes it worse.")

    if deflect > 1:
        notes.append(f"Modelled ~{deflect:.1f} deflections per fetch: the fetch side could offer "
                     f"{offered:.1f} pages/s but politeness admits {host_ceiling:.1f}.")

    return {
        "pages_per_sec": round(pps, 3),
        "bottleneck": bottleneck,
        "ceilings": {k: (None if v == float("inf") else round(v, 3)) for k, v in ceilings.items()},
        "deflect_per_fetch": round(deflect, 2),
        "hours_to_drain": (round(frontier / (pps * 3600), 2) if pps > 0 and frontier else None),
        "render_share_effective": round(eff_share, 3),
        "notes": notes,
    }


def live_params(snap: dict) -> dict:
    """Read the model's inputs off a live snapshot, so the sandbox's baseline is THIS system
    rather than a default. Anything the snapshot did not report keeps its default and is named in
    `assumed`, because an operator comparing against a baseline deserves to know which parts of
    that baseline were measured."""
    p = dict(DEFAULTS)
    assumed: list[str] = []

    up = _get(snap, "fleet", "up")
    if up:
        p["instances"] = up
    else:
        assumed.append("instances")

    cores = _get(snap, "hardware", "cpu_count")
    if cores:
        p["cpu_cores"] = cores
    else:
        assumed.append("cpu_cores")

    target = _get(snap, "lanes", "adaptive", "target")
    if target:
        p["cpu_target"] = target
    else:
        assumed.append("cpu_target")

    frontier = _get(snap, "lanes", "frontier")
    if frontier:
        p["frontier"] = frontier
    else:
        assumed.append("frontier")

    # The browser tiers, read off the ladder. Without this the baseline modelled C3 and C4 as UP
    # while the map beside it showed both down — so every comparison ran against a fleet that had
    # a render path it did not actually have, which inflates the baseline and makes any scenario
    # look worse than it is. A baseline must describe the system as it IS.
    for tier in ("c3", "c4"):
        up = _get(snap, "ladder", tier, "up")
        if isinstance(up, bool):
            p[tier + "_up"] = up
        else:
            assumed.append(tier + "_up")

    # Contention, not headcount. The effective host count off the live issue distribution beats
    # the batch's domain list, which counts hosts that have no pending work as if they were
    # absorbing load. Fall back to the domain count only when there are no issues to measure.
    eff = effective_hosts(_get(snap, "issues", "by_host"))
    doms = _get(snap, "batch", "current_domains")
    if eff:
        p["unique_hosts"] = eff
    elif isinstance(doms, list) and doms:
        p["unique_hosts"] = len(doms)
        assumed.append("unique_hosts")
    else:
        assumed.append("unique_hosts")

    # render_share, host_concurrency, the deflection cost and the two latencies are not reported
    # anywhere: the crawler counts deflections and renders but never the mean round trip. They
    # stay at their defaults and are declared assumed — the honest answer, and precisely the list
    # of counters worth adding to the crawler if this model is ever to be trusted quantitatively.
    assumed += ["render_share", "host_concurrency", "render_seconds", "httpx_seconds",
                "deflect_cost_s"]

    out = {"params": p, "assumed": assumed}

    # Calibration: the twin scored against the system it mirrors. A twin that is never checked
    # against reality is a drawing, so the measured rate travels WITH the model's answer and the
    # panel can show both. A wide gap is information, not an embarrassment — it means one of the
    # assumed inputs above is wrong, and says so out loud rather than quietly reporting a
    # confident number.
    measured = _get(snap, "throughput", "pages_per_sec")
    if measured:
        modelled = _capacity(p)["pages_per_sec"]
        out["calibration"] = {
            "measured_pages_per_sec": measured, "modelled_pages_per_sec": modelled,
            "ratio": round(modelled / measured, 2) if measured else None,
            "note": ("Model and measurement agree within a factor of 1.5."
                     if measured and 0.67 <= modelled / measured <= 1.5 else
                     "Model and measurement disagree — at least one assumed input above is "
                     "wrong for this workload. Treat the ceilings as shape, not as forecast."),
        }
    return out


# ── Controls: the twin as a control surface, not just a window ────────────────────────────────
#
# A digital twin earns its name when the loop closes: watch the mirror, try it in the sandbox,
# then APPLY it — and watch the mirror move. This section is the "apply" half.
#
# Two rules shape everything below.
#
# **Nothing here invents a control.** Every action maps to an endpoint this orchestrator already
# exposes and the dashboard already uses; the twin adds a place to reach them from, not a new way
# to poke the crawler. Availability is DISCOVERED from the live router rather than hardcoded,
# because the data-centre tree runs ahead of this one (it has fleet/maintenance, fleet/prune and
# fleet/assign; this tree does not). A hardcoded list would offer buttons that 404 on one
# deployment and hide working ones on the other.
#
# **A knob the operator cannot actually turn must say so.** Most sandbox inputs have NO live
# control — cpu_target is read from the environment once per drain loop, so changing it needs a
# container restart, and a restart discards in-memory frontiers. Presenting those as adjustable
# would be the same lie as rendering a number no service returned. Each knob therefore carries how
# it is REALLY changed, and the UI reads that instead of guessing.

# risk: low     — reversible, no work lost, safe to click while crawling
#       medium  — changes what the fleet does next; recoverable but disruptive
#       high    — stops work in flight or drops a frontier; needs explicit confirmation
CONTROLS: list[dict] = [
    {"id": "refill_once", "label": "Place queued work now", "route": "/crawler/fleet/refill",
     "method": "POST", "risk": "low",
     "detail": "Runs one refill pass: any instance sitting idle gets the next job from the queue.",
     "why": "The usual fix when the queue has work but instances are idle."},

    {"id": "maintenance_on", "label": "Hold refill + autopilot", "method": "POST",
     "route": "/crawler/fleet/maintenance", "query": {"on": "true"}, "risk": "medium",
     "detail": "Stops auto-refill and the autopilot from claiming instances, without stopping "
               "the crawls already running.",
     "why": "The ONLY reliable way to place specific work: auto-refill otherwise appends to a "
            "running batch within seconds and the job you placed starves."},

    {"id": "maintenance_off", "label": "Resume refill + autopilot", "method": "POST",
     "route": "/crawler/fleet/maintenance", "query": {"on": "false"}, "risk": "medium",
     "detail": "Lifts the hold and immediately runs a forced refill.",
     "why": "The forced refill can resume dozens of saved jobs at once — this is the right tool "
            "for RESUMING work, and the wrong one for reducing load."},

    {"id": "rebalance", "label": "Rebalance domains across instances", "method": "POST",
     "route": "/crawler/fleet/rebalance", "risk": "medium",
     "detail": "Re-assigns which instance owns which domain.",
     "why": "New instances own zero domains until this runs. Shrinking the fleet can strand a "
            "frontier, so read what it reports before trusting it."},

    {"id": "stop_crawl", "label": "Stop the running batch", "method": "POST",
     "route": "/crawler/stop", "risk": "high",
     "detail": "Halts the batch in flight. Captured pages are kept and the frontier is preserved "
               "for resume. This IS the hard stop: every lane closes and every timer is disabled "
               "until Start.",
     "why": "Stops work that is running. Only do this deliberately."},

    {"id": "stop", "label": "STOP — halt everything, stay stopped", "method": "POST",
     "route": "/control/stop", "risk": "high",
     "detail": "Halts every instance, closes every lane and hard-disables the scheduler tick, "
               "auto-refill, the autopilot and the daily re-pin until Start. Frontiers are kept.",
     "why": "The one stop that nothing on a timer can undo."},

    {"id": "start", "label": "START — open the current mode's lanes", "method": "POST",
     "route": "/control/start", "risk": "medium",
     "detail": "Lifts the stop. Timers whose toggle is on resume on their next tick; a campaign "
               "starts when its batch is submitted.",
     "why": "The only thing that lifts a stop."},

    {"id": "master_off", "label": "Master switch OFF", "method": "POST", "route": "/control/master",
     "body": {"enabled": False}, "risk": "high",
     "detail": "The same hard stop as STOP, but waits for teardown before answering.",
     "why": "The big red switch. Off means off."},

    {"id": "master_on", "label": "Master switch ON", "method": "POST", "route": "/control/master",
     "body": {"enabled": True}, "risk": "medium",
     "detail": "The same as START.", "why": "Resumes normal operation."},
]

# How each sandbox knob is REALLY changed on the running system. `live` means an action on this
# page can change it now; `restart` means the value is read from the environment and only a
# container restart applies it — which discards in-memory frontiers, so it is never offered as a
# button; `workload` means it is not a setting at all but a property of what you chose to crawl.
KNOB_ACTIONS: dict[str, dict] = {
    "instances": {
        "kind": "live",
        "how": "The fleet is a fixed set of containers, but how many are WORKING is controlled by "
               "refill and rebalance. Use 'Place queued work now' to fill idle instances."},
    "unique_hosts": {
        "kind": "workload",
        "how": "Not a setting — it is how many distinct hosts your queued work spans, weighted by "
               "contention. Raise it by crawling more domains at once instead of piling a deep "
               "frontier onto a few. Usually the highest-value change available on this system."},
    "host_concurrency": {
        "kind": "restart",
        "how": "Adaptive between 3 and 48, governed by CPU. The floor and ceiling come from the "
               "environment (CRAWLER_ADAPTIVE_MIN/MAX) and need a container restart."},
    "cpu_target": {
        "kind": "restart",
        "how": "CRAWLER_CPU_TARGET, read once per drain loop — a change applies to the NEXT batch, "
               "not the running one, and setting it at all needs a container restart."},
    "cpu_cores": {"kind": "fixed", "how": "Physical. 40 cores, shared with the other stacks."},
    "render_share": {
        "kind": "workload",
        "how": "The fraction of pages needing a real browser. Driven by which SITES you crawl "
               "rather than by a setting — JS-heavy sites raise it."},
    "render_seconds": {"kind": "workload", "how": "How slow the sites you crawl are to render."},
    "httpx_seconds": {"kind": "workload", "how": "How slow the sites you crawl are to answer."},
    "c3_up": {"kind": "live",
              "how": "C3 is the CamoFox container. If it is wedged, restarting THAT container is "
                     "safe — it holds no frontier."},
    "c4_up": {"kind": "live",
              "how": "C4 is the workstation host-solver behind a tunnel. Restart the tunnel or the "
                     "solver on the workstation."},
    "frontier": {"kind": "workload",
                 "how": "How much work is queued. Grows as pages are discovered."},
    "deflect_cost_s": {"kind": "modelled",
                       "how": "A model constant, not a system setting — what one bounce costs a "
                              "worker. Exposed so you can test how sensitive the answer is to it."},
}

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


def control_plan(available: set[str] | None, state: dict | None = None) -> list[dict]:
    """The controls this deployment can actually offer.

    `available` is the set of route paths the running router exposes; a control whose route is not
    there comes back with `available: false` and the reason rather than being dropped. Showing an
    operator that a capability EXISTS but is missing from this build is information — silently
    omitting it makes two deployments look identical when they are not.
    """
    out = []
    for c in CONTROLS:
        row = dict(c)
        row["available"] = available is None or c["route"] in available
        if not row["available"]:
            row["unavailable_reason"] = (
                f"this build exposes no {c['route']} route — the data-centre tree runs ahead of "
                "the local one and this control exists only there")
        out.append(row)
    if isinstance(state, dict) and "master" in state:
        for row in out:
            if row["id"] == "master_off" and state.get("master") is False:
                row["current"] = "already off"
            if row["id"] == "master_on" and state.get("master") is True:
                row["current"] = "already on"
    return out


def find_control(cid: str) -> dict | None:
    """Look up one control by id. The whitelist IS the registry: an id that is not in it can never
    be dispatched, so the apply route cannot be talked into calling an arbitrary endpoint."""
    return next((dict(c) for c in CONTROLS if c["id"] == cid), None)


def knob_guidance() -> dict:
    """Every sandbox input paired with how it is really changed. Read by the panel so that a
    slider never implies a control which does not exist."""
    return {k: dict(v, default=DEFAULTS.get(k)) for k, v in KNOB_ACTIONS.items()}


# ── Diagnosis: name the fault, don't just draw it ─────────────────────────────────────────────
#
# The mirror shows counters and the sandbox explores capacity. Neither tells an operator at 3am
# that "render_q 2698 behind a lane at 28 of 192" is a scheduler that starved itself rather than
# a fleet that is busy. Every signature below is a fault this system has actually produced, and
# each one was originally diagnosed by hand from exactly these numbers — so encoding them costs
# nothing new and saves the reading.
#
# Three rules.
#
# **A finding needs evidence, and the evidence is the numbers that triggered it.** No finding may
# say "throughput is low" without quoting what it read. An operator must be able to check the
# twin's reasoning rather than trust it.
#
# **A missing counter produces NO finding.** Absent is not zero. A rule whose inputs are missing
# is skipped silently — inventing a fault from a gap is worse than staying quiet, because a false
# alarm here sends someone to restart a container that was fine.
#
# **Where a fault has a known WRONG fix, the finding says so.** The single most expensive mistake
# on this system is restarting a container that is merely busy: instances report "down" when a
# probe times out under load, and a restart discards that instance's in-memory frontier. Any
# finding that could tempt a restart carries that warning.

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def diagnose(snap: dict) -> list[dict]:
    """Named faults read off a live snapshot, most severe first.

    Returns a list of findings, each with: id, severity, headline, evidence (the numbers that
    fired the rule), action (what to actually do), and optionally control (the id of a control on
    this page that addresses it) and warning (a known wrong fix to avoid).
    """
    if not isinstance(snap, dict) or not snap.get("reachable"):
        return [{
            "id": "unreachable", "severity": "critical",
            "headline": "The crawler is not answering",
            "evidence": "No metrics returned from the fleet.",
            "action": "Check the crawler containers are up before reading anything else here — "
                      "every panel below is blank for the same reason.",
        }]

    def sect(key: str) -> dict:
        """A section of the snapshot, or an empty dict.

        NOT `snap.get(k) or {}` — that returns the value itself when it is a non-empty string or
        list, and the next `.get` raises AttributeError. Diagnosis reads a payload assembled from
        several services and must survive any shape one of them returns.
        """
        v = snap.get(key)
        return v if isinstance(v, dict) else {}

    out: list[dict] = []
    lanes, hw, tp = sect("lanes"), sect("hardware"), sect("throughput")
    defl, fleet, issues, ladder = sect("deflection"), sect("fleet"), sect("issues"), sect("ladder")
    _flows = sect("pipeline").get("flows")
    pipe = [f for f in _flows if isinstance(f, dict)] if isinstance(_flows, list) else []

    lane_cap = _num(lanes.get("render_lane_cap"))
    lane_used = _num(lanes.get("render_lane_used"))
    rq = _num(lanes.get("render_q"))
    cpu = _num(hw.get("cpu_percent"))
    pps = _num(tp.get("pages_per_sec"))
    per_fetch = _num(defl.get("per_fetch"))
    running = tp.get("running")

    # 1 · Render-lane starvation. A deep queue behind a lane that is mostly EMPTY is not a busy
    # fleet — it is work that is admitted nowhere. A full lane with a deep queue would be healthy
    # saturation, which is why the rule tests the lane's emptiness and not the queue's depth alone.
    if rq is not None and lane_cap and lane_used is not None and rq > 50 and lane_used < lane_cap * 0.5:
        out.append({
            "id": "render_lane_starved", "severity": "critical",
            "headline": "Render queue is deep while the render lane sits mostly idle",
            "evidence": f"{int(rq)} pages queued for a browser, but only {int(lane_used)} of "
                        f"{int(lane_cap)} render slots are in use "
                        f"({lane_used / lane_cap * 100:.0f}% of the lane).",
            "action": "Work is queued and not being admitted. Check C3 (CamoFox) is actually "
                      "serving — a wedged browser stays 'healthy' while refusing every tab, which "
                      "looks exactly like this. Restarting the CamoFox container is safe: it "
                      "holds no frontier.",
        })

    # 2 · Deflection livelock. Bounces vastly outnumbering fetches WITH a quiet box is the
    # signature where the scheduler starved itself: workers spend their iterations re-queueing
    # URLs whose hosts are busy. The CPU condition is what separates it from ordinary politeness.
    if per_fetch is not None and per_fetch >= 5:
        quiet = cpu is not None and cpu < 60
        out.append({
            "id": "deflection_livelock",
            "severity": "critical" if quiet else "warning",
            "headline": ("The scheduler is bouncing far more than it fetches"
                         + (" while the box sits quiet" if quiet else "")),
            "evidence": (f"{per_fetch:.1f} host deflections per fetch"
                         + (f", CPU {cpu:.0f}%" if cpu is not None else "")
                         + (f", {pps:.2f} pages/s" if pps is not None else "")),
            "action": "The frontier is piled onto too few hosts, so most loop iterations hit a "
                      "host already at its politeness limit. Crawl MORE DOMAINS at once rather "
                      "than driving a deep frontier on a few — the sandbox on this page will show "
                      "you the size of that change.",
            "warning": "Adding instances makes this WORSE, not better: it raises what the fleet "
                       "offers without raising what the hosts accept.",
            "knob": "unique_hosts",
        })

    # 3 · Instances not answering. The roll-up's "down" means the probe timed out, which under
    # load usually means busy. This is the finding most likely to provoke the one genuinely
    # destructive reaction, so the warning is the point of it.
    not_answering = _num(fleet.get("not_answering"))
    if not_answering is None:
        not_answering = _num(fleet.get("down"))
    total = _num(fleet.get("total"))
    if not_answering and total:
        all_to = fleet.get("all_timeouts")
        out.append({
            "id": "instances_not_answering",
            "severity": "warning" if all_to else "critical",
            "headline": f"{int(not_answering)} of {int(total)} instances did not answer the probe",
            "evidence": ("every failure was a timeout rather than a refusal"
                         if all_to else "at least one instance refused the connection"),
            "action": ("Timeouts under load usually mean busy, not dead — confirm with "
                       "`docker ps` before doing anything."
                       if all_to else
                       "A refused connection means the process is gone; check the container."),
            "warning": "Do NOT restart an instance that is merely busy — a restart discards its "
                       "in-memory frontier, and the domains pinned to it stall until it returns.",
        })

    # 4 · A browser tier is down. Only material if renders are actually being asked for.
    for key, name, note in (
            ("c3", "C3 CamoFox", "the fleet's render tier — without it, JS-heavy pages fail"),
            ("c4", "C4 host solver", "the workstation GPU browser used for interactive "
                                     "Cloudflare/Imperva challenges")):
        row = ladder.get(key)
        if isinstance(row, dict) and row.get("up") is False:
            wants_render = (rq or 0) > 0
            out.append({
                "id": f"{key}_down",
                "severity": "warning" if wants_render else "info",
                "headline": f"{name} is not answering",
                "evidence": str(row.get("detail") or "probe reported down")
                            + (f"; {int(rq)} pages are queued for a render" if wants_render else ""),
                "action": f"{name} is {note}. Pages needing it will fail rather than queue, so "
                          "this is a coverage loss, not a slowdown — throughput can even look "
                          "better while the hard pages are being dropped.",
                "knob": f"{key}_up",
            })

    # 5 · Host concentration. Politeness is per-host, so one domain owning most of the contention
    # caps the whole crawl however many domains are nominally in the batch.
    by_host = issues.get("by_host")
    eff = effective_hosts(by_host)
    if eff is not None and isinstance(by_host, dict) and by_host:
        top, topv = max(by_host.items(), key=lambda kv: _num(kv[1]) or 0)
        tot = sum(v for v in (_num(x) for x in by_host.values()) if v)
        share = (_num(topv) or 0) / tot if tot else 0
        if eff < 15 or share > 0.2:
            out.append({
                "id": "host_concentration", "severity": "warning",
                "headline": "Contention is concentrated on a handful of hosts",
                "evidence": f"{len(by_host)} hosts listed, but an effective count of {eff:.1f}; "
                            f"{top} alone holds {share * 100:.0f}% of the contention.",
                "action": "Politeness is a per-host limit, so a frontier piled on a few domains "
                          "behaves like only a few hosts. Spreading work over more domains is the "
                          "highest-value change available here.",
                "knob": "unique_hosts",
            })

    # 6 · Ingest rejecting a large share of what is sent. Some rejection is normal (an unchanged
    # page collides on document_id), so only a big share is worth raising.
    sent = next((f["value"] for f in pipe if f.get("to") == "ingest"), None)
    rej = next((f["value"] for f in pipe if f.get("to") == "drop_reject"), None)
    if sent and rej is not None and sent > 100 and rej / sent > 0.3:
        out.append({
            "id": "ingest_rejecting", "severity": "warning",
            "headline": "Ingest is not accepting a large share of what the crawler sends",
            "evidence": f"{int(rej)} of {int(sent)} sent pages were not accepted "
                        f"({rej / sent * 100:.0f}%).",
            "action": "Some rejection is normal — an unchanged page collides on its content-"
                      "derived document_id. A sustained high share instead points at a validation "
                      "rule rejecting real pages; check the ingest logs for the failing rule.",
        })

    # 7 · Running but fetching nothing. Distinguishes a stalled crawl from an idle fleet.
    if running and pps is not None and pps < 0.1:
        out.append({
            "id": "stalled", "severity": "critical",
            "headline": "A crawl is running but almost nothing is being fetched",
            "evidence": f"{pps:.3f} pages/s with the batch reporting running.",
            "action": "Check whether the frontier still holds eligible work, and whether the "
                      "instances are wedged in teardown — running with an empty pending queue on "
                      "an idle box is a teardown hang, and stop_crawl will not clear it.",
        })

    # 8 · The reading itself is old. A stale number that is not labelled stale reads as a live one.
    cache = sect("_cache")
    if cache.get("stale"):
        age = _num(cache.get("age_s"))
        out.append({
            "id": "stale_reading", "severity": "info",
            "headline": "These numbers are from the last good reading, not from now",
            "evidence": (f"cached {age:.0f}s ago" if age is not None else "cache reported stale")
                        + (f"; refresh failed: {cache['error']}" if cache.get("error") else ""),
            "action": "A refresh is running behind this page. If it keeps failing, the service "
                      "that owns these numbers is the one to look at.",
        })

    out.sort(key=lambda f: SEVERITY_ORDER.get(f["severity"], 9))
    return out


# ── self-check ───────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # The mirror invents nothing.
    s = build_snapshot({"reachable": True, "progress": {"running": True, "pages_per_sec": 3.6,
                                                        "totals": {"fetched": 100, "kept": 40}}},
                       None, None, None)
    flows = {(f["from"], f["to"]): f["value"] for f in s["pipeline"]["flows"]}
    assert flows[("frontier", "fetch")] == 100.0
    assert flows[("fetch", "parse")] == 40.0
    # The 60 pages that were fetched but not kept and not explained by any drop counter are real
    # fetch failures, and get their OWN edge rather than being rounded away.
    assert flows[("fetch", "drop_failed")] == 60.0
    # Counters nobody reported stay absent — never drawn as a zero-width edge.
    assert ("fetch", "drop_gate") not in flows and ("fetch", "drop_dupe") not in flows
    assert s["deflection"]["per_fetch"] is None
    assert build_snapshot(None, None, None, None)["verdict"]["state"] == "down"

    # Effective hosts, not host count: one domain holding a quarter of the contention drags the
    # effective number far below the number of names.
    assert effective_hosts({"a": 20027, "b": 8821, "c": 8032}) == 2.5, "skew drags 3 hosts to 2.5"
    assert effective_hosts({"a": 10, "b": 10, "c": 10, "d": 10}) == 4.0, "even spread == headcount"
    assert effective_hosts({}) is None and effective_hosts(None) is None

    # The sandbox is a comparison and it names the wall.
    r = simulate({"instances": 10}, baseline={"instances": 6})
    assert r["sandbox"] is True and r["ceilings"]["cpu"] is not None

    # The model lands near the live measurement at the live operating point — 3.63 pages/s and
    # 9.02 deflections per fetch. This is the assertion that catches a change to the physics.
    # Every input pinned, so this tests the physics and not the current defaults.
    LIVE = {"unique_hosts": 3, "host_concurrency": 3, "instances": 10, "render_share": 0.10,
            "cpu_cores": 40, "cpu_target": 85}
    piled = simulate(LIVE)
    assert 3.0 < piled["pages_per_sec"] < 4.2, piled["pages_per_sec"]     # measured 3.63
    assert 6 < piled["deflect_per_fetch"] < 10, piled["deflect_per_fetch"]  # measured 9.02

    # A frontier piled onto a few hosts cannot be rescued with hardware: doubling the fleet buys
    # nothing, because it raises what the fleet OFFERS and not what the hosts accept.
    assert simulate({**LIVE, "instances": 20})["pages_per_sec"] \
        <= piled["pages_per_sec"] * 1.01, "more instances must not rescue a host-bound crawl"
    # Spreading the same frontier over more hosts is what actually helps, and by a lot.
    spread = simulate({**LIVE, "unique_hosts": 25})
    assert spread["pages_per_sec"] > piled["pages_per_sec"] * 3
    assert spread["deflect_per_fetch"] < piled["deflect_per_fetch"]

    # Losing both browser tiers is a coverage loss reported as such, never a silent speed-up.
    dead = simulate({"c3_up": False, "c4_up": False, "render_share": 0.35})
    assert dead["render_share_effective"] == 0.0
    assert any("will FAIL" in n for n in dead["notes"])

    print("twin self-check ok —", r["pages_per_sec"], "pages/s,", r["bottleneck"], "bound")

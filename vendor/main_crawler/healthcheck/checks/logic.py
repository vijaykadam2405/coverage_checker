"""AREA = "logic" - is the crawler's CONTROL PLANE actually behaving in production?

Every other health area proves the crawl *fetched* something. This one proves the machinery that
DECIDES what to fetch - the scheduler, the per-instance sharding, the operator category switches,
the adaptive tiering, the reserved daily lane and the fleet roster - is in a healthy, self-consistent
state right now. The faults these guard against are the quiet kind: a maintenance flag left on that
silently pauses the fleet, a category bucket that vanished from the panel, one instance holding all
the work while the rest idle, a scheduler task that died in the background, a daily lane pointed at a
container that does not exist. None of those show up as an error anywhere; they just mean nothing (or
the wrong thing) is being crawled, and the only way to catch them is to read the running system's own
state and assert on it.

All of the live checks are READ-ONLY. They GET a handful of the orchestrator's report endpoints
(``/api/control/state``, ``/api/control/categories``, ``/api/crawler/fleet/health``,
``/api/schedule/forecast``) - never the switches that mutate them. In particular this module does
NOT call ``/api/selfcheck``: that endpoint writes a canary document into the ingest store, which is a
write, so it is off-limits to a read-only health probe.

!  THE UNIT SUITES ARE OPT-IN, AGAINST A SEPARATE DATABASE.  !
The crawler and orchestrator unit suites (``crawler/tests``, ``orchestrator/tests``) are NOT run by
default. Several of those tests talk to whatever ``DATABASE_URL`` names and exercise dedupe/delete
paths with apply=True - an unscoped run has already deleted production corpus rows once. So the two
``unit suite:`` checks below SKIP unless the operator explicitly opts in with BOTH ``HC_RUN_UNIT=1``
AND a throwaway ``HC_TEST_DATABASE_URL`` that is verifiably NOT the production ``DATABASE_URL``. If
either is missing, or the two URLs match, the check SKIPS loudly and touches nothing. This is the
only place in the health suite that runs another process, and it is deliberately hard to fire.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

from .. import hc

# -- canonical control-plane facts, read from the source under orchestrator/src --------------------
# categories.BUCKETS - the switchable operator vocabulary. Kept as a literal (the healthcheck does
# not import the orchestrator package, which may not be installed here) and pinned to the source: if
# categories.py grows a bucket, this list must grow with it or the check will (correctly) fail.
# Source: orchestrator/src/mallory_orchestrator/categories.py :: BUCKETS
EXPECTED_BUCKETS = ("manufacture", "news", "tender", "think_tank", "gov_portal", "general")

# tiering.TIER_SECONDS keys - the only cadence tiers a site schedule may carry.
# Source: orchestrator/src/mallory_orchestrator/tiering.py :: TIER_SECONDS
VALID_TIERS = {"daily", "weekly", "monthly"}


def _orch(path: str):
    """GET a JSON report endpoint on the orchestrator. Raises (-> FAIL) on transport/non-200/non-JSON."""
    return hc.http_json(f"{hc.ORCH_BASE}{path}")


# ==============================================================================================
#  Live, read-only control-plane probes
# ==============================================================================================

@hc.check(area="logic", name="maintenance flag is off")
def maintenance_off() -> str:
    """Maintenance mode silently freezes the fleet: the scheduler, refill watcher and autopilot all
    stand down while it is set, and it does not clear itself. In normal operation it must be OFF, or
    the crawl is paused and every downstream freshness number slowly goes stale with no error.

    Reads the flag (never toggles it) from /api/control/state and reports it alongside the other two
    switches that can also hold the fleet still, so a failure names which one is set."""
    st = _orch("/api/control/state")
    maint = bool(st.get("maintenance"))
    ctx = f"master={st.get('master')} halted={st.get('halted')}"
    hc.need(not maint,
            f"maintenance mode is ON - the fleet is paused (scheduler/refill/autopilot stand down); "
            f"{ctx}")
    return f"maintenance OFF ({ctx})"


@hc.check(area="logic", name="category buckets incl general")
def category_buckets() -> str:
    """The control panel's stop switches. The set the crawler exposes must match the canonical
    operator vocabulary in categories.py - in particular it must include the newer "general" bucket
    (mainstream non-defence press), which is a SEPARATE switch from "news" (defence trade press) so
    that stopping one cannot stop the other. A missing bucket means a whole class of sites has no
    switch and cannot be stopped.

    Reads /api/control/categories (read-only) and asserts every expected bucket is present."""
    rows = _orch("/api/control/categories")
    hc.need(isinstance(rows, list) and rows, "categories endpoint returned no rows")
    live = {str(r.get("category")) for r in rows}
    missing = [b for b in EXPECTED_BUCKETS if b not in live]
    hc.need(not missing, f"category buckets missing from the live panel: {missing} (have {sorted(live)})")
    hc.need("general" in live, "the 'general' news bucket is absent - general and trade press share a switch")
    off = sorted(str(r.get("category")) for r in rows if not r.get("enabled"))
    extra = sorted(live - set(EXPECTED_BUCKETS))
    return (f"{len(EXPECTED_BUCKETS)} buckets present incl general"
            + (f"; disabled={off}" if off else "; all enabled")
            + (f"; extra rows={extra}" if extra else ""))


@hc.check(area="logic", name="scheduler alive + backlog")
def scheduler_sanity() -> str:
    """The scheduler runs as a background asyncio task; if it dies, the whole control panel keeps
    serving and nothing new is ever dispatched - a silent stop. This asserts the task is alive and
    reports the coherent schedule state: the next due time and the current backlog (jobs due now,
    summed across the operator buckets).

    A large backlog is NOT failed on its own - dispatch is deliberately paced and capped by
    CRAWLER_MAX_BUSY_INSTANCES, so a due-now number in the hundreds is normal steady state. A dead
    scheduler is the real fault, so that is what fails."""
    st = _orch("/api/control/state")
    alive = bool(st.get("scheduler_alive"))
    hc.need(alive, "the scheduler background task is NOT alive - nothing will be dispatched")
    # Backlog = jobs due now across all buckets (same predicate the panel and the next tick use).
    try:
        cats = _orch("/api/control/categories")
        due_now = sum(int(r.get("due_now") or 0) for r in cats)
    except Exception:  # noqa: BLE001 - backlog is context, not the assertion
        due_now = -1
    nxt = st.get("next_due")
    counts = f"enabled={st.get('enabled')} paused={st.get('paused')} total={st.get('total')}"
    backlog = f"backlog(due_now)={due_now}" if due_now >= 0 else "backlog=unknown"
    return (f"scheduler alive, tick={st.get('tick_seconds')}s, next_due={nxt or 'none'}, "
            f"{backlog}, {counts}")


@hc.check(area="logic", name="fleet instances all reporting")
def fleet_reporting() -> str:
    """Every crawler instance in the configured pool must be answering. An instance that stops
    reporting is one that has silently dropped its share of the work - its domains' frontiers live
    only inside that container, so nothing else can pick them up. FAILs if any configured instance
    is unreachable, or (when HC_EXPECTED_INSTANCES is set) if the pool has shrunk below the expected
    size.

    Reads /api/crawler/fleet/health (read-only)."""
    fh = _orch("/api/crawler/fleet/health")
    if not fh.get("reachable"):
        raise AssertionError(f"no crawler instance answered (verdict={fh.get('verdict')}): "
                             f"{fh.get('detail')}")
    inst = fh.get("instances") or {}
    total, up = int(inst.get("total") or 0), int(inst.get("up") or 0)
    hc.need(total > 0, "the fleet pool is empty (no instances configured)")
    hc.need(up >= total,
            f"{total - up} of {total} configured instance(s) are not reporting - "
            f"their share of the crawl is stranded")
    expected = hc.cfg("HC_EXPECTED_INSTANCES", "").strip()
    if expected.isdigit():
        hc.need(total >= int(expected),
                f"pool has {total} instance(s), expected at least {expected} "
                f"(HC_EXPECTED_INSTANCES) - the fleet shrank")
    return (f"{up}/{total} instances reporting"
            + (f", expected>={expected}" if expected.isdigit() else "")
            + f", verdict={fh.get('verdict')}")


@hc.check(area="logic", name="shard distribution across fleet")
def shard_distribution() -> str:
    """Is the crawl work actually SPREAD, or piled on one instance? A domain is pinned to a single
    instance for the life of its frontier (assign-once sharding), so a routing that hashes everything
    onto one box leaves the rest idle while a six-figure backlog sits unreachable elsewhere. This
    reads the live per-instance pending distribution and fails when one instance holds a
    pathological share of a meaningful amount of queued work.

    Tunables: HC_SHARD_MAX_SHARE (default 0.90), HC_SHARD_MIN_PENDING (default 1000). SKIPs when
    there is too little queued work to judge spread, or when the fleet is not reachable (the fleet
    check owns that failure). Reads /api/crawler/fleet/health (read-only)."""
    fh = _orch("/api/crawler/fleet/health")
    if not fh.get("reachable"):
        raise hc.Skip("fleet not reachable - see the fleet-instances check")
    per = fh.get("per_instance") or []
    up = int((fh.get("instances") or {}).get("up") or 0)
    total_pending = int(fh.get("pending_total") or 0)
    min_pending = int(float(hc.cfg("HC_SHARD_MIN_PENDING", "1000")))
    if up < 3:
        raise hc.Skip(f"only {up} instance(s) up - spread is not meaningful")
    if total_pending < min_pending:
        raise hc.Skip(f"only {total_pending:,} URL(s) queued fleet-wide (< {min_pending:,}) - "
                      f"too little work to judge spread")
    busiest = fh.get("busiest") or (max(per, key=lambda p: p.get("pending", 0)) if per else {})
    share = (int(busiest.get("pending") or 0) / total_pending) if total_pending else 0.0
    max_share = float(hc.cfg("HC_SHARD_MAX_SHARE", "0.90"))
    top = ", ".join(f"i{p['instance']}={p.get('pending', 0):,}"
                    for p in sorted(per, key=lambda p: -p.get("pending", 0))[:5])
    hc.need(share <= max_share,
            f"instance {busiest.get('instance')} holds {share:.0%} of {total_pending:,} queued "
            f"URL(s) (> {max_share:.0%}) - work is not spread. top: {top}")
    return f"busiest {share:.0%} of {total_pending:,} queued (cap {max_share:.0%}); top: {top}"


@hc.check(area="logic", name="daily lane self-consistent")
def daily_lane() -> str:
    """The reserved daily lane runs a URL list on specific instance indices. Those indices are only
    valid if the instances actually exist in the running pool - an index past the end silently points
    the lane at a container holding a DIFFERENT site's frontier (or nothing). This mirrors
    daily.reservation()'s own consistency rule: every reserved index must be < pool size.

    Lane config is read from the environment (CRAWLER_DAILY_INSTANCES, or the older singular
    CRAWLER_DAILY_INSTANCE; override with HC_DAILY_INSTANCES) and the pool size from the live fleet.
    SKIPs when the lane is disabled (no indices configured)."""
    raw = (hc.cfg("HC_DAILY_INSTANCES", "")
           or hc.cfg("CRAWLER_DAILY_INSTANCES", "")
           or hc.cfg("CRAWLER_DAILY_INSTANCE", "")).strip()
    if not raw:
        raise hc.Skip("daily lane disabled (CRAWLER_DAILY_INSTANCES unset)")
    want = sorted({int(p) for p in raw.replace(";", ",").split(",")
                   if p.strip().lstrip("-").isdigit() and int(p) >= 0})
    if not want:
        raise hc.Skip(f"daily lane disabled (no valid instance index in {raw!r})")
    fh = _orch("/api/crawler/fleet/health")
    if not fh.get("reachable"):
        raise hc.Skip("fleet not reachable - cannot validate the lane against the pool")
    pool = int((fh.get("instances") or {}).get("total") or 0)
    missing = [i for i in want if i >= pool]
    hc.need(not missing,
            f"daily lane reserves instance(s) {missing} but the pool has only {pool} "
            f"(indices 0..{pool - 1}) - the lane points at a container that does not exist")
    exclusive = hc.cfg("CRAWLER_DAILY_EXCLUSIVE", "0").lower() in ("1", "true", "yes", "on")
    shared = len(want) >= pool  # the lane claims the whole pool: honest, but no punctuality promise
    return (f"lane on instances {want} of pool {pool}, "
            f"{'exclusive' if exclusive else 'priority'}"
            + (" (SHARED: lane claims the whole pool)" if shared else ""))


@hc.check(area="logic", name="tier assignments plausible")
def tiering_plausible() -> str:
    """Sources are placed on an adaptive cadence tier (daily/weekly/monthly) driven by measured
    change rate. This asserts the tiering engine has actually populated tiers and that every assigned
    tier is a valid one - an unknown tier key means the engine drifted from tiering.TIER_SECONDS.
    Reports the distribution across tiers and phases and the forecast's worker-sizing.

    Reads /api/schedule/forecast (read-only). SKIPs when no site schedules exist yet (nothing to
    judge - a fresh deployment before the census)."""
    fc = _orch("/api/schedule/forecast")
    by_tier = fc.get("by_tier") or {}
    total = sum(int(v or 0) for v in by_tier.values())
    if total == 0:
        raise hc.Skip("no site schedules populated yet - tiering not exposed")
    bad = sorted(t for t, n in by_tier.items() if (n or 0) > 0 and t not in VALID_TIERS)
    hc.need(not bad, f"site schedules carry unknown tier(s) {bad} - not in {sorted(VALID_TIERS)}")
    phases = fc.get("phase_counts") or {}
    workers = (fc.get("sizing") or {}).get("required_parallel_workers")
    dist = ", ".join(f"{t}={int(by_tier.get(t) or 0)}" for t in ("daily", "weekly", "monthly"))
    return f"{total} scheduled site(s): {dist}; phases={phases}; sized for {workers} worker(s)"


# ==============================================================================================
#  OPTIONAL, GUARDED unit-suite runners - see the module-level warning. Off by default.
# ==============================================================================================

# Repo root: .../Production crawler  (this file is <root>/healthcheck/checks/logic.py)
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _norm_db(url: str) -> str:
    """Normalise a DSN enough to compare two spellings of the same database safely."""
    u = (url or "").strip().rstrip("/")
    if u.startswith("postgresql+"):
        u = "postgresql://" + u.split("://", 1)[1]
    return u.lower()


def _unit_guard() -> str:
    """Return the vetted test DATABASE_URL, or raise hc.Skip with a loud reason. NEVER returns a URL
    that could be the production database - that is the whole point of this function."""
    if hc.cfg("HC_RUN_UNIT", "0") != "1":
        raise hc.Skip("unit suites not run - set HC_RUN_UNIT=1 + HC_TEST_DATABASE_URL")
    test_db = hc.cfg("HC_TEST_DATABASE_URL", "")
    if not test_db:
        raise hc.Skip("REFUSING to run unit suites: HC_RUN_UNIT=1 but HC_TEST_DATABASE_URL is unset "
                      "- the suites would run against the default DATABASE_URL and can mutate it")
    prod = hc.DATABASE_URL or ""
    if prod and _norm_db(test_db) == _norm_db(prod):
        raise hc.Skip("REFUSING to run unit suites: HC_TEST_DATABASE_URL equals the production "
                      "DATABASE_URL - these tests exercise dedupe/delete paths with apply=True")
    return test_db


_SUMMARY = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|deselected|xfailed|xpassed)")


def _parse_counts(output: str) -> dict:
    counts: dict[str, int] = {}
    for m in _SUMMARY.finditer(output):
        key = "error" if m.group(2).startswith("error") else m.group(2)
        counts[key] = counts.get(key, 0) + int(m.group(1))
    return counts


def _run_suite(suite: str, path_root: str) -> str:
    """Run one unit suite against the vetted TEST database, in a subprocess with the right PYTHONPATH.

    suite      - the directory under the repo root that owns a `tests/` package (crawler|orchestrator)
    path_root  - the dir to prepend to PYTHONPATH so the suite's package imports resolve without an
                 install (crawler/ for `import crawler`; orchestrator/src for `import mallory_orchestrator`)
    """
    test_db = _unit_guard()  # raises hc.Skip unless the operator opted in with a separate DB
    suite_dir = os.path.join(_REPO_ROOT, suite)
    if not os.path.isdir(os.path.join(suite_dir, "tests")):
        raise AssertionError(f"suite dir not found: {os.path.join(suite_dir, 'tests')}")

    # Match CI: deselect the documented known-failure ids so "green" means the same thing here.
    deselect: list[str] = []
    kf = os.path.join(_REPO_ROOT, "ci", f"known_failures_{suite}.txt")
    if os.path.isfile(kf):
        with open(kf, encoding="utf-8") as f:
            deselect = [ln.strip() for ln in f if ln.strip()]

    env = os.environ.copy()
    env["DATABASE_URL"] = test_db                 # the ONLY DB these tests may touch
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.path.join(_REPO_ROOT, path_root) + os.pathsep + env.get("PYTHONPATH", "")

    args = [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]
    for did in deselect:
        args += ["--deselect", did]

    timeout = float(hc.cfg("HC_UNIT_TIMEOUT_S", "1200"))
    try:
        proc = subprocess.run(args, cwd=suite_dir, env=env, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"{suite} suite timed out after {timeout:.0f}s")
    out = (proc.stdout or "") + "\n" + (proc.stderr or "")

    if "No module named pytest" in out:
        raise hc.Skip("pytest is not installed in the healthcheck environment")
    counts = _parse_counts(out)
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "no counts parsed"
    n_fail = counts.get("failed", 0) + counts.get("error", 0)
    # pytest exit codes: 0 all-pass, 1 failures, 2 interrupted, 3 internal, 4 usage, 5 none collected.
    if proc.returncode == 5:
        raise AssertionError(f"{suite}: pytest collected NO tests (exit 5) - {summary}")
    if proc.returncode != 0 or n_fail:
        tail = "\n".join(out.strip().splitlines()[-8:])
        raise AssertionError(f"{suite} suite FAILED (exit {proc.returncode}): {summary}\n{tail}")
    return f"{suite} suite passed against HC_TEST_DATABASE_URL - {summary}"


@hc.check(area="logic", name="unit suite: crawler (guarded)")
def unit_suite_crawler() -> str:
    """OPT-IN. Runs crawler/tests against HC_TEST_DATABASE_URL only. SKIPs by default. See the
    module warning: these tests can mutate whatever DATABASE_URL names, so they never touch prod."""
    return _run_suite("crawler", "crawler")


@hc.check(area="logic", name="unit suite: orchestrator (guarded)")
def unit_suite_orchestrator() -> str:
    """OPT-IN. Runs orchestrator/tests against HC_TEST_DATABASE_URL only. SKIPs by default. See the
    module warning: these tests can mutate whatever DATABASE_URL names, so they never touch prod."""
    return _run_suite("orchestrator", os.path.join("orchestrator", "src"))

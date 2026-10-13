#!/usr/bin/env python3
"""Move a crawl job's durable state from one instance to another, so idle instances can work the
backlog instead of watching one instance hold it.

WHY THIS EXISTS. The fleet splits work by domain and each instance keeps its own
`crawl_history.sqlite`, so a job's pending URLs and its already-seen set physically live inside ONE
container. `refill` therefore refuses to move work between instances, and `shard.rebalance` only
moves domains with NO pending frontier — both for the same stated reason: there was no export path,
so moving a domain would restart it from its seed while its real progress sat unreachable next door.

Measured 2026-07-29: instance 0 held 208,435 pending URLs and 493 jobs while four instances sat
completely idle. That is not a scheduling problem, it is a data-locality problem, and it caps fleet
throughput at whatever ONE instance can do against a handful of hosts.

THE EXPORT PATH TURNS OUT TO BE TRIVIAL: all twelve databases live on the same shared volume
(`/app/data` for instance 0, `/app/data/iN` for the rest), so every instance's DB is readable and
writable from any container. No protocol, no dump/restore — SQLite ATTACH and INSERT..SELECT.

WHAT MOVES, and why each part matters:
  * `crawl_frontier`  — the pending URLs. Without these the job has nothing to do.
  * `crawl_pages`     — THE SEEN SET. This is the one that is easy to forget and expensive to get
                        wrong: leave it behind and the target re-fetches every page the source
                        already stored, so the "move" silently doubles the work and the corpus.
                        Keyed on canonical_url, not job_id, so it is selected by host.
  * `crawl_job_state` — pages_done, status and the job JSON itself, so the target can resume it.
  * `host_render` / `host_careful` — learned per-host hints (needs-a-browser, be-gentle). Cheap to
                        carry and re-learning them costs real fetches against a fragile host.

SAFETY. Refuses to run while either instance is crawling — moving a frontier out from under a
running engine races it. Copies first, VERIFIES the row counts landed, and only then deletes from
the source; a failure leaves the job intact where it was. Stop the crawl, move, update
`domain_shards`, resume.

    python move_frontier.py --plan                 # what it would move, nothing written
    python move_frontier.py --apply                # do it, then print the domain_shards updates
    python move_frontier.py --apply --job JOB_ID --to i8    # move one named job
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from collections import defaultdict
from urllib.parse import urlsplit

DATA_ROOT = os.environ.get("CRAWLER_DATA_ROOT", "/app/data")
# instance index -> its data directory. Instance 0 is the volume root; the rest are iN where N is
# the CONTAINER number, which is instance+1 (crawler-api-8-1 is instance 7 and uses i8).
INSTANCES = {0: DATA_ROOT, **{i: f"{DATA_ROOT}/i{i + 1}" for i in range(1, 12)}}

FRONTIER_TABLES = ("crawl_frontier", "crawl_job_state")
HOST_TABLES = ("host_render", "host_careful")


def db_path(inst: int) -> str:
    return os.path.join(INSTANCES[inst], "crawl_history.sqlite")


def connect(inst: int, readonly: bool = False) -> sqlite3.Connection:
    p = db_path(inst)
    if readonly:
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    c = sqlite3.connect(p, timeout=60)
    c.execute("PRAGMA busy_timeout=60000")
    return c


def job_hosts(job_json: str | None) -> set[str]:
    """Hosts this job owns, from its seed URLs. Used to select the seen-set rows, which are keyed by
    URL rather than by job."""
    hosts: set[str] = set()
    try:
        j = json.loads(job_json or "{}")
    except (TypeError, ValueError):
        return hosts
    for u in (j.get("seed_urls") or []):
        h = urlsplit(u).netloc.lower()
        if h:
            hosts.add(h)
            hosts.add(h.removeprefix("www."))
    return hosts


def survey() -> dict[int, dict]:
    out: dict[int, dict] = {}
    for inst in sorted(INSTANCES):
        if not os.path.exists(db_path(inst)):
            continue
        c = connect(inst, readonly=True)
        jobs = {}
        for jid, pending in c.execute(
                "select j.job_id, (select count(*) from crawl_frontier f "
                " where f.job_id = j.job_id and f.state = 'pending') "
                "from crawl_job_state j"):
            jobs[jid] = pending
        out[inst] = {"jobs": jobs, "pending": sum(jobs.values())}
        c.close()
    return out


def plan_moves(state: dict[int, dict], reserve: int = 2000) -> list[tuple[str, int, int]]:
    """Greedy: repeatedly take the biggest movable job off the most-loaded instance and give it to
    the least-loaded, while that strictly improves the imbalance.

    `reserve` keeps a floor of pending URLs on the source so a busy instance is never stripped bare —
    the goal is to fill idle instances, not to shuffle work for its own sake.
    """
    pending = {i: s["pending"] for i, s in state.items()}
    jobs = {i: dict(s["jobs"]) for i, s in state.items()}
    moves: list[tuple[str, int, int]] = []
    for _ in range(500):                       # bounded: this is a helper, not a solver
        src = max(pending, key=lambda i: pending[i])
        dst = min(pending, key=lambda i: pending[i])
        if src == dst or pending[src] - pending[dst] < 500:
            break
        movable = {j: n for j, n in jobs[src].items() if n > 0}
        if not movable or pending[src] - max(movable.values()) < reserve:
            # nothing left we can move without stripping the source below its floor
            movable = {j: n for j, n in movable.items() if pending[src] - n >= reserve}
            if not movable:
                break
        # the largest job that still fits inside half the gap — moving more than that just makes
        # the destination the new worst offender
        gap = (pending[src] - pending[dst]) // 2
        fits = {j: n for j, n in movable.items() if n <= max(gap, 1)}
        jid, n = max((fits or movable).items(), key=lambda kv: kv[1])
        moves.append((jid, src, dst))
        del jobs[src][jid]
        jobs[dst][jid] = n
        pending[src] -= n
        pending[dst] += n
    return moves


STALE_RUNNING_S = int(os.environ.get("MOVE_STALE_RUNNING_S", "600"))


def is_crawling(inst: int) -> tuple[bool, int, int]:
    """(crawling, fresh_rows, stale_rows). A job mid-run must not have its frontier moved.

    `status='running'` alone is NOT the answer: the engine writes that row and does not always get to
    clear it — a crash, a container restart or a wedged teardown leaves it set for ever. Measured
    here: 81 rows on instance 0 and 6 on instance 3 still said `running` with `updated_at` TWENTY
    HOURS old, while all twelve engines reported `running:false`. A guard that trusted the flag would
    block this tool permanently and train whoever runs it to pass --force by reflex, which is worse
    than having no guard.

    A live engine heartbeats `updated_at`, so freshness is the real signal: only rows touched within
    STALE_RUNNING_S count as a running crawl.
    """
    c = connect(inst, readonly=True)
    try:
        rows = c.execute("select updated_at from crawl_job_state where status = 'running'").fetchall()
    finally:
        c.close()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=STALE_RUNNING_S)
    fresh = stale = 0
    for (u,) in rows:
        try:
            ts = dt.datetime.fromisoformat(str(u).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
        except (TypeError, ValueError):
            stale += 1            # unparseable timestamp is not evidence of a live crawl
            continue
        if ts >= cutoff:
            fresh += 1
        else:
            stale += 1
    return fresh > 0, fresh, stale


def move(jid: str, src: int, dst: int, apply: bool) -> dict:
    """Copy one job's durable state to `dst`, verify it landed, then remove it from `src`."""
    s = connect(src)
    row = s.execute("select job_json from crawl_job_state where job_id = ?", (jid,)).fetchone()
    if row is None:
        s.close()
        return {"job": jid, "error": "no such job"}
    hosts = job_hosts(row[0])
    like = [f"%//{h}/%" for h in hosts] + [f"%//{h}" for h in hosts]
    where_pages = " or ".join(["canonical_url like ?"] * len(like)) or "0"

    counts = {
        "frontier": s.execute("select count(*) from crawl_frontier where job_id = ?", (jid,)).fetchone()[0],
        "pages": s.execute(f"select count(*) from crawl_pages where {where_pages}", like).fetchone()[0],
    }
    if not apply:
        s.close()
        return {"job": jid, "from": src, "to": dst, "hosts": sorted(hosts), **counts}

    s.execute("ATTACH DATABASE ? AS tgt", (db_path(dst),))
    try:
        s.execute("BEGIN IMMEDIATE")
        # CLEAR THE TARGET'S OWN ROWS FOR THIS JOB FIRST. A job lives on exactly one instance, so
        # anything already here is a leftover from an earlier life of it — and leaving it turns the
        # verify below into a false negative: the target ends up with MORE rows than the source sent
        # (measured: 2,260 against 981 for cat_all_drdo), the count check fails, and a copy that
        # actually worked is refused. Frontier and job_state are authoritative-from-source;
        # `crawl_pages` deliberately is NOT cleared, because the seen set MERGES — more URLs known to
        # be done is strictly better and dropping the target's own would re-fetch them.
        s.execute("DELETE FROM tgt.crawl_frontier WHERE job_id = ?", (jid,))
        s.execute("INSERT OR REPLACE INTO tgt.crawl_frontier "
                  "SELECT * FROM main.crawl_frontier WHERE job_id = ?", (jid,))
        s.execute("INSERT OR REPLACE INTO tgt.crawl_job_state "
                  "SELECT * FROM main.crawl_job_state WHERE job_id = ?", (jid,))
        s.execute(f"INSERT OR REPLACE INTO tgt.crawl_pages "
                  f"SELECT * FROM main.crawl_pages WHERE {where_pages}", like)
        for t in HOST_TABLES:
            ph = ",".join("?" * len(hosts)) or "''"
            s.execute(f"INSERT OR REPLACE INTO tgt.{t} SELECT * FROM main.{t} "
                      f"WHERE host IN ({ph})", tuple(hosts))
        s.execute("COMMIT")

        # VERIFY BEFORE DELETING. A partial copy that we then delete the source of loses the job.
        got_f = s.execute("select count(*) from tgt.crawl_frontier where job_id = ?", (jid,)).fetchone()[0]
        got_j = s.execute("select count(*) from tgt.crawl_job_state where job_id = ?", (jid,)).fetchone()[0]
        if got_f != counts["frontier"] or got_j != 1:
            return {"job": jid, "error": f"verify failed: frontier {got_f}/{counts['frontier']}, "
                                        f"state {got_j}/1 — source left intact"}

        s.execute("BEGIN IMMEDIATE")
        s.execute("DELETE FROM main.crawl_frontier WHERE job_id = ?", (jid,))
        s.execute("DELETE FROM main.crawl_job_state WHERE job_id = ?", (jid,))
        # crawl_pages is deliberately NOT deleted from the source: it is a per-URL cache, harmless
        # where it sits, and deleting it by host would also drop rows a DIFFERENT job on this
        # instance still relies on for dedup.
        s.execute("COMMIT")
    finally:
        try:
            s.execute("DETACH DATABASE tgt")
        except sqlite3.Error:
            pass
        s.close()
    return {"job": jid, "from": src, "to": dst, "moved": counts, "ok": True}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--job")
    ap.add_argument("--to", type=int, help="destination instance INDEX (0-11)")
    ap.add_argument("--force", action="store_true", help="skip the running-crawl check")
    a = ap.parse_args()

    state = survey()
    print("pending URLs per instance:")
    for i, s in sorted(state.items()):
        print(f"  instance {i:>2}: {s['pending']:>8}  ({len(s['jobs'])} jobs)")

    if a.job:
        if a.to is None:
            sys.exit("--job needs --to")
        moves = [(a.job, next(i for i, s in state.items() if a.job in s["jobs"]), a.to)]
    else:
        moves = plan_moves(state)

    if not moves:
        print("\nalready balanced — nothing to move")
        return

    print(f"\n{len(moves)} move(s):")
    for jid, src, dst in moves:
        print(f"  {jid:44} {src:>2} -> {dst:<2}  ({state[src]['jobs'].get(jid, 0)} pending)")

    if not a.apply:
        print("\nPLAN ONLY — nothing written. Re-run with --apply.")
        return

    busy, stale_total = [], 0
    for i in sorted({m[1] for m in moves} | {m[2] for m in moves}):
        crawling, fresh, stale = is_crawling(i)
        stale_total += stale
        if crawling:
            busy.append((i, fresh))
    if stale_total:
        print(f"\nignoring {stale_total} stale 'running' row(s) — updated_at older than "
              f"{STALE_RUNNING_S}s, and a live engine heartbeats it")
    if busy and not a.force:
        detail = ", ".join(f"instance {i} ({n} live job(s))" for i, n in busy)
        sys.exit(f"\nREFUSING: {detail}. Stop the crawl first — moving a frontier out from under a "
                 f"live engine races it. (--force to override)")

    shard_updates: dict[str, int] = {}
    ok = err = 0
    for jid, src, dst in moves:
        r = move(jid, src, dst, apply=True)
        if r.get("ok"):
            ok += 1
            s = connect(dst, readonly=True)
            jj = s.execute("select job_json from crawl_job_state where job_id = ?", (jid,)).fetchone()
            s.close()
            for h in job_hosts(jj[0] if jj else None):
                shard_updates[h.removeprefix("www.")] = dst
            print(f"  moved {jid} {src}->{dst}: {r['moved']}")
        else:
            err += 1
            print(f"  FAILED {jid}: {r.get('error')}")

    print(f"\n{ok} moved, {err} failed")
    # The orchestrator routes by domain_shards, so without this the next dispatch sends the domain
    # back to the instance that no longer holds its frontier.
    with open("/tmp/shard_updates.json", "w") as fh:
        json.dump(shard_updates, fh)
    print(f"domain_shards updates for {len(shard_updates)} domain(s) -> /tmp/shard_updates.json")

    after = survey()
    print("\npending URLs per instance AFTER:")
    for i, s in sorted(after.items()):
        print(f"  instance {i:>2}: {s['pending']:>8}  ({len(s['jobs'])} jobs)")


if __name__ == "__main__":
    main()

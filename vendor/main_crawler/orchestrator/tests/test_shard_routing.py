"""Routing a site to the wrong crawler instance loses its crawl silently.

The fleet is N containers, one engine each, and a site's durable frontier — its pending URLs, its
already-seen set, its spent page budget — lives inside ONE of them. Send the job somewhere else and
nothing errors: the receiving instance has no saved frontier for that job, so it starts again from
the seed while the real progress sits unreachable in another container's data directory.

Two properties keep that from happening, and both are easy to break by accident:

  stable   the same domain must map to the same instance across processes and restarts.
           `hash()` does NOT — PYTHONHASHSEED is randomised per process.
  sticky   the mapping must survive a change to N. Pure `% N` moves most domains the moment an
           instance is added, and frontiers do not move with them.

Runnable directly:  python tests/test_shard_routing.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from mallory_orchestrator import shard  # noqa: E402
from mallory_orchestrator.models import Base, DomainShard  # noqa: E402


def _db():
    eng = create_engine("sqlite://")            # in-memory, one per test
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def test_www_and_apex_are_the_same_site():
    """They are one server. Splitting them would give one site two instances, two frontiers and
    two politeness budgets — the same bug that had bhel.com taking 2x its configured rate."""
    for a, b in (("https://www.bhel.com/x", "https://bhel.com/y"),
                 ("http://WWW.Bhel.com", "https://bhel.com./z")):
        assert shard.host_of(a) == shard.host_of(b) == "bhel.com", (a, b)
    db = _db()
    assert shard.instance_for(db, "https://www.bhel.com/a", 7) == \
           shard.instance_for(db, "https://bhel.com/b", 7)


def test_the_hash_is_stable_across_processes():
    """THE one that cannot be caught by running twice in the same interpreter: hash() is seeded per
    process, so a same-process check passes while production reshuffles on every restart."""
    code = ("import sys; sys.path.insert(0, r'%s');"
            "from mallory_orchestrator import shard;"
            "print([shard.hash_instance(d, 9) for d in "
            "('bhel.com','drdo.gov.in','saab.com','leonardo.com','cyientdlm.com')])"
            % str(Path(__file__).resolve().parents[1] / "src"))
    runs = []
    for _ in range(3):
        # PYTHONHASHSEED left to vary on purpose — that is the condition being tested.
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr[:400]
        runs.append(out.stdout.strip())
    assert len(set(runs)) == 1, f"the partition moves between processes: {runs}"


def test_growing_the_fleet_does_not_move_an_existing_domain():
    """The reason the assignment is written down. Pure `% N` would move ~6 of 7 domains when N goes
    7 -> 9, and each moved site would restart from its seed."""
    db = _db()
    before = {d: shard.instance_for(db, f"https://{d}/", 7)
              for d in ("bhel.com", "drdo.gov.in", "saab.com", "leonardo.com", "chemring.com")}
    after = {d: shard.instance_for(db, f"https://{d}/", 9) for d in before}
    assert before == after, f"domains moved when the fleet grew: {before} -> {after}"
    # ...and a NEW domain is free to use the wider fleet.
    fresh = {shard.instance_for(db, f"https://new{i}.com/", 9) for i in range(40)}
    assert max(fresh) > 6, "new domains never reach the instances that were just added"


def test_a_single_instance_routes_everything_to_zero():
    """The pre-fleet deployment must behave exactly as it did before any of this existed."""
    db = _db()
    for d in ("bhel.com", "saab.com", "x.co.uk"):
        assert shard.instance_for(db, f"https://{d}/", 1) == 0
    assert db.query(DomainShard).count() == 0, "a one-instance fleet should not write assignments"


def test_an_unroutable_url_still_goes_somewhere():
    """A job we cannot parse a host from must not raise mid-dispatch and kill the batch."""
    db = _db()
    for bad in ("", "not a url", "file:///etc/passwd"):
        assert shard.instance_for(db, bad, 7) == 0


def test_route_buckets_a_job_list_without_losing_or_duplicating_any():
    db = _db()
    jobs = [{"job_id": f"j{i}", "seed_urls": [f"https://site{i}.com/"]} for i in range(50)]
    buckets = shard.route(db, jobs, 7)
    flat = [j["job_id"] for v in buckets.values() for j in v]
    assert sorted(flat) == sorted(j["job_id"] for j in jobs), "route lost or duplicated a job"
    assert len(buckets) > 1, "50 domains landed on one instance — the split is not spreading"


def test_shrinking_the_fleet_names_what_it_would_strand():
    """Frontiers cannot move between containers, so shrinking orphans them. Being told which
    domains beats discovering it as sites quietly re-crawling from scratch."""
    db = _db()
    for i in range(40):
        shard.instance_for(db, f"https://s{i}.com/", 9)
    orphans = shard.stranded(db, 4)
    assert orphans, "shrinking 9 -> 4 stranded nothing, which cannot be right"
    for d in orphans:
        assert db.get(DomainShard, d).instance >= 4


def test_rebalance_moves_only_what_is_safe_to_move():
    """Growing the fleet is useless on its own — every domain already has an owner, so new
    instances receive nothing for ever. Rebalancing is the other half, and it must move ONLY
    domains with no pending frontier: a domain mid-crawl keeps its progress inside its current
    container, so moving it restarts the site from its seed while the real frontier sits
    unreachable next door."""
    db = _db()
    for i in range(60):
        shard.instance_for(db, f"https://s{i}.com/", 7)
    busy = {f"s{i}.com" for i in range(0, 60, 3)}          # 20 domains mid-crawl
    before = {r.domain: r.instance for r in db.query(DomainShard).all()}
    out = shard.rebalance(db, 12, busy)
    after = {r.domain: r.instance for r in db.query(DomainShard).all()}

    for d in busy:
        assert after[d] == before[d], f"{d} was mid-crawl and got moved — its frontier is stranded"
    assert out["held_busy"] == len(busy), out
    assert out["moved"] > 0, "nothing moved, so the new instances stay empty"
    # ...and the freed domains actually reach the instances that were just added.
    assert max(after[d] for d in after if d not in busy) > 6,         "rebalance never used the new instances"


def test_rebalance_is_idempotent():
    """Running it twice must not churn assignments — a second run has nothing left to move."""
    db = _db()
    for i in range(40):
        shard.instance_for(db, f"https://t{i}.com/", 5)
    shard.rebalance(db, 9, set())
    second = shard.rebalance(db, 9, set())
    assert second["moved"] == 0, f"rebalance is not stable: moved {second['moved']} on a repeat run"


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} SHARD-ROUTING TESTS PASSED")


if __name__ == "__main__":
    main()

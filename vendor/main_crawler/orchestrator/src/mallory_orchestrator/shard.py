"""Which crawler instance owns a site.

The fleet is N crawler containers, one engine process each. A site must always land on the SAME
instance, because that instance's `crawl_history.sqlite` holds its durable frontier — the pending
URLs, the already-seen set and the spent page budget. Send it somewhere else and the crawl silently
restarts from the seed while the real frontier sits unreachable in another container's data
directory.

Two rules make that hold.

**Hash, never `hash()`.** PYTHONHASHSEED is randomised per process, so `hash("bhel.com") % 8` gives
a different answer after every restart. blake2b is stable across processes, restarts and machines.

**Assign once, never reassign.** Hashing alone is only stable while N is. The moment an instance is
added, `% N` moves most domains — and their frontiers do not move with them, because the frontier
lives inside a container with no export path. So the first time a domain is seen its instance is
written down, and from then on the table is the answer. Growth then costs nothing: existing domains
stay put, new ones fill the new instances. Shrinking is the operation that needs care, and
`stranded()` names exactly which domains it would orphan instead of letting them go quiet.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DomainShard


def host_of(url: str) -> str:
    """The routing key: lowercase host, `www.` stripped.

    Identical to `orchestrate._seed_host` and to the crawler's own `canonicalize.registered_domain`,
    so the three cannot disagree about which site a URL belongs to. www and apex MUST agree — they
    are one server, and splitting them would put one site's pages on two instances with two
    frontiers and two politeness budgets.
    """
    return (urlsplit(str(url)).hostname or "").lower().rstrip(".").removeprefix("www.")


def _reserved(reserved: int | Iterable[int] | None = None) -> set[int]:
    """The instances the daily lane runs on. Read from settings when the caller does not pass any,
    so every existing call site honours the lane without being edited.

    Accepts an int for backwards compatibility: callers written against the single-instance lane
    pass `reserved=6` and still mean {6}. -1 keeps meaning "none".
    """
    if reserved is None:
        from .config import daily_instance_list
        return set(daily_instance_list())
    if isinstance(reserved, int):
        return {reserved} if reserved >= 0 else set()
    return {int(i) for i in reserved if int(i) >= 0}


def _exclusive() -> bool:
    from .config import daily_exclusive_enabled
    return daily_exclusive_enabled()


def allowed_instances(n: int, reserved: int | Iterable[int] | None = None) -> list[int]:
    """The instances the GENERAL fleet may assign a domain to.

    Only an EXCLUSIVE lane is carved out here. In priority mode the lane's instances still own
    catalogue domains and work them between daily sweeps — holding them back is the dispatcher's
    job (see orchestrate._dispatch_routed), not the shard table's. Excluding them here instead
    would be wrong in a way that is hard to see later: assign-once means a domain kept off these
    instances never comes back, so flipping exclusive off would not return the work, it would only
    stop new domains from leaving.

    Falls back to the whole fleet when the carve-out would leave nothing — a deployment that
    reserves every instance it has must share them, and sharing is strictly better than routing
    catalogue work into a void. `daily.reservation()` reports that as shared:true.
    """
    if not _exclusive():
        return list(range(n))
    r = _reserved(reserved)
    idx = [i for i in range(n) if i not in r]
    return idx or list(range(n))


def hash_instance(domain: str, n: int, reserved: int | Iterable[int] | None = None) -> int:
    """Stable fallback for a domain nobody has assigned yet. Never returns a daily-lane instance
    while the lane is EXCLUSIVE; in priority mode the lane's instances take their share like any
    other, because they work the catalogue between sweeps."""
    if n <= 1:
        return 0
    allowed = allowed_instances(n, reserved)
    d = hashlib.blake2b(domain.encode("utf-8", "ignore"), digest_size=8).digest()
    return allowed[int.from_bytes(d, "big") % len(allowed)]


def instance_for(db: Session, url: str, n: int,
                 reserved: int | Iterable[int] | None = None) -> int:
    """The instance that owns `url`, assigning it on first sight.

    Returns 0 for an unusable URL rather than raising: a job we cannot route still has to go
    somewhere, and instance 0 is the one that has always existed.
    """
    domain = host_of(url)
    if not domain or n <= 1:
        return 0
    row = db.get(DomainShard, domain)
    if row is not None:
        # Clamp rather than re-assign. If the fleet shrank below a recorded index the frontier is
        # already stranded (see stranded()); sending the job to a live instance at least keeps the
        # crawl running instead of posting into a void.
        return row.instance if row.instance < n else row.instance % n
    idx = hash_instance(domain, n, reserved)
    db.add(DomainShard(domain=domain, instance=idx))
    db.commit()
    return idx


def owner_of(db: Session, host: str, n: int) -> int | None:
    """Which instance ALREADY owns `host`, or None if nothing has been assigned.

    Deliberately read-only, unlike instance_for(): this answers a dashboard drill-down, and a panel
    that someone opened to LOOK at something must not write a routing decision as a side effect —
    a domain assigned by a click would then be crawled there forever.

    None means "do not guess". The caller falls back to asking every instance, which is correct if
    slow; guessing a hash for an unassigned domain would confidently query the wrong container and
    render "no data for this site" for a site with plenty.
    """
    domain = host_of(host) or host_of("https://" + str(host or ""))
    if not domain or n <= 1:
        return 0 if domain else None
    row = db.get(DomainShard, domain)
    if row is None:
        return None
    return row.instance if row.instance < n else row.instance % n


def route(db: Session, jobs: list[dict], n: int,
          reserved: int | Iterable[int] | None = None) -> dict[int, list[dict]]:
    """Bucket a job list by owning instance. Buckets with no jobs are omitted."""
    out: dict[int, list[dict]] = {}
    for j in jobs:
        seeds = j.get("seed_urls") or []
        out.setdefault(instance_for(db, seeds[0] if seeds else "", n, reserved), []).append(j)
    return out


def rebalance(db: Session, n: int, busy: set[str],
              reserved: int | Iterable[int] | None = None) -> dict:
    """Spread FINISHED domains over a fleet that just grew. Nothing else moves.

    Assign-once is what makes growth safe, and it is also what makes growth useless on its own: add
    three instances to a catalogue that is already fully assigned and they receive nothing, for
    ever, because every domain already has an owner. This is the other half — after adding
    instances, re-hash the domains that have nothing to lose.

    `busy` is every domain with pending frontier rows somewhere in the fleet. Those keep their
    owner: their saved progress physically lives in that container, and moving them would restart
    the site from its seed while the real frontier sat unreachable. A domain that has finished has
    no frontier to strand, so re-hashing it costs nothing and lets the next crawl of that site land
    on a new instance.

    Returns what moved and what was held, because "rebalanced" with no numbers is indistinguishable
    from "did nothing".
    """
    moved, held = 0, 0
    for row in db.scalars(select(DomainShard)).all():
        if row.domain in busy:
            held += 1
            continue
        idx = hash_instance(row.domain, n, reserved)
        if idx != row.instance:
            row.instance = idx
            moved += 1
    db.commit()
    return {"instances": n, "moved": moved, "held_busy": held,
            "total": moved + held + db.query(DomainShard).count() - (moved + held)}


def stranded(db: Session, n: int) -> list[str]:
    """Domains assigned to an instance that no longer exists.

    Shrinking the fleet does not move frontiers, so this is the list of sites whose saved progress
    would be unreachable. Named, not silently re-hashed: re-hashing makes them quietly re-crawl from
    the seed, which looks like the crawler forgetting its work.
    """
    return [d for (d,) in db.execute(
        select(DomainShard.domain).where(DomainShard.instance >= n)).all()]


def reserved_conflicts(db: Session, reserved: int | Iterable[int] | None = None) -> list[str]:
    """Catalogue domains still routed to the reserved instance.

    Reserving an instance changes where NEW domains go; it deliberately does not move domains
    already assigned there, because their frontier physically lives in that container and moving it
    restarts the site from its seed (the same reason assign-once exists at all). So the reservation
    is only fully in force once these have finished and been rebalanced. Named rather than
    silently re-hashed, for exactly the reason stranded() is.
    """
    r = _reserved(reserved)
    if not r or not _exclusive():
        # In priority mode these are not conflicts: the lane's instances are SUPPOSED to own
        # catalogue domains and work them between sweeps. Reporting them would turn the normal
        # steady state into a permanent warning.
        return []
    return [d for (d,) in db.execute(
        select(DomainShard.domain).where(DomainShard.instance.in_(sorted(r)))).all()]

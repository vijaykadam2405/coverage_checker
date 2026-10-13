#!/usr/bin/env python3
"""Recompute crawl depth as MIN HOPS FROM THE SEED, from the link graph we already stored.

WHY THIS EXISTS. `depth` is meant to be "hops from the seed", and every rule that reads it assumes
that. Under the production LIFO frontier (CRAWLER_FRONTIER=dfs) the first sighting of a URL records
the depth of a DESCENT instead, and `_enqueue_candidate` makes first sight final — so the number is
a fact about the walk, not about the site. MEASURED on al-monitor.com: 81,297 pages over 1,820
distinct depths, max 1,826, all real articles, while that site's own /archives index sits at d1 and
every monthly page under it at d2. Those articles are three hops from the seed.

The engine now relaxes depth downward whenever a URL is re-sighted by a shorter path
(async_engine._enqueue_candidate -> CrawlHistory.frontier_relax). That fixes every crawl from now
on, and it is INERT on a frontier that is already poisoned: relaxation needs a shallow hub to be
re-parsed, and the hubs were fetched long ago and are marked `done`. Measured after deploying the
live fix: seven minutes of crawling moved zero depth bands.

So this repairs the existing data instead, and it needs no network and no sitemap. The engine stored
the HTML of every page it fetched. That HTML *is* the link graph. Re-extract it with the crawler's
own parser, run a real BFS from the seeds, and the true min-hop depth falls out.

WHAT IT WRITES
  * crawl_frontier.depth for PENDING rows, downward only. These are the rows that still steer the
    crawl, so this is where a wrong depth still does damage.
  * documents.depth ONLY with --documents. Off by default: a stored row's depth is provenance, it
    records where the crawler actually was. Repairing it makes the dashboard legible; that is a
    judgement call for the operator, not a default.

WHY NOT the frontier's own parent_url: it stores only the FIRST-DISCOVERY edge, which is exactly the
DFS tree, so min-hop over it just returns the chain depth again. The shallow edges were never
written. Stored HTML is the only place the real graph survives.

    python repair_depths.py --source al-monitor.com                  # plan, writes nothing
    python repair_depths.py --source al-monitor.com --apply
    python repair_depths.py --source al-monitor.com --apply --documents
"""
from __future__ import annotations

import argparse
import collections
import os
import sqlite3
import sys

sys.path.insert(0, "/app")

from crawler import parse                                    # noqa: E402
from crawler.canonicalize import canonicalize_url            # noqa: E402

DATA_ROOT = os.environ.get("CRAWLER_DATA_ROOT", "/app/data")
INSTANCES = {0: DATA_ROOT, **{i: "%s/i%d" % (DATA_ROOT, i + 1) for i in range(1, 12)}}


def _pg():
    import psycopg
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = "postgresql://" + url.split("://", 1)[1]
    return psycopg.connect(url)


def bfs(source: str, conn, seeds: list[str]) -> dict[str, int]:
    """True min-hop depth, expanding LEVEL BY LEVEL and pulling HTML only for the level being
    expanded.

    The first version loaded every stored page's HTML up front and parsed it all before starting.
    That is the obvious shape and the wrong one: it spent two hours on al-monitor's 80k pages (~8 GB
    of HTML through BeautifulSoup) before it could report anything, and most of those pages are
    leaf articles whose outlinks cannot shorten anyone's path.

    A real BFS never needs the whole graph in advance. Parse a level, learn the next level, stop as
    soon as a level discovers nothing new — which for a site whose true diameter is single digits
    means a fraction of the corpus is ever parsed. Same answer, because the first time BFS reaches a
    node IS its shortest distance; that is the invariant the LIFO frontier broke, restored offline.

    Uses the crawler's own extract_links + canonicalize_url deliberately: a second, hand-rolled link
    parser would quietly disagree and produce depths the crawler itself could never reproduce.
    """
    dist = {s: 0 for s in seeds}
    frontier = list(seeds)
    level = 0
    parsed = 0
    while frontier:
        level += 1
        nxt: list[str] = []
        # Pull this level's HTML in one round trip per chunk rather than one query per URL.
        for i in range(0, len(frontier), 500):
            chunk = frontier[i:i + 500]
            with conn.cursor() as cur:
                cur.execute("SELECT url, html FROM documents "
                            "WHERE source_id = %s AND url = ANY(%s) AND html IS NOT NULL",
                            (source, chunk))
                for url, html in cur.fetchall():
                    parsed += 1
                    try:
                        links = parse.extract_links(html, url)
                    except Exception:
                        continue
                    for l in links:
                        try:
                            c = canonicalize_url(l, url)
                        except Exception:
                            continue
                        if c not in dist:
                            dist[c] = level
                            nxt.append(c)
        print("  level %-3d: expanded %d page(s), %d url(s) newly reached (total %d, parsed %d)"
              % (level, len(frontier), len(nxt), len(dist), parsed), flush=True)
        if not nxt:
            break
        frontier = nxt
    return dist


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--documents", action="store_true",
                    help="also repair documents.depth (provenance — opt in deliberately)")
    a = ap.parse_args()

    conn = _pg()

    # Seeds: the shallowest things we hold. Depth 0/1 rows are the pages the operator pointed at and
    # their top nav — the one part of the recorded depth a DFS walk cannot have got wrong, because
    # nothing is nearer than the seed.
    with conn.cursor() as cur:
        cur.execute("SELECT url FROM documents WHERE source_id = %s AND depth <= 1", (a.source,))
        seeds = sorted({canonicalize_url(r[0]) for r in cur.fetchall()})
    print("%s: %d seed/nav page(s) at depth<=1" % (a.source, len(seeds)))
    if not seeds:
        sys.exit("no depth<=1 pages stored — cannot anchor a BFS without inventing a root")

    print("walking the stored link graph level by level…")
    dist = bfs(a.source, conn, seeds)
    if dist:
        hist = collections.Counter(dist.values())
        print("  BFS reached %d urls; true depth 0..%d" % (len(dist), max(dist.values())))
        print("  " + ", ".join("d%d=%d" % (d, hist[d]) for d in sorted(hist)[:12]))

    # ---- frontier (pending rows steer the crawl; this is where a wrong depth still does harm)
    total_rows = total_fix = 0
    for inst, root in INSTANCES.items():
        p = os.path.join(root, "crawl_history.sqlite")
        if not os.path.exists(p):
            continue
        c = sqlite3.connect(p, timeout=60)
        rows = c.execute(
            "SELECT job_id, url, depth FROM crawl_frontier WHERE state='pending'").fetchall()
        fix = []
        for jid, url, d in rows:
            nd = dist.get(canonicalize_url(url))
            if nd is not None and d is not None and nd < d:
                fix.append((nd, jid, url, nd))
        total_rows += len(rows)
        total_fix += len(fix)
        if fix:
            print("  instance %-2d: %d pending row(s) to relax (max %d -> %d)"
                  % (inst, len(fix), max(f[0] for f in fix), min(f[0] for f in fix)))
            if a.apply:
                c.executemany("UPDATE crawl_frontier SET depth=? "
                              "WHERE job_id=? AND url=? AND state='pending' AND depth > ?", fix)
                c.commit()
        c.close()
    print("frontier: %d pending row(s) scanned, %d relaxable" % (total_rows, total_fix))

    # ---- documents (provenance; opt-in)
    with conn.cursor() as cur:
        cur.execute("SELECT url, depth FROM documents WHERE source_id = %s", (a.source,))
        docs = cur.fetchall()
    dfix = [(dist[canonicalize_url(u)], u) for u, d in docs
            if d is not None and dist.get(canonicalize_url(u)) is not None
            and dist[canonicalize_url(u)] < d]
    print("documents: %d row(s), %d with an over-stated depth" % (len(docs), len(dfix)))
    if dfix:
        print("  worst: %s" % ", ".join(
            "%s -> d%d" % (u.rsplit("/", 1)[-1][:28], nd)
            for nd, u in sorted(dfix, key=lambda x: -x[0])[:3]))
    if a.documents and a.apply and dfix:
        with conn.cursor() as cur:
            cur.executemany("UPDATE documents SET depth = %s WHERE url = %s AND source_id = %s",
                            [(nd, u, a.source) for nd, u in dfix])
        conn.commit()
        print("  documents.depth repaired")
    elif dfix and not a.documents:
        print("  (pass --documents to repair those too)")

    if not a.apply:
        print("\nPLAN ONLY — nothing written. Re-run with --apply.")
    conn.close()


if __name__ == "__main__":
    main()

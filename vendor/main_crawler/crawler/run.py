"""Crawler CLI.

  python run.py testing                 # run the §8 testing batch + exit criteria (offline)
  python run.py gen                      # generate jobs from the seed -> jobs/generated_jobs.json
  python run.py run jobs/foo.json        # run a job file end-to-end (in-process ingest)
  python run.py push jobs/foo.json       # run jobs and POST pages to the Ingest API (INGEST_BASE_URL)
  python run.py serve                    # serve the stub Ingest API + dashboard on :9090
  python run.py crawler-api              # serve the Crawler API (job in -> records out) on :8099

Set CRAWLER_ALLOW_NETWORK=1 to allow live fetching (fixtures still take priority
unless CRAWLER_PREFER_FIXTURES=0).
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from crawler import config


def _summary(results) -> None:
    tot = Counter()
    for r in results:
        s = r.get("summary", {}) if isinstance(r, dict) else r
        tot["fetched"] += s.get("fetched", 0) if isinstance(s, dict) else s.fetched
        tot["kept"] += s.get("kept", 0) if isinstance(s, dict) else s.kept
        tot["dropped"] += s.get("dropped_by_gate", 0) if isinstance(s, dict) else s.dropped_by_gate
        tot["304"] += s.get("not_modified_304", 0) if isinstance(s, dict) else s.not_modified
        tot["skipped_unchanged"] += s.get("skipped_unchanged", 0) if isinstance(s, dict) else s.skipped_unchanged
        tot["skipped_duplicate"] += s.get("skipped_duplicate", 0) if isinstance(s, dict) else s.skipped_duplicate
        tot["accepted"] += s.get("accepted", 0) if isinstance(s, dict) else s.accepted
        tot["rejected"] += s.get("rejected", 0) if isinstance(s, dict) else s.rejected
    print("\n=== BATCH SUMMARY ===")
    for k in ("fetched", "kept", "dropped", "304", "skipped_unchanged",
              "skipped_duplicate", "accepted", "rejected"):
        print(f"  {k:18} {tot[k]}")


def cmd_testing() -> int:
    import run_testing_batch
    return run_testing_batch.main()


def cmd_gen() -> int:
    from crawler.jobgen import distinct_sites, generate, write_jobs
    jobs = generate()
    out = Path("jobs/generated_jobs.json")
    out.parent.mkdir(exist_ok=True)
    write_jobs(jobs, out)
    by = Counter(j.job_type for j in jobs)
    print(f"Generated {len(jobs)} jobs across {len({u for j in jobs for u in j.seed_urls})} "
          f"target URLs / {distinct_sites(jobs)} distinct hosts.")
    print(f"  by type: {dict(by)}")
    print(f"  written -> {out}")
    return 0


def _dispatch(jobs, *, forward: bool):
    """Route each job to its tier: archive_date => C2 (archive, no browser), else the live pool.
    Shared by run/push so both stay honest about which tier ran."""
    from crawler.async_engine import run_batch_async
    c2_jobs = [j for j in jobs if j.is_c2()]
    live_jobs = [j for j in jobs if not j.is_c2()]
    out = []
    if c2_jobs:
        from crawler.c2 import run_c2_batch
        from crawler.keywords import get_corpus
        from crawler.seed import load_seed
        print(f"  C2 archive tier: {len(c2_jobs)} job(s) — no browser, rate-limited to archive.org")
        # kp must be the real corpus, not None: gate.evaluate treats kp=None as "no corpus ->
        # keep every page", which would silently disable the keyword gate for a keyword=True job.
        out += run_c2_batch(c2_jobs, forward=forward, seed=load_seed(), kp=get_corpus())
    if live_jobs:
        out += run_batch_async(live_jobs, forward=forward)
    pos = {j.job_id: i for i, j in enumerate(jobs)}
    out.sort(key=lambda r: pos.get(r.get("job_id"), len(pos)))
    return out


def cmd_run(path: str) -> int:
    from crawler.models import Job
    raw = json.loads(Path(path).read_text())
    jobs = [Job(**j) for j in raw]
    print(f"Running {len(jobs)} jobs from {path} "
          f"(network={'on' if config.allow_network() else 'off'}, "
          f"fixtures={'on' if config.prefer_fixtures() else 'off'})")
    results = _dispatch(jobs, forward=False)
    _summary(results)
    print(f"  accepted records -> {config.OUTPUT_DIR / 'ingested.ndjson'}")
    return 0


def cmd_push(path: str) -> int:
    """Run jobs and POST every kept page to the Ingest API over HTTP (Postgres + MinIO).

    Target comes from INGEST_BASE_URL (default http://127.0.0.1:9090, the ingest service).
    """
    from crawler.models import Job
    raw = json.loads(Path(path).read_text())
    jobs = [Job(**j) for j in raw]
    print(f"Pushing {len(jobs)} jobs from {path} -> {config.INGEST_BASE_URL} "
          f"(network={'on' if config.allow_network() else 'off'}, "
          f"fixtures={'on' if config.prefer_fixtures() else 'off'})")
    results = _dispatch(jobs, forward=True)

    tot = Counter()
    reasons: Counter = Counter()
    for r in results:
        s = r.get("summary", {})
        for k in ("fetched", "kept", "dropped_by_gate", "not_modified_304",
                  "skipped_unchanged", "skipped_duplicate", "sent", "accepted",
                  "rejected", "errors"):
            tot[k] += s.get(k, 0)
        reasons.update(s.get("gate_reasons", {}))
    print("\n=== PUSH SUMMARY ===")
    for k, v in tot.items():
        print(f"  {k:18} {v}")
    if reasons:
        print(f"  gate reasons       {dict(reasons)}")
    return 0 if tot.get("errors", 0) == 0 else 1


def cmd_serve() -> int:
    import uvicorn
    uvicorn.run("ingest_api.app:app", host="0.0.0.0", port=9090, log_level="info")
    return 0


def cmd_crawler_api() -> int:
    import logging
    import os
    import uvicorn
    # Give the engine loggers a root handler. Without this, uvicorn only wires its own loggers, so
    # async_engine's log.info (fetch_fail, cooldown, adaptive) hit Python's WARNING-only last-resort
    # handler and vanish — the batch looks silent. CRAWLER_LOG_LEVEL=WARNING to quiet it back down.
    logging.basicConfig(level=os.environ.get("CRAWLER_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = int(os.environ.get("CRAWLER_API_PORT", "8099"))
    uvicorn.run("crawler_api.app:app", host="0.0.0.0", port=port, log_level="info")
    return 0


def cmd_c3_testbed() -> int:
    """Self-hosted bot-detection lab for hardening the C3 stealth browser (see c3_testbed/app.py)."""
    import os
    import uvicorn
    port = int(os.environ.get("C3_TESTBED_PORT", "9085"))
    uvicorn.run("c3_testbed.app:app", host="0.0.0.0", port=port, log_level="warning")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, *rest = argv
    if cmd == "testing":
        return cmd_testing()
    if cmd == "gen":
        return cmd_gen()
    if cmd == "run":
        if not rest:
            print("usage: python run.py run <jobs.json>")
            return 2
        return cmd_run(rest[0])
    if cmd == "push":
        if not rest:
            print("usage: INGEST_BASE_URL=http://127.0.0.1:9090 python run.py push <jobs.json>")
            return 2
        return cmd_push(rest[0])
    if cmd == "serve":
        return cmd_serve()
    if cmd == "crawler-api":
        return cmd_crawler_api()
    if cmd == "c3-testbed":
        return cmd_c3_testbed()
    print(f"unknown command: {cmd}\n{__doc__}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

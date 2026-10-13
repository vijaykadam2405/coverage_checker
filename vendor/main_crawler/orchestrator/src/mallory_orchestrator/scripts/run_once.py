"""Generate + dispatch jobs once. Use --test for the offline fixture batch (crawler → L2)."""

from __future__ import annotations

import sys

from ..db import SessionLocal
from ..orchestrate import build_jobs, build_test_jobs, run_batch


def main() -> None:
    test = "--test" in sys.argv
    with SessionLocal() as db:
        jobs = build_test_jobs() if test else build_jobs(db, only_due=True)
        print(f"{'[test] ' if test else ''}dispatching {len(jobs)} job(s)…")
        result = run_batch(db, jobs)
    print(result)


if __name__ == "__main__":
    main()

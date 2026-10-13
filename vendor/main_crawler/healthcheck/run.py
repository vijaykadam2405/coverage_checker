"""Daily health-check runner.

Usage:
    python -m healthcheck.run                # run everything, print report, exit 0/1
    python -m healthcheck.run tiers coverage # run only these areas
    HC_JSON_OUT=/var/log/mallory/hc.json python -m healthcheck.run

Exit code: 0 iff no check FAILed (SKIP is not a failure). A single non-zero exit is
the signal a daily cron/systemd timer alerts on. The per-area report tells the
operator WHERE it broke without reading logs.

Import order matters only in that importing a check module registers its checks; the
modules are otherwise independent. A module that fails to import (missing dep on this
host) is reported as a load error, never a silent gap.
"""
from __future__ import annotations

import importlib
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone

from . import hc

CHECK_MODULES = [
    "services",     # liveness: containers, health endpoints, Postgres, MinIO
    "tiers",        # C1/C2/C3/C4 fetch tiers actually work + are being used
    "coverage",     # sites are being crawled properly (freshness, depth, no 1-page sites)
    "delta",        # push/verify delta + sum pages == total reconciliation
    "dashboards",   # every dashboard endpoint serves real, self-consistent data
    "logic",        # control-plane logic + the unit suites folded in
]

_C = {"PASS": "\033[32m", "FAIL": "\033[31m", "SKIP": "\033[33m", "": "\033[0m"}


def _color(s: str, status: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"{_C.get(status,'')}{s}{_C['']}"


def main(argv: list[str]) -> int:
    areas = [a for a in argv[1:] if not a.startswith("-")]
    load_errors: list[tuple[str, str]] = []
    for mod in CHECK_MODULES:
        try:
            importlib.import_module(f"{__package__}.checks.{mod}")
        except Exception as e:  # noqa: BLE001
            load_errors.append((mod, f"{type(e).__name__}: {e}"))

    started = datetime.now(timezone.utc)
    results: list[hc.Result] = []
    if areas:
        for a in areas:
            results.extend(hc.run_all(area_filter=a))
    else:
        results = hc.run_all()

    # -- report, grouped by area ------------------------------------------------
    by_area: dict[str, list[hc.Result]] = {}
    for r in results:
        by_area.setdefault(r.area, []).append(r)

    print(f"\nMallory crawler health check - {started.isoformat(timespec='seconds')} "
          f"on {socket.gethostname()}")
    print(f"  ingest={hc.INGEST_BASE}  orch={hc.ORCH_BASE}  crawler-api={hc.CRAWLER_API_BASE}")
    print("=" * 78)

    n_pass = n_fail = n_skip = 0
    for area in sorted(by_area):
        print(f"\n[{area}]")
        for r in by_area[area]:
            if r.status == "PASS":
                n_pass += 1
            elif r.status == "SKIP":
                n_skip += 1
            else:
                n_fail += 1
            tag = _color(f"{r.status:4}", r.status)
            crit = " *CRIT*" if (r.critical and r.status == "FAIL") else ""
            print(f"  {tag} {r.name:<38} {r.seconds:5.1f}s  {r.detail}{crit}")

    if load_errors:
        print("\n[load-errors]  (a check module could not be imported on this host)")
        for mod, err in load_errors:
            print(f"  {_color('FAIL','FAIL')} checks.{mod:<30} {err}")
        n_fail += len(load_errors)

    print("\n" + "=" * 78)
    verdict = "HEALTHY" if n_fail == 0 else "UNHEALTHY"
    print(f"{_color(verdict, 'PASS' if n_fail == 0 else 'FAIL')}: "
          f"{n_pass} passed, {n_fail} failed, {n_skip} skipped "
          f"in {sum(r.seconds for r in results):.1f}s")

    # -- machine-readable log for trending / alerting ---------------------------
    out = os.environ.get("HC_JSON_OUT", "").strip()
    if out:
        doc = {
            "started": started.isoformat(),
            "host": socket.gethostname(),
            "verdict": verdict,
            "counts": {"pass": n_pass, "fail": n_fail, "skip": n_skip},
            "results": [vars(r) for r in results],
            "load_errors": [{"module": m, "error": e} for m, e in load_errors],
        }
        try:
            os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2)
            print(f"wrote {out}")
        except OSError as e:
            print(f"(could not write {out}: {e})")

    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

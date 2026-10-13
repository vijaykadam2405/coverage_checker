"""python -m sipri_harvester [transfers|datasets|all]

Sequential by design. SIPRI's server is slow and we are a guest on it: the sections share one
process-wide rate limiter (fetcher._throttle), so running them concurrently would only double the
request rate against a host that is already the bottleneck. Wall-clock is not the constraint here —
the whole arms-transfer database is ~15 requests.
"""
from __future__ import annotations

import json
import logging
import sys

from . import config, datasets, reference, store, transfers

log = logging.getLogger("sipri")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    argv = list(sys.argv[1:] if argv is None else argv)
    what = (argv[0] if argv else "all").lower()

    if not store.ensure_schema():
        # Not fatal: the harvest still runs and reports what it fetched. But say so loudly — a run
        # that fetches 30k records into nowhere must never read as success.
        log.error("could not prepare schema %s / bucket %s — results will NOT be persisted",
                  config.PG_SCHEMA, config.MINIO_BUCKET)

    results: list[dict] = []

    if what in ("all", "transfers"):
        log.info("arms transfers: pulling via %s%s", config.API_BASE, config.TRADES_SEARCH)
        results.append(transfers.harvest(store))

    if what in ("all", "reference"):
        results.append(reference.harvest_countries(store))
        results.append(reference.harvest_armaments(store))

    if what in ("all", "pages"):
        results.extend(reference.harvest_pages(store))

    if what in ("all", "datasets"):
        for entry in discover_entries():
            log.info("dataset %s: %s", entry["key"], entry["url"])
            results.append(datasets.harvest_file_dataset(entry, store))

    print(json.dumps({"results": results, "stats": store.stats()}, indent=2, default=str))

    # Exit non-zero when anything is incomplete, so a scheduler sees a failed run rather than a
    # green tick over a half-empty table.
    bad = [r for r in results
           if r.get("status") == "failed" or r.get("complete") is False]
    return 1 if bad else 0


def discover_entries() -> list[dict]:
    """Re-resolve dataset URLs before downloading: SIPRI's filenames carry a year/version and change
    at the annual update, so the registry's URLs rot."""
    try:
        from . import discover
        return discover.refresh_registry()
    except Exception as exc:                      # noqa: BLE001 — fall back to the pinned URLs
        log.warning("URL re-discovery failed (%s) — using the registry as-is", exc)
        return list(config.FILE_DATASETS)


if __name__ == "__main__":
    raise SystemExit(main())

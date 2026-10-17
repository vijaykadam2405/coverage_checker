from __future__ import annotations

from crawler_coverage_auditor.analysis.coverage import is_inventory_url


def _found(row: dict) -> bool:
    return row.get("match_status") not in {None, "NOT_FOUND", "REVIEW_REQUIRED"}


def compare_runs(baseline: list[dict], current: list[dict]) -> tuple[dict[str, int], list[dict]]:
    """Compare Corpus URL coverage between two completed audit inventories."""
    before = {row["normalized_url"]: row for row in baseline if is_inventory_url(row)}
    after = {row["normalized_url"]: row for row in current if is_inventory_url(row)}
    changes: list[dict] = []
    for url in sorted(set(before) | set(after)):
        old, new = before.get(url), after.get(url)
        if old and new and not _found(old) and _found(new):
            changes.append({"url": url, "change": "RECOVERED"})
        elif old and new and _found(old) and not _found(new):
            changes.append({"url": url, "change": "NEWLY_MISSING"})
        elif not old and new:
            changes.append({"url": url, "change": "NEW_EXPECTED_URL"})
    recovered = sum(row["change"] == "RECOVERED" for row in changes)
    newly_missing = sum(row["change"] == "NEWLY_MISSING" for row in changes)
    return {"recovered_urls": recovered, "newly_missing_urls": newly_missing, "net_recovered_urls": recovered - newly_missing}, changes

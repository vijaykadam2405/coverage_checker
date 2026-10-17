from __future__ import annotations

from collections.abc import Iterable

from crawler_coverage_auditor.analysis.coverage import is_inventory_url


def missing_rows(rows: Iterable[dict]) -> list[dict]:
    return [row for row in rows if is_inventory_url(row) and row.get("match_status") == "NOT_FOUND"]

from __future__ import annotations

import json
import re
from collections import defaultdict
from urllib.parse import parse_qsl, urlsplit
from crawler_coverage_auditor.analysis.coverage import is_found, is_inventory_url


def _methods(row: dict) -> set[str]:
    value = row.get("discovery_methods") or "[]"
    try:
        return set(json.loads(value)) if isinstance(value, str) else set(value)
    except (TypeError, json.JSONDecodeError):
        return set()


def _found(row: dict) -> bool:
    return is_found(row)


def _valid(rows: list[dict]) -> list[dict]:
    return [row for row in rows if is_inventory_url(row) and row.get("page_type") != "IMAGE"]


def archive_year(url: str) -> str | None:
    query_year = next((value for key, value in parse_qsl(urlsplit(url).query) if key.lower() == "year" and re.fullmatch(r"(?:19|20)\d{2}", value)), None)
    if query_year:
        return query_year
    match = re.search(r"/((?:19|20)\d{2})(?:[-/]|$)", urlsplit(url).path)
    return match.group(1) if match else None


def archive_coverage(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in _valid(rows):
        year = archive_year(str(row.get("normalized_url", "")))
        if year:
            grouped[year].append(row)
    return [{"year": year, "expected": len(group), "found": sum(_found(row) for row in group),
             "missing": sum(row.get("match_status") == "NOT_FOUND" for row in group),
             "unverified": sum(not _found(row) and row.get("match_status") != "NOT_FOUND" for row in group),
             "coverage_percent": round(100 * sum(_found(row) for row in group) / len(group), 2)}
            for year, group in sorted(grouped.items(), reverse=True)]


def sitemap_coverage(rows: list[dict]) -> list[dict]:
    selected = [row for row in _valid(rows) if "SITEMAP" in _methods(row)]
    sitemap_only = [row for row in selected if _methods(row) <= {"SITEMAP"}]
    return [
        {"segment": "All sitemap URLs", "expected": len(selected), "found": sum(_found(row) for row in selected),
         "missing": sum(row.get("match_status") == "NOT_FOUND" for row in selected)},
        {"segment": "Sitemap-only URLs", "expected": len(sitemap_only), "found": sum(_found(row) for row in sitemap_only),
         "missing": sum(row.get("match_status") == "NOT_FOUND" for row in sitemap_only)},
    ]


def js_only_rows(rows: list[dict]) -> list[dict]:
    static_methods = {"SEED", "STATIC", "SITEMAP", "PAGINATION", "FILTER_GET"}
    return [row for row in _valid(rows) if "JS_DISCOVERY" in _methods(row) and not (_methods(row) & static_methods)]

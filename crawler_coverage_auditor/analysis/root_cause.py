from __future__ import annotations

from crawler_coverage_auditor.analysis.pagination_gaps import pagination_missing_ranges
from crawler_coverage_auditor.analysis.inventory_segments import archive_coverage, js_only_rows, sitemap_coverage


def root_causes(rows: list[dict]) -> list[dict[str, str]]:
    causes: list[dict[str, str]] = []
    for pattern, ranges in pagination_missing_ranges(rows).items():
        if ranges:
            joined = ", ".join(f"{start}-{end}" for start, end in ranges)
            causes.append({"severity": "HIGH", "kind": "DEEP_PAGINATION", "evidence": f"{pattern}: missing pages {joined}"})
    deep = [row for row in rows if row.get("validity") == "VALID_PAGE" and (row.get("navigation_depth") or 0) >= 5 and row.get("match_status") == "NOT_FOUND"]
    if deep:
        causes.append({"severity": "HIGH", "kind": "DEPTH_COVERAGE", "evidence": f"{len(deep)} missing valid URLs at navigation depth >= 5"})
    for segment in sitemap_coverage(rows):
        if segment["expected"] and segment["missing"]:
            causes.append({"severity": "HIGH", "kind": "SITEMAP_GAP", "evidence": f"{segment['segment']}: {segment['missing']} / {segment['expected']} absent from Corpus"})
    js_missing = [row for row in js_only_rows(rows) if row.get("match_status") == "NOT_FOUND"]
    if js_missing:
        causes.append({"severity": "HIGH", "kind": "JS_ONLY_GAP", "evidence": f"{len(js_missing)} JavaScript-only URLs absent from Corpus"})
    for row in archive_coverage(rows):
        if row["missing"] and row["coverage_percent"] < 80:
            causes.append({"severity": "HIGH", "kind": "ARCHIVE_GAP", "evidence": f"Archive year {row['year']} coverage is {row['coverage_percent']}%"})
    return causes

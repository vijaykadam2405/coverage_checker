from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from crawler_coverage_auditor.validation.text_coverage import ASSESSED_TEXT_STATUSES

FOUND_STATUSES = {"EXACT_MATCH", "NORMALIZED_MATCH", "CANONICAL_MATCH", "REDIRECT_MATCH", "FOUND_EXACT", "FOUND_NORMALIZED", "FOUND_CANONICAL", "FOUND_REDIRECT"}


def is_found(row: dict) -> bool:
    return row.get("match_status") in FOUND_STATUSES


def is_inventory_url(row: dict) -> bool:
    """URL-only resources are eligible for presence checks, not validated content."""
    return row.get('validity') == 'VALID_PAGE' or (row.get('validity') == 'URL_ONLY' and row.get('page_type') in {'DOCUMENT', 'IMAGE', 'RESOURCE'})


def metrics(eligible, found, missing, not_tracked=0):
    eligible, found, missing = int(eligible or 0), int(found or 0), int(missing or 0)
    checked = found + missing
    in_scope = eligible - int(not_tracked or 0)
    pending = in_scope - checked
    percent = lambda numerator, denominator: round(100 * numerator / denominator, 2) if denominator else None
    return {'eligible': eligible, 'checked': checked, 'found': found, 'missing': missing, 'unverified': pending,
            'verification_completion': percent(checked, in_scope), 'verified_coverage': percent(found, checked),
            'coverage_lower_bound': percent(found, in_scope), 'coverage_upper_bound': percent(found + pending, in_scope)} | ({'not_tracked': not_tracked} if not_tracked else {})


def coverage_summary(rows: Iterable[dict], *, include_images=False) -> dict[str, float | int | None]:
    rows = list(rows)
    valid = [row for row in rows if is_inventory_url(row) and (include_images or row.get('page_type') != 'IMAGE')]
    found = [row for row in valid if is_found(row)]
    missing = [row for row in valid if row.get("match_status") == "NOT_FOUND"]
    not_tracked = sum(row.get('match_status') == 'NOT_TRACKED' for row in valid)
    unverified = len(valid) - not_tracked - len(found) - len(missing)
    text_checked = [row for row in found if row.get('page_type', 'HTML') == 'HTML' and row.get("text_status") in ASSESSED_TEXT_STATUSES]
    full_text = [row for row in text_checked if row.get("text_status") in {"FOUND_FULL", "FOUND_MOSTLY_FULL"}]
    counts = metrics(len(valid), len(found), len(missing), not_tracked)
    return {"expected_urls": len(valid), "found_urls": len(found), "missing_urls": len(missing), "unverified_urls": unverified,
            "checked_urls": len(found) + len(missing), "discovered_urls": len(rows),
            'not_tracked_urls': not_tracked,
            "url_coverage_percent": counts['verified_coverage'],
            "verification_completion_percent": counts['verification_completion'],
            "coverage_lower_percent": counts['coverage_lower_bound'],
            "coverage_upper_percent": counts['coverage_upper_bound'],
            "text_checked_urls": len(text_checked), "full_or_mostly_full_text": len(full_text),
            "corpus_candidate_urls": sum(is_inventory_url(row) for row in rows),
            "all_checked_urls": sum(is_inventory_url(row) and (is_found(row) or row.get('match_status') == 'NOT_FOUND') for row in rows),
            "url_only_resources": sum(row.get('validity') == 'URL_ONLY' for row in rows),
            "text_coverage_percent": round(100 * len(full_text) / len(text_checked), 2) if text_checked else None}


def coverage_by_depth(rows: Iterable[dict]) -> list[dict]:
    groups: dict[int | str, list[dict]] = defaultdict(list)
    for row in rows:
        if is_inventory_url(row) and row.get("page_type") == 'HTML':
            groups[row.get('navigation_depth') if row.get('navigation_depth') is not None else 'Unknown'].append(row)
    return [{"depth": depth, "expected": len(items), "found": sum(is_found(item) for item in items),
             "missing": sum(item.get("match_status") == "NOT_FOUND" for item in items),
             "unverified": sum(not is_found(item) and item.get("match_status") != "NOT_FOUND" for item in items)} for depth, items in sorted(groups.items(), key=lambda item: (isinstance(item[0], str), item[0]))]

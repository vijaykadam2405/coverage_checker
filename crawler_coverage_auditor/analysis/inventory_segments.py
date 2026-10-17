from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date
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
    pairs = [(key.lower(), value) for key, value in parse_qsl(urlsplit(url).query)]
    query_year = next((value for key, value in pairs if key in {'year', 'archive_year', 'publication_year', 'published_year'} and re.fullmatch(r"(?:19|20)\d{2}", value)), None)
    if query_year:
        return query_year
    years = set()
    for key, value in pairs:
        if key in {'date', 'publish_date', 'publication_date', 'archive_date', 'from', 'to', 'date_from', 'date_to', 'start_date', 'end_date'}:
            try:
                parsed = date.fromisoformat(value)
                if 1900 <= parsed.year <= 2099:
                    years.add(str(parsed.year))
            except ValueError:
                pass
    if len(years) == 1:
        return years.pop()
    if len(years) > 1:
        return None  # A multi-year range must not be assigned to one year.
    match = re.search(r"/((?:19|20)\d{2})(?:[-/]|$)", urlsplit(url).path)
    return match.group(1) if match else None


def row_archive_year(row: dict) -> str | None:
    year = archive_year(str(row.get('normalized_url') or row.get('url') or ''))
    if year:
        return year
    if row.get('page_type') != 'HTML' or row.get('validity') != 'VALID_PAGE':
        return None
    try:
        evidence = json.loads(row.get('content_evidence_json') or '{}')
        publication = evidence.get('website_publication', {})
        parsed = date.fromisoformat(publication.get('date', ''))
        if publication.get('source', '').startswith('website ') and 1900 <= parsed.year <= 2099:
            return str(parsed.year)
    except (TypeError, ValueError, AttributeError):
        pass
    return None


def archive_coverage(rows: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in _valid(rows):
        year = row_archive_year(row)
        if year:
            grouped[year].append(row)
    from .coverage import coverage_summary
    results = []
    for year, group in sorted(grouped.items(), reverse=True):
        summary = coverage_summary(group)
        results.append({'year': year, 'expected': len(group), 'found': summary['found_urls'],
                        'missing': summary['missing_urls'], 'unverified': summary['unverified_urls'],
                        'not_tracked': summary['not_tracked_urls'], 'coverage_percent': summary['url_coverage_percent'],
                        'verification_percent': summary['verification_completion_percent']})
    return results


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
    return [row for row in _valid(rows) if _methods(row) & {'JS_DISCOVERY','JS_SOURCE'} and not (_methods(row) & static_methods)]

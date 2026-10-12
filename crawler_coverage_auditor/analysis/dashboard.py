"""Evidence-based aggregates used by the live UI and downloadable analysis."""
from __future__ import annotations

from collections import Counter, defaultdict
import json
from urllib.parse import parse_qsl, urlsplit

from crawler_coverage_auditor.analysis.coverage import coverage_summary, is_found, is_inventory_url
from crawler_coverage_auditor.analysis.inventory_segments import _methods, archive_year
from crawler_coverage_auditor.analysis.pagination_gaps import contiguous_ranges
from crawler_coverage_auditor.analysis.root_cause import root_causes
from crawler_coverage_auditor.discovery.pagination import numeric_page_parameter, pagination_series_key
from crawler_coverage_auditor.validation.normalize import classify_parameter
from crawler_coverage_auditor.analysis.inventory import inventory_breakdown, html_content_duplicates

TEXT_LABELS = {"FOUND_FULL": "Full content", "FOUND_MOSTLY_FULL": "Mostly full", "FOUND_PARTIAL_TEXT": "Partial content",
               "FOUND_VERY_PARTIAL_TEXT": "Very partial content", "TEXT_MISSING": "Text missing",
               "TEXT_NOT_AVAILABLE": "Stored text unavailable / pending", "SOURCE_TEXT_NOT_AVAILABLE": "Source text unavailable", "URL_ONLY": "URL only", 'API_TEXT_NOT_SUPPORTED': 'Not provided by Corpus API',
               'TEXT_API_ERROR': 'Text API error · resume to retry', 'TEXT_TRUNCATED': 'Stored text truncated · not assessed', 'TEXT_TYPE_MISMATCH': 'Stored record is not HTML · needs review'}
MATCH_LABELS = {"EXACT_MATCH": "Found in corpus", "FOUND_EXACT": "Found in corpus", "NORMALIZED_MATCH": "Found via normalized URL",
                "FOUND_NORMALIZED": "Found via normalized URL", "CANONICAL_MATCH": "Found via canonical URL",
                "REDIRECT_MATCH": "Found via redirect", "REVIEW_REQUIRED": "Needs review", "NOT_FOUND": "Missing from corpus", 'NOT_TRACKED': 'Not tracked by corpus', 'API_ERROR': 'API error · not verified'}


def grouped_coverage(rows: list[dict], grouper, name: str) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        if is_inventory_url(row):
            key = grouper(row)
            if key is not None:
                groups[key].append(row)
    result = []
    for key, group in groups.items():
        summary = coverage_summary(group, include_images=True)
        result.append({name: key, "Valid URLs": summary["expected_urls"], "Found": summary["found_urls"],
                       "Missing": summary["missing_urls"], "Unverified": summary["unverified_urls"], "Coverage %": summary["url_coverage_percent"], 'Verification %': summary['verification_completion_percent']})
    return sorted(result, key=lambda item: (isinstance(item[name], str), item[name]))


def filter_state(row: dict) -> str:
    return ", ".join(f"{key}={value}" for key, value in parse_qsl(urlsplit(row.get("normalized_url") or row["url"]).query)
                     if classify_parameter(key) == "FILTER")


def section(row: dict) -> str:
    parts = urlsplit(row.get("normalized_url") or row["url"]).path.strip("/").split("/")
    return "/" + parts[0] if parts[0] else "/ (home)"


def detail_row(row: dict) -> dict:
    validity, match = row.get("validity"), row.get("match_status")
    eligible = is_inventory_url(row)
    html = row.get('page_type', 'HTML') == 'HTML'
    state = "Checking corpus" if row.get("state") == "CHECKING_CORPUS" else "URL collected" if validity == 'URL_ONLY' else "Crawling" if row.get("state") == "FETCHING" else "Queued" if not validity else "Collected" if eligible and match is None else "Completed" if eligible else "Failed / excluded"
    corpus = MATCH_LABELS.get(match, "Found in corpus" if is_found(row) else "Pending") if eligible else "Not applicable"
    content = TEXT_LABELS.get(row.get("text_status"), "Not checked") if html else 'URL only'
    issue = ("FOUND BY COVERAGE — MISSED BY CRAWLER" if html else 'Resource URL missing from corpus; file availability not assessed') if match == "NOT_FOUND" and eligible else row.get("http_error") or ""
    severity = "High" if issue or (validity and not eligible and validity != 'RENDER_REQUIRED') else "Medium" if match == "REVIEW_REQUIRED" or content in {"Partial content", "Very partial content", "Text missing"} else "None"
    if not issue and validity and not eligible and validity != 'RENDER_REQUIRED':
        issue = validity.replace("_", " ").title()
    if not issue and content in {"Partial content", "Very partial content", "Text missing"}:
        issue = "Stored content is incomplete"
    pagination = numeric_page_parameter(row.get("normalized_url") or row["url"])
    try:
        evidence = json.loads(row.get('evidence_json') or '{}')
    except (ValueError, TypeError):
        evidence = {}
    word_method = evidence.get('text_comparison', {}).get('method') == 'unique_word_coverage'
    if word_method and content in {'Partial content', 'Very partial content'} and issue == 'Stored content is incomplete':
        issue = 'Stored text differs from saved website text; capture dates may differ'
    return {"URL": row["url"], "Title": row.get("title"), "Crawl status": state, "Corpus status": [corpus],
            "Content coverage": [content], "Depth": row.get("navigation_depth") if row.get("navigation_depth") is not None else row.get("min_depth"),
            "Discovery method": ", ".join(sorted(_methods(row))), "Parent URL": row.get("parent_url"),
            "Content type": row.get("content_type"), "Resource": row.get("page_type", "HTML"), "Filter state": filter_state(row),
            "Pagination page": pagination[1] if pagination else None, "HTTP": row.get("status_code"), "Issue": issue,
            "Severity": [severity], "Live text characters": row.get("text_length") if html else None, "Corpus text characters": row.get("corpus_text_length") if html else None,
            "Text similarity %": round(row["similarity"] * 100, 1) if html and not word_method and row.get("similarity") is not None else None,
            'Word coverage %': round(row['similarity'] * 100, 1) if html and word_method and row.get('similarity') is not None else None,
            "Canonical URL": row.get("canonical_url"), "Final URL": row.get("final_url"), "Corpus URL": row.get("corpus_url"),
            'Verification source': 'Corpus Coverage API' if (row.get('provider') or '').startswith('coverage_api:') else 'Historical / not checked',
            'Corpus first fetched': evidence.get('first_fetched'), 'Corpus last fetched': evidence.get('last_fetched'),
            'Corpus title': evidence.get('title'), 'Corpus versions': evidence.get('versions'), 'API match': evidence.get('match'),
            'Corpus text captured at': evidence.get('text_api', {}).get('last_fetched_at'),
            'Website text captured at': row.get('fetched_at'),
            'Corpus text version': evidence.get('text_api', {}).get('version_id'),
            'Beginning word coverage %': evidence.get('text_comparison', {}).get('segments', {}).get('beginning'),
            'Middle word coverage %': evidence.get('text_comparison', {}).get('segments', {}).get('middle'),
            'Ending word coverage %': evidence.get('text_comparison', {}).get('segments', {}).get('ending')}


def build_analysis(rows: list[dict], sections: dict, *, include_details=True) -> dict:
    summary = coverage_summary(rows)
    valid = [row for row in rows if is_inventory_url(row) and row.get("page_type") != "IMAGE"]
    docs = [row for row in rows if row.get("page_type") == "DOCUMENT"]
    text_counts = Counter(TEXT_LABELS.get(row.get("text_status"), "Not checked") for row in valid if is_found(row) and row.get('page_type', 'HTML') == 'HTML')
    segments = []
    for label, group in [("HTML", [row for row in rows if row.get("page_type") == "HTML"]),
                         ("Pagination", [row for row in rows if "PAGINATION" in _methods(row)]),
                         ("Filters", [row for row in rows if filter_state(row)]),
                         ("Archives", [row for row in rows if archive_year(row.get("normalized_url", ""))]),
                         ("Documents", docs),
                         ("JavaScript / API", [row for row in rows if _methods(row) & {"JS_DISCOVERY", "API_DISCOVERY"}]),
                         ("Sitemap", [row for row in rows if "SITEMAP" in _methods(row)])]:
        value = coverage_summary(group)
        segments.append({"Area": label, "Valid URLs": value["expected_urls"], "Found": value["found_urls"], "Missing": value["missing_urls"],
                         "Unverified": value["unverified_urls"], "Coverage %": value["url_coverage_percent"] if value["expected_urls"] else None})
    series = []
    for item in sections.get("pagination", []):
        missing, found, unverified = [], [], []
        for row in valid:
            page = numeric_page_parameter(row.get("normalized_url", ""))
            if page and pagination_series_key(row["normalized_url"], page[0]) == item["series_key"]:
                (found if is_found(row) else missing if row.get("match_status") == "NOT_FOUND" else unverified).append(page[1])
        series.append({"Series": item["template_url"], "Parameter": item["parameter_name"], "Observed maximum": item.get('observed_max'), "Website maximum": item.get("max_page") if item['status'] == 'VERIFIED' else None,
                       "Maximum verified": item["status"] == "VERIFIED", "Found pages": len(found), "Missing pages": len(missing),
                       "Unverified pages": len(unverified), "Missing ranges": ", ".join(str(start) if start == end else f"{start}–{end}" for start, end in contiguous_ranges(missing)),
                       "Boundary status": item["status"], "Evidence": item.get("evidence")})
    gaps = root_causes(rows)
    for item in grouped_coverage(valid, lambda row: filter_state(row) or None, "Filter state"):
        if item["Missing"] and item["Coverage %"] is not None and item["Coverage %"] < 80:
            gaps.append({"severity": "HIGH", "kind": "FILTER_GAP", "evidence": f"{item['Filter state']}: {item['Missing']} confirmed misses of {item['Valid URLs']} valid URLs ({item['Coverage %']}% coverage). Likely filter traversal gap; cause needs production log verification."})
    return {"summary": summary, "details": [detail_row(row) for row in rows] if include_details else [], "segments": segments,
            "inventory": inventory_breakdown(rows), "html_duplicates": html_content_duplicates(rows),
            "depth": grouped_coverage([row for row in rows if row.get('page_type') == 'HTML'], lambda row: row.get("navigation_depth") if row.get("navigation_depth") is not None else 'Unknown', "Depth"),
            "years": grouped_coverage(rows, lambda row: archive_year(row.get("normalized_url", "")), "Year"),
            "sections": grouped_coverage(rows, section, "Website section"),
            "filter_states": grouped_coverage(rows, lambda row: filter_state(row) or None, "Filter state"),
            "pagination": series, "content_counts": dict(text_counts), "documents": coverage_summary(docs), "gaps": gaps,
            "filters_detected": len({(item["source_url"], item["control_name"]) for item in sections.get("filters", [])}),
            "max_depth": max((row['navigation_depth'] for row in rows if row.get('navigation_depth') is not None), default=None)}

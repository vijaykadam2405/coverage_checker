from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from crawler_coverage_auditor.analysis.dashboard import build_analysis


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def write_html(path: Path, summary: dict, rows: list[dict], issues: list[dict], *,
               depth_rows: list[dict] | None = None, pagination_rows: list[dict] | None = None,
               filter_rows: list[dict] | None = None, document_rows: list[dict] | None = None,
               archive_rows: list[dict] | None = None, sitemap_rows: list[dict] | None = None,
               js_rows: list[dict] | None = None, limits: list[dict] | None = None,
               events: list[dict] | None = None, errors: list[dict] | None = None) -> None:
    """Self-contained offline report; identical corpus/coverage rules to the live UI."""
    sections = {"pagination": pagination_rows or [], "filters": filter_rows or [], "documents": document_rows or []}
    analysis = build_analysis(rows, sections)
    data = {"summary": summary, "rows": rows, "issues": issues, "analysis": analysis,
            "filters": filter_rows or [], "documents": document_rows or [], "sitemaps": sitemap_rows or [],
            "limits": limits or [], "events": events or [], "errors": errors or []}
    template = Path(__file__).with_name("templates") / "coverage.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template.read_text(encoding="utf-8").replace("__AUDIT_DATA__", _json(data)), encoding="utf-8")

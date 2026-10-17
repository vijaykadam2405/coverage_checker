"""Discovery evidence reconciliation, never a synthetic website completeness score."""

import json
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SOURCE_NAMES = {
    "SCRIPT": "JavaScript assets",
    "MANIFEST": "App manifests",
    "WORDPRESS_ROOT": "Public API discovery",
    "WORDPRESS_LIST": "Public content collections",
    "RESOURCESYNC": "ResourceSync lists",
    "WAYBACK": "Wayback URL index",
    "COMMONCRAWL": "Common Crawl URL index",
}


def discovery_reconciliation(tasks):
    groups = defaultdict(
        lambda: {
            "states": defaultdict(int),
            "ids": set(),
            "totals": set(),
            "pages": set(),
            "maximums": set(),
        }
    )
    sources = defaultdict(lambda: defaultdict(int))
    for task in tasks:
        sources[task["kind"]][task["state"]] += 1
        if task["kind"] != "WORDPRESS_LIST":
            continue
        parts = urlsplit(task["url"])
        key = urlunsplit(
            (
                *parts[:3],
                urlencode(
                    [
                        (name, value)
                        for name, value in parse_qsl(parts.query)
                        if name != "page"
                    ]
                ),
                "",
            )
        )
        group = groups[key]
        group["states"][task["state"]] += 1
        try:
            evidence = json.loads(task.get("evidence_json") or "{}")
        except (ValueError, TypeError):
            continue
        if task["state"] == "DONE":
            group["ids"].update(evidence.get("item_ids", []))
            for field, target in [
                ("reported_total", "totals"),
                ("page", "pages"),
                ("reported_pages", "maximums"),
            ]:
                value = evidence.get(field)
                if type(value) is int:
                    group[target].add(value)
    collections = []
    for url, group in groups.items():
        total = next(iter(group["totals"])) if len(group["totals"]) == 1 else None
        maximum = next(iter(group["maximums"])) if len(group["maximums"]) == 1 else None
        complete = (
            total is not None
            and maximum is not None
            and len(group["ids"]) == total
            and len(group["pages"]) == maximum
            and set(group["states"]) <= {"DONE"}
        )
        collections.append(
            {
                "Collection": url,
                "Unique items observed": len(group["ids"]),
                "Reported total": total,
                "Pages processed": len(group["pages"]),
                "Reported pages": maximum,
                "Status": "Published counts reconciled"
                if complete
                else "Unresolved or changing collection",
                "Evidence": "Applies only to this API collection. Equal counts are supporting evidence, not proof of all website URLs.",
            }
        )
    return {
        "sources": [
            {
                "Source": SOURCE_NAMES.get(kind, kind),
                "Completed": states["DONE"],
                "Pending": states["PENDING"] + states["RUNNING"],
                "Failed": states["FAILED"],
                "Disabled": states["SKIPPED"],
            }
            for kind, states in sorted(sources.items())
        ],
        "collections": collections,
        "note": "All discovered work processed does not establish that all existing website pages were discovered.",
    }

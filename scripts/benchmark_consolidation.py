"""Offline parser benchmark. Synthetic input, no website/Corpus/database writes."""

import argparse
import hashlib
import json
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler_coverage_auditor.discovery.advanced import (
    inline_scripts,
    metadata_links,
    source_tasks,
)
from crawler_coverage_auditor.discovery.evidence import embedded_links
from crawler_coverage_auditor.discovery.filters import detect_filters, get_filter_urls
from crawler_coverage_auditor.discovery.page import page_analysis
from crawler_coverage_auditor.discovery.pagination import detect_pagination
from crawler_coverage_auditor.discovery.static_links import (
    count_listing_items,
    extract_links,
)
from crawler_coverage_auditor.validation.access import detect_access
from crawler_coverage_auditor.validation.normalize import normalize_url
from crawler_coverage_auditor.validation.publication import publication_evidence
from crawler_coverage_auditor.validation.text_extract import extract_text


def sample():
    return (
        '<html><head><title>News</title><link rel="previous" href="?page=1"><link rel="next" href="?page=3"><script type="application/ld+json">'
        + json.dumps({"url": "/article", "image": "/picture.png"})
        + '</script></head><body><nav><a href="/navigation">Navigation</a></nav><main><h1>Latest news</h1>'
        + "".join(
            '<article><a href="/news/article-%d">Article %d</a><p>%s</p></article>'
            % (i, i, "Meaningful article text and detail. " * 4)
            for i in range(150)
        )
        + '<a download href="/report.pdf">PDF</a><img data-src="/lazy.png"><form action="/news/" method="get"><select name="year"><option value="2024">2024</option><option value="2025">2025</option></select></form><script>const route="/script-route";</script></main></body></html>'
    )


def analyze(html):
    base = "https://example.test/news/"
    with page_analysis() as metrics:
        result = {
            "static": sorted({normalize_url(x.url) for x in extract_links(html, base)}),
            "structured": sorted(
                {normalize_url(x.url) for x in embedded_links(html, base)}
            ),
            "metadata": sorted(
                {normalize_url(x.url) for x in metadata_links(html, base)}
            ),
            "tasks": source_tasks(html, base),
            "scripts": inline_scripts(html),
            "filters": [get_filter_urls(c, base) for c in detect_filters(html, base)],
            "pagination": [
                (h.kind, h.value, h.url) for h in detect_pagination(html, base)
            ],
            "text": extract_text(html),
            "access": str(detect_access(200, {}, html, base)),
            "items": count_listing_items(html),
            "publication": publication_evidence(html, base),
        }
        return result, metrics.metrics()


def benchmark(repeat=7):
    html = sample()
    result, metrics = analyze(html)
    times = []
    for _ in range(repeat):
        started = time.perf_counter()
        analyze(html)
        times.append(time.perf_counter() - started)
    tracemalloc.start()
    analyze(html)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "html_bytes": len(html.encode()),
        "median_ms": round(statistics.median(times) * 1000, 2),
        "peak_traced_bytes": peak,
        "analysis": metrics,
        "url_sets": {key: result[key] for key in ("static", "structured", "metadata")},
        "text_sha256": hashlib.sha256(result["text"][0].encode()).hexdigest(),
        "note": "Local synthetic analysis only; not end-to-end scan speed or RSS memory.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=7)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    print(json.dumps(benchmark(args.repeat), indent=2))

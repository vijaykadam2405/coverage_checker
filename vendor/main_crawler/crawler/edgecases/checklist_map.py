"""Map the user's 27-problem checklist (crawler_testing.xlsx) to what the crawler actually does.

The xlsx is the operator's own taxonomy of things that break a crawler. Almost every item maps onto
a category already built and measured in this project; a few are handled by long-standing engine
machinery. This produces one honest row per problem: the mechanism, where it lives, and the measured
status — so the report answers "you listed 27 problems; here is each one" directly.

    python edgecases/checklist_map.py     # -> edgecases/checklist_status.jsonl + a printed table
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECKLIST = HERE / "corpus" / "user_checklist.json"
OUT = HERE / "checklist_status.jsonl"

# problem-number -> (mechanism, where it lives, status). Status is one of:
#   handled     — a detector/handler exists and is measured
#   detect+skip — detected and deliberately skipped with a recorded reason (the right call)
#   partial     — works for the common case; a documented gap remains
#   engine      — handled by long-standing engine machinery (not new in this project)
STATUS = {
    1:  ("robots.txt consulted per host; disallowed paths skipped, crawl-delay honoured",
         "crawler/robots.py, RobotsCache", "engine"),
    2:  ("429/503 -> honour Retry-After + cool the whole host down; auto-careful drops a host that "
         "refuses under load to concurrency 1", "errors.py policy + WEAK_UNDER_LOAD", "handled"),
    3:  ("captcha classified by type+sitekey; image-text solved free by local VLM OCR; interactive "
         "Cloudflare solved on the host (C4)", "captcha.py, camofox_client, hostsolver.py", "handled"),
    4:  ("login/SSO/JSON-auth walls detected and SKIPPED with a reason — never a login form stored "
         "as content", "edgecase.py CASE_AUTH_WALL", "detect+skip"),
    5:  ("geo-block via HTTP 451 or a region message; skipped with a distinct reason",
         "edgecase.py CASE_GEO_BLOCK", "detect+skip"),
    6:  ("WAF/anti-bot by vendor fingerprint (Cloudflare/Akamai/DataDome/Imperva/Kasada/PerimeterX); "
         "managed -> C3 stealth, interactive -> C4 host solve", "captcha.py + async_engine ladder", "handled"),
    7:  ("JS-shell pages detected (SPA root marker + thin text) and escalated to a browser render",
         "edgecase.py CASE_JS_RENDER + _render_page", "handled"),
    8:  ("lazy-load / infinite scroll: bounded auto-scroll + lazy-image resolve before snapshot",
         "async_engine._auto_interact + _LAZY_RESOLVE_JS", "partial"),
    9:  ("malformed HTML parsed with lxml's recovering parser; a page that yields no main text is "
         "recorded, not crashed on", "parse.py / extract.py", "engine"),
    10: ("redirect loop caught when the client hits its hop cap; dropped as a trap",
         "edgecase.py CASE_REDIRECT_LOOP", "handled"),
    11: ("404/410 -> marked permanently gone, never re-fetched; soft-404 (200 body) also caught",
         "errors.py GONE + edgecase.py CASE_SOFT_404", "handled"),
    12: ("5xx -> retry_later (one in-run requeue + next-run eligible); 522/524 origin errors read "
         "as timeout", "errors.py policy + edgecase.py", "handled"),
    13: ("per-I/O timeout AND a wall-clock body budget, so a slow-trickle tarpit can't hold a worker",
         "errors.py TIMEOUT + fetcher._body_budget_s", "handled"),
    14: ("TLS trust failure classified and SKIPPED — never bypassed with verify=False",
         "errors.py SSL (terminal)", "detect+skip"),
    15: ("DNS NXDOMAIN -> mark gone, feed the circuit breaker; bare ConnectError also classified",
         "errors.py DNS + _PATTERNS", "handled"),
    16: ("duplicate content collapsed by content-hash (dedup) + canonical URL normalisation",
         "dedup.py + canonicalize.py", "engine"),
    17: ("charset from the HTTP header; body decoded with errors='ignore' so non-UTF-8 never crashes "
         "the parse", "fetcher.py decode", "partial"),
    18: ("non-HTML routed by content-type + a download-abort signal; PDFs text-extracted, assets to "
         "MinIO", "fetcher._classify_kind + pdfextract.py", "handled"),
    19: ("pagination followed via rel=next + numbered pagers; deep page-N past a bound is a trap",
         "async_engine._enqueue_links + errors.looks_like_trap", "handled"),
    20: ("tracking params (utm/fbclid/gclid...) stripped in canonicalisation; per-host+path "
         "query-variant cap bounds the rest. Note: arbitrary session-id NAMES (;jsessionid, ?sid) "
         "are not in the strip list — the cap catches them, canonicalisation does not",
         "canonicalize.py TRACKING_PARAMS + _enqueue_candidate", "partial"),
    21: ("calendar / facet / deep-page traps refused at the frontier (92% of the 64-URL benchmark, "
         "45 by stateless guards)", "edgecase.calendar_trap + errors.looks_like_trap", "handled"),
    23: ("the <link rel=canonical> tag itself is NOT read; dedup instead collapses identical bodies "
         "by content-hash, which achieves the same end (one stored copy) without trusting the tag",
         "dedup.py content-hash", "partial"),
    24: ("scheme + host lowercased, trailing slash and tracking params stripped. Deliberately does "
         "NOT collapse www or force http->https: measured, that caused a redirect loop on a host "
         "whose apex 307s back to www. Path case is preserved (some servers are case-sensitive)",
         "canonicalize.py", "partial"),
    27: ("per-host adaptive concurrency + delay + careful mode; the crawl throttles itself rather "
         "than DoSing a host", "async_engine HostLimiter + adaptive gate", "engine"),
    28: ("incremental recrawl (conditional GET/304) + content-hash detect a page that changed vs a "
         "stale one", "async_engine _conditional_unchanged + dedup", "handled"),
    29: ("A/B variants: recorded as they arrive; content-hash keeps only distinct bodies — variant "
         "detection itself is not attempted", "dedup.py", "partial"),
    30: ("parsing generalises via a keyword gate + structural extraction rather than per-site "
         "selectors; a page with no main text is dropped, not mis-parsed", "gate.py + extract.py", "engine"),
}


def main() -> int:
    if not CHECKLIST.exists():
        print(f"missing {CHECKLIST} — run the xlsx extractor first")
        return 1
    data = json.loads(CHECKLIST.read_text(encoding="utf-8"))
    rows = []
    for entry in data:
        prob = entry["problem"]
        m = re.match(r"(\d+)\.", prob)
        num = int(m.group(1)) if m else 0
        mech, where, status = STATUS.get(num, ("(unmapped)", "", "gap"))
        rows.append({"num": num, "problem": prob, "urls": entry["urls"],
                     "mechanism": mech, "where": where, "status": status})
    rows.sort(key=lambda r: r["num"])
    with OUT.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    from collections import Counter
    tally = Counter(r["status"] for r in rows)
    print(f"{'#':>3}  {'status':<12} problem")
    for r in rows:
        print(f"{r['num']:>3}  {r['status']:<12} {r['problem'][:64]}")
    print(f"\n  {len(rows)} problems: {dict(tally)}")
    print(f"  -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

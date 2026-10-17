# CrawlScope consolidation audit and verification

Date: 14 October 2026. Scope: the active React/FastAPI coverage application, not
activation of the retained production crawler fleet. No production scan was
stopped, no audit database migration was necessary, and no saved inventory was
deleted or rewritten. The worktree already contained user changes; those were
preserved. No dependency upgrades or external publication were performed.

## Baseline and method

The initial phase was read-only for project code. The baseline suite completed
with **384 Python tests passing** (173.74 seconds; two non-failing warnings).
Frontend formatting, **18 frontend tests**, and the Vite production build passed.
The initial SQLite read snapshot contained inventories 1/2/3/4 with
237/649/32,255/870 URLs respectively; inventory 3 was running. There were no
duplicate `(audit_run_id, normalized_url)` records.

Because a production scan was active, there was no comparative live crawl, no
experimental request against Corpus, and no restart. Comparisons use controlled
offline HTML/JSON/header/filter inputs, isolated local HTTP/browser fixtures and
temporary SQLite databases. In-process API smoke checks explicitly forbade
outbound HTTPX calls. Counts from a live inventory are observations, not a fixed
benchmark or proof of discovery completeness.

## Capability inventory

PASS means the described supported behavior is connected and tested, not that
every website or protocol variation is supported.

| Capability | State | Audit decision |
| --- | --- | --- |
| Static anchors, frames, files, lazy images, buttons, canonical/alternate links | PASS | Retain; consolidate native/vendor parsing into the owned extractor |
| Robots policy, crawl-delay, nested/compressed sitemaps | PASS | Retain independently; policy is not an inventory |
| RSS/Atom and HTTP pagination relations | PASS | Reuse feed results; one header relation parser |
| Structured JSON, JSON-LD, hydration/framework state | PASS | Share exact snapshot and JSON results, retaining separate source labels |
| JavaScript source parsing/imports | PASS | Retain Acorn; do not evaluate source in the source-analysis process |
| Browser-rendered pages, frames, same-page tabs, Load More, scrolling | PASS | Retain; static HTML cannot replace runtime discovery |
| Arbitrary custom JavaScript controls / POST filters | PARTIAL | Supported controls only; public read-only guards remain |
| GET filters and observed control dependencies | PASS | Cache generated URLs by effective action/query/default/value dependencies |
| Exhaustive multi-filter Cartesian products | PARTIAL | Not claimed; no blind combination explosion |
| Numeric query/path pagination | PASS | Existing probes and boundary evidence retained |
| Non-contiguous pagination and unknown opaque cursors | PARTIAL | Follow evidence; no universal maximum guarantee or invented cursors |
| Date pagination | PASS | Actual next/previous date URLs followed; dates are not guessed |
| Multiple independent paginators/custom parameter conventions | PARTIAL | Recognized patterns only; may require site adapters |
| Public WordPress collections/manifests/ResourceSync | PASS | Advertised supported collections only; reconcile available counts |
| Arbitrary public APIs / GraphQL applications | PARTIAL | Published URL/continuation evidence supported, not universal introspection |
| Wayback CDX candidates | PASS | Normal scans schedule it; historical HTML must validate live |
| Common Crawl | PARTIAL | Adapter requires an explicitly selected index |
| Orphan-page discovery | PARTIAL | Independent inventories can expose candidates; invisible pages are unknowable |
| URL templates/identifier patterns | PARTIAL | Templates are evidence, not fabricated existing URLs |
| Localization | PARTIAL | Published alternate links are collected; locale permutations are not invented |
| Corpus URL presence and HTML text retention | PASS | Separate read-only, serial API operations remain |
| PDFs/documents/images | PASS | URLs only; presence is distinct from availability |
| Document download validation/extraction | NOT RUN | Intentionally excluded by existing URL-only requirements |
| Login/CAPTCHA solving, fleet, ingestion | NOT RUN | Not activated; independent public-page auditing retained |
| Adaptive work reduction | PARTIAL | Exact-snapshot/cache reuse, not disabling a source after low yield |
| Absolute all-page completeness | NOT RUN | Not provable from an exhausted queue alone |

## Implemented consolidation

1. `discovery/page.py` supplies bounded task-local exact-snapshot analysis. It
   shares parsed HTML/JSON and derived JSON link results. Destructive text,
   access and publication readers receive copies; navigation/scripts survive.
   The scope resets on return, exception and cancellation. Independent tasks
   cannot inherit another top-level scan's mutable analysis object.
2. `static_links.extract_links` now owns normal link extraction. The second
   imported parsing execution pass is retired. URL-set regression cases retain
   prior useful link behavior and resources, while preserving SPA fragments
   instead of manufacturing fragment-stripped route aliases.
3. `evidence.header_relations` is the one relation parser used by both pagination
   headers and advertised API/ResourceSync discovery. It supports first/next/
   prev/previous/last, multi-token relations and commas inside URL targets, skips
   malformed targets and does not treat `rel=` in a quoted title as a relation.
4. Candidate deduplication uses `(resolved URL, tag)`, retaining type hints and
   the first meaningful label. Database normalization, provenance/parent-edge
   merging and unique constraints still apply downstream.
5. The feed links computed during response classification are reused during
   collection. XML remains a distinct parser from HTML.
6. GET filter generation caches immutable URL results using the effective
   origin/path, retained query pairs, defaults after precedence, name and actual
   values. Labels/selectors/definitions and edges remain per source page.
7. Text retention reuses live unique-word sets. Historical token similarity
   preserves its lower-case whitespace-token definition rather than replacing
   it with casefolded Unicode words. Redundant whitespace normalization in that
   legacy calculation is removed without changing its result.
8. Summaries have a one-second, bounded, defensive-copy cache invalidated by
   database/WAL changes, expiry or file replacement. A revision change during a
   read prevents caching that result. Default summary responses retain `health`.
   React explicitly requests `diagnostics=false`, for which `health` is null and
   median/fingerprint-health calculations do not run. The default diagnostic API
   remains available to existing consumers.
9. Browser GET JSON may be reused once through the HTTP client. Reuse requires
   exact URL/query, matching semantic request/session headers, a successful
   non-redirect JSON response, explicit positive max-age, fresh age, no Vary,
   no Set-Cookie, no authorization/range/unknown application headers and the
   existing policy checks. Storage is at most eight 1-MiB bodies, for at most
   15 seconds. Missing proof means an ordinary paced fetch, never an unsafe hit.
   POST responses and documents are never substituted. Actual live hit rate is
   unmeasured; browser/HTTP Accept or language differences intentionally miss.

All 17 normal discovery groups remain available. Source tags are not collapsed
into Metadata, and merging parsers does not merge unrelated URLs or remove
parent relationships/depth evidence. Native/imported parsing compatibility
adapters and the complete vendor import remain for accounting and tests, not
duplicate normal-scan execution. Challenge helpers remain connected.

## Genuine discovery additions

- Native head relations now include first, last and previous, including
  multi-token rel attributes.
- Known `location.assign()`/`location.replace()` button URLs are recognized.
- Pagination skips malformed sibling hrefs instead of aborting the page.
- Published previous/first/last links also feed observed non-numeric/date
  traversal; deferred pagination preserves those directions.
- HTML-relative metadata, scripts, forms and pagination honor `<base href>`.
  Relative HTTP headers still resolve against the response URL, not HTML base.
- Embedded JSON MIME matching accepts case differences and MIME parameters.

Numeric probing retains its explicit contiguous/monotonic-sequence assumption;
blocks, duplicate results and observed holes do not establish a verified end.
No page-count, depth, browser-action or filter-value ceiling was introduced.

## Measurements

The same 32,272-byte synthetic news document, 150 article links and seven timed
iterations were used before/after on this machine while the real scan remained
active. These are local analysis measurements, not website scan speed promises.

| Measure | Before | After |
| --- | ---: | ---: |
| Median combined analysis | 84.02 ms | 50.94 ms |
| BeautifulSoup constructions | 12 | 4 |
| Traced temporary allocation peak | 3,007,372 bytes | 1,657,246 bytes |
| Static normalized URL set | 156 | Same 156 |
| Structured normalized URL set | 2 | Same 2 |
| Metadata normalized URL set | 2 | Same 2 |
| Extracted text/title/H1 | Baseline | Identical |
| GET filter URL states | Baseline | Identical |

The observed median reduction is **39.4%** for this analysis workload. The four
post-change constructions include three defensive tree copies; the new analysis
counter confirms **one full HTML parse** and one JSON parse for this snapshot.
Pagination gains previous-page/number evidence that the previous detector missed.
The new benchmark script produced a corroborating median of 50.82 ms; timings and
tracemalloc peaks vary between processes. RSS memory and production end-to-end
speed were NOT RUN as comparative experiments.

Controlled tests additionally verify:

- A single download anchor emits one URL/tag candidate instead of two.
- A valid matching cached GET response avoids one network GET; a later use or
  mismatched session/query/header requires the normal request.
- Equivalent filter generation hits the cache; changing dependencies produces
  different states and mutating a returned list cannot corrupt the cache.
- Repeated unchanged summaries use one read connection instead of two; a WAL
  write invalidates the result, and explicit diagnostic opt-out avoids the
  health queries completely.
- Existing serial/checkpoint tests retain their website, browser, Corpus-presence
  and HTML-text operation counts. File bodies/text are not fetched.

Reproduce local analysis without touching inventory or contacting services:

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_consolidation.py --repeat 7
```

New report summary fields prefixed `performance_` record parse/cache counts,
parser time, controller stage time including waits, website request attempts and
response-header latency, Corpus request attempts/transport time/backoffs, and
browser-response reuse. These apply to that process invocation, not all resumed
historical work. Stage times are inclusive and must not be summed as disjoint
CPU time. SQL per-statement production profiling is NOT RUN to avoid adding
trace overhead; deterministic tests measure relevant operations locally.

## Files and cleanup

Added shared modules: `discovery/page.py`, `services/read_cache.py`, and
`validation/response_cache.py`. Added regression tests:
`test_logic_consolidation.py`, `test_response_reuse.py`, `test_read_cache.py`.
Added `scripts/benchmark_consolidation.py` and this report.

Modified discovery: static links, evidence, resources, advanced adapters,
filters, pagination, tabs and browser; their controller is connected in auditor.
Modified validation: text extraction, access checks, publication, fingerprints,
text coverage and HTTP client; modified Corpus client only for numeric metrics.
Modified queries/API and the React dashboard polling URL; integration status
and README/integration documentation now describe the owned parsing path.
Two old test expectations were updated deliberately: parser execution no longer
invokes the vendor a second time, and duplicate candidates are eliminated before
storage. New tests check replacement behavior rather than deleting the tests.

The separate cleanup pass removed unused imports, normalized helper naming,
formatted new Python modules/tests and reviewed imports/runtime connections.
No vendor artifacts, runtime data, credentials, backups or required dependencies
were deleted. No source files were removed in this task. The pre-existing
deletion of `docs/ZIP_ADAPTATION.md` belongs to earlier work, not this cleanup.
New safety/cache modules and tests increase total source lines; the measured
benefit is fewer runtime parsing paths and repeated operations, not a claim of
net line-count reduction.

## Final checks

| Check | State |
| --- | --- |
| Baseline audit and test/build results | PASS |
| Controlled URL-set/text/filter preservation | PASS |
| New cache safety/invalidation regressions | PASS |
| Fresh backend import and Python syntax inspection | PASS |
| Fresh read-only API state/summary/inventory/analysis/features smoke | PASS |
| SQLite duplicate and foreign-key inspection | PASS |
| Dependency consistency (`pip check`) | PASS |
| Tracked runtime/.env/build files | PASS: none tracked |
| Final Python tests after cleanup | PASS: 415 tests, 174.94 seconds, two non-failing warnings |
| Final frontend checks after cleanup | PASS: 18 tests, formatting and production build |
| Static unused/undefined-name checks (`ruff --select F`) | PASS |
| New Python file formatting | PASS |
| Type checker | NOT RUN: no configured type-check command |
| Whole live-site before/after discovery comparison | NOT RUN: active scan preserved |
| Activate new backend in the current worker | NOT RUN: pause/finish and restart required |
| Universal custom controls, GraphQL, route-value enumeration, all-page proof | PARTIAL: supported evidence-based behavior only |

Development tests caught a missing publication JSON import, nested Excel-summary
metrics and a legacy duplicate-list assertion; these were corrected before the
final cleanup. The pre-cleanup full suite had 411 passes and that one legacy
expectation failure; the corrected focused serial/checkpoint/normalization suite
then passed. Final verification results below supersede intermediate runs.

No automated type checker was configured and none was installed as an unrelated
upgrade. Remaining known warnings are Starlette's TestClient/httpx deprecation
and an invalid escape in untouched vendor test source. No live Corpus capacity
test or automatic restart was attempted. Pause the scan, wait for saving to
finish, stop its server with Ctrl+C, and run `npm.cmd --prefix frontend start`
when ready to activate the new backend. Existing results/checkpoints stay saved.

### Intended-change completion checklist

| Intended change | Implemented | Runtime connected | Regression tested | Verified |
| --- | --- | --- | --- | --- |
| Shared snapshots and mutation isolation | PASS | PASS | PASS | PASS |
| Embedded JSON parse/result reuse with original source labels | PASS | PASS | PASS | PASS |
| Owned HTML parser / retired second vendor pass | PASS | PASS | PASS | PASS |
| One HTTP relation parser | PASS | PASS | PASS | PASS |
| Candidate deduplication and feed result reuse | PASS | PASS | PASS | PASS |
| Dependency-sensitive filter URL cache | PASS | PASS | PASS | PASS |
| Conditional browser GET JSON reuse | PASS | PASS | PASS | PASS: controlled reuse; production hit rate NOT RUN |
| Revision-aware summary cache / diagnostic opt-out | PASS | PASS | PASS | PASS |
| Distinct text metrics with shared intermediates | PASS | PASS | PASS | PASS |
| Base-relative, malformed-link, MIME and observed-date improvements | PASS | PASS | PASS | PASS |
| Numeric request/parser/stage performance evidence | PASS | PASS | PASS | PASS: inclusive timings, not full SQL profiling |
| Automatic source disabling / exhaustive patterns and controls | PARTIAL | PARTIAL | PARTIAL | PARTIAL: deliberately not claimed |
| Full active-site parity and new backend activation | NOT RUN | NOT RUN | NOT RUN | NOT RUN: active worker preserved |

Final backend result: **415 passed, two warnings** (174.94 seconds), compared with
the **384-pass baseline**. Final frontend result: **18 passed**, formatting and
production build passed. Fresh-process read-only API smoke returned HTTP 200 for
state, summaries with/without diagnostics, URL inventory, analysis and feature
status while outbound HTTPX was forbidden. Static checks and `pip check` passed.

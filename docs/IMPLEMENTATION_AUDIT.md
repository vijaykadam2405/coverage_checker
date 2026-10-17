# CrawlScope implementation audit — 8 October 2026

This is a historical implementation record. The 11 October update replaces staged
dashboard verification with automatic serial per-URL processing; see
[current workflow review](SERIAL_WORKFLOW_REVIEW.md) and the root README for current
pacing, configuration, frontend actions and tests.

## Baseline and approach

Read the active Python crawler, storage, discovery, validation, corpus, reporting,
services and API sources; React entry, hooks, services, pages, components, config,
utilities and styles; launcher and verification scripts. Traced imports and callers.
Baseline before edits: **131 Python tests passed (64.34s), 11 frontend tests passed**.
One upstream Starlette/httpx deprecation warning. No production audit or load test.

Applied the official [Ponytail skill](https://github.com/DietrichGebert/ponytail/blob/main/skills/ponytail/SKILL.md):
preserve existing capabilities, reuse SQLite/FastAPI/React, avoid new dependencies,
and verify nontrivial behavior. Preserve runtime databases, reports and credentials.

## Active call graph and findings

`frontend/scripts/start.mjs → dashboard.py → web/api.py → services/jobs.py →
auditor.py → discovery + validation + corpus/coverage_api.py → storage/database.py
→ analysis + reporting`. CLI: `audit.py → main.py → auditor.py`.

- Reuse independent static/rendered/API/feed/sitemap/filter/pagination discovery.
- Preserve two durable stages and explicit Corpus check, backoff, resume and secrets.
- Replace full-inventory polling and client filtering with bounded SQL endpoints.
- Separate verified coverage, completion and lower bounds; N/A is not zero.
- Replace the capture-only modal with an operational, persisted Control room page.
- Replace historical Corpus text-quality claims with live HTML extraction health.
- Keep offline HTML/Excel reports, fixtures and safety boundaries.

## File classification

| Area | Classification / action |
| --- | --- |
| audit.py, dashboard.py, main.py | Active entry points, retained |
| auditor.py, config.py | Active orchestration/configuration, extend existing behavior |
| discovery/browser, evidence, filters, max_page, pagination, resources, sitemap, static_links, tabs | Active discovery, retained and wired to controls |
| validation/* | Active validation/session handling, retain and add network safety |
| storage/* | Active persistence, additive migration/indexes only |
| services/*, web/api.py | Active, refactor read paths and API contracts |
| analysis/coverage, dashboard, diagnostics, inventory, inventory_segments, gaps, pagination_gaps, regression, root_cause | Active calculations/reports, preserve and correct semantics |
| reporting/* including templates/coverage.html | Active offline outputs, retained |
| frontend/src/* | Active React UI, refactor against bounded API |
| tests/*, frontend/tests/* | Tests, migrate obsolete integration tests and add coverage |
| scripts/preview_react.py | Active visual QA helper, update for page navigation |
| discovery/archives.py, documents.py, url_patterns.py, analysis/depth_analysis.py, filter_gaps.py | No active callers; removed after reference verification (section E) |
| corpus/dashboard_client.py, corpus_browser.py, corpus_search.py, content_compare.py, url_match.py | Earlier integration/test-only paths; removed after verifying active replacement tests |
| legacy/streamlit, legacy/corpus_verification.py | Obsolete UI/integration sources removed after reference verification; ignored compiled caches remain |
| data/, reports/ | User runtime data, never delete as code cleanup |
| .venv/, node_modules/, dist/, caches | Generated dependencies/artifacts, excluded from source; keep installed dependencies needed locally |

## A. Completed refactor

The old React hook repeatedly requested a full run snapshot, including every URL,
detail and analysis. Inventory filtered that array locally. The UI represented one
selected scan, mixed coverage/completion, and put capture switches in a modal.

Kept: SQLite/FastAPI/React, independent crawler, existing safety/retry/session/robots,
deep navigation, filters/tabs/API/sitemap/pagination, explicit durable verification,
read-only API client, protected credentials, offline reports, single-port launcher.
Refactored: shared metrics/status translation, SQL read endpoints, host/type identity,
UI composition, lightweight polling, bounded activity, real progress, full Control room.
Strengthened: MIME precedence, IPv6 normalization, actual browser text evidence,
pagination uncertainty/probe evidence, additive host/source indexes, public-target
egress guards, compatibility settings migration, historical note classification.

No new runtime dependencies, Redux, chart library, WebSockets, service/database
replacement or production-data deletion. No production crawl or Corpus load test.

## B. Requirement matrix

Paths below are relative to the project root; grouped references identify existing
responsibilities, not parallel implementations.

| Requirement | Implementation / files | Verification |
| --- | --- | --- |
| 1 Complete audit | Active source read; call graph above and SOURCE_MAP.md | Baseline + reference search before deletion |
| 2 Preserve discovery | auditor.py; discovery/browser, evidence, filters, sitemap, static_links, tabs, pagination | browser_discovery, content_tabs, filters, pagination, end_to_end, two_stage_workflow |
| 3 Evidence accuracy | analysis/states.py, coverage.py; services/queries.py | monitor_queries metric/filter/detail tests; crawlscope_accuracy; coverage_api |
| 4 Scalable real-time | services/queries.py, web/api.py; hooks/useDashboard, useQuery | 20k fixture, API and browser request assertions |
| 5 Multiple websites | SQL latest target grouping; Overview WebsiteWorkspace | latest_target_sites_are_not_history_or_resource_hosts |
| 6 Compact metrics | Overview, Metric, config/capture; SQL type counters | browser HTML/PDF counts; capture_selection; MIME cases |
| 7 Coverage formulas | Shared analysis/coverage.metrics and SQL counts | metrics_do_not_conflate_verified_coverage_and_completion |
| 8 Honest progress | coverage/ScanProgress, query counters, auditor state | actual counters; no fake discovery denominator; browser review |
| 9 HTML extraction health | SQL text lengths/fingerprints/median; Overview/Analysis | extraction_health test; unavailable vs empty tests |
| 10 Live URL activity | latest 20 server records; Overview filters | bounded live endpoint and browser checks; no snapshot URL transfer |
| 11 URL inventory | SQL filters/pages/sort, Inventory, lazy detail, streaming CSV | paginated_filters_details_and_full_filtered_export; query unit + browser |

Additional task sections:

| Sections | Implementation / verification |
| --- | --- |
| 12 Detailed evidence | Existing UrlEvidenceDialog reused, per-URL API, live/Corpus metadata and clear URL-only disclaimer; API + Escape/focus tests |
| 13 Visual analysis | Accessible donut/stacked bars, counts, depth, source/outcome/host charts, conditional evidence tables, manual refresh; four-breakpoint browser QA |
| 14 Readable activity | Cursor/severity/stage/event filtering, duration and real counters, issue/impact/continuation/action, whitelisted structured details |
| 15 Control room | First-class grouped page; persisted validated settings passed into AuditConfig; active-job lock; no empty capture, schema v2 migration; API/control/browser tests |
| 16 Discovery / maximum | Existing independent methods retained; probe safeguard not a claimed maximum; observed vs verified/probe table; 487-page, repeated/bounded/rate-limit/non-monotonic tests |
| 17 Status model | Shared states.py labels/eligibility, common React badges; website, Corpus and text states stay distinct |
| 18 Resource classification | MIME-first subtype evidence, separate API/feed/media capture; unknown binary remains unknown; URL-only and capture tests |
| 19 Database / performance | Additive migration/indexes/read snapshots; compact SQL aggregates and lazy analysis; old-schema and scale tests |
| 20 Website data model | Existing target_url reused, SQL latest-target grouping; no new workspace relational hierarchy |
| 21–22 UI / overview architecture | Compact workspace and selected-run hierarchy, understandable counters, process, coverage, live activity, boundaries |
| 23–24 Responsive / accessible | Four breakpoints, local table scrolling, labels, tab keyboard controls, inert closed mobile navigation, Escape/focus restoration |
| 25 Security | HTTPS Corpus-only credentials; public-target/redirect/browser guards; local origin/writer/report-path protections |
| 26 Error handling | Failures remain unverified; bounded recovery and saved progress, readable explanations and historical-note separation |
| 27–29 Cleanup / structure / responsibilities | Proven dead-source removal, tests migrated to active contracts, old CSS removed, source map and active structure below; no runtime records deleted |
| 30 Automated tests | Python regression suite; frontend unit checks; isolated Chromium UI fixture with no production requests |
| 31 Performance | 20,000 URLs, 10 hosts, 8 underlying type examples, 5,000 events, 2 comparable historical runs and 3 other sites; measured below |
| 32 Visual QA | 1600/1366/820/390 across 5 views, 20 screenshots; no document overflow/page errors; keyboard/Escape/dialog focus |
| 33 Targeted files | Existing App/pages/hook/API/config/auditor evolved, not greenfield rewrite |
| 34 Prohibited changes | No fabricated data in runtime, no robots/access bypass, no private targets, no missing labels for errors, no PDF/image parsing |
| 35 Acceptance | Features exercised by named suites above; limits remain explicit rather than promising a complete website inventory |
| 36 Deliverables | A–I here, README and SOURCE_MAP.md |
| 37 Execution order | Baseline and code tracing before backend changes; backend then views/control; regression/visual; reference cleanup; documentation; final rerun |

## C. Data definitions

All inventory aggregates exclude traversal-only rows (`capture_enabled=0`).
Discovered = selected deduplicated saved URL candidates, including failed candidates.
Eligible = VALID_PAGE HTML/API/feed or URL_ONLY document/image/resource rows.
URL_ONLY denotes collection, **not HTTP/content validation of the remote file**.
Failed = measured ineligible outcomes, excluding render-required/pending validation.
HTML inventory counts valid HTML only. PDF/document/image/media counts eligible
URL-only file links. Other includes unclassified downloads/archives. API and feed
metadata may be fetched for navigation; their bodies are not Corpus completeness.

For each selected category/host/scope, let eligible E, confirmed found F, confirmed
missing M, checked C=F+M, unverified U=E−C:

| Metric | Formula / empty handling |
| --- | --- |
| Verified coverage | 100F/C; null/N/A if C=0 |
| Verification completion | 100C/E; null/N/A if E=0 |
| Lower coverage bound | 100F/E; null/N/A if E=0 |
| Upper coverage bound | 100(F+U)/E; null/N/A if E=0 |
| HTML extracted-text rate | valid HTML text_length>0 / all valid HTML ×100; N/A if none measured |
| Median characters | median of measured valid HTML text lengths, including zero |
| Empty HTML text | measured valid HTML text_length=0; NULL is separately unavailable |
| Render required | recorded RENDER_REQUIRED HTML outcome |
| Repeated fingerprint groups | count of valid HTML fingerprint groups with more than one URL; not proven duplicates |
| Deepest navigation | MAX recorded navigation_depth; NULL means unknown, never min_depth substituted |
| Discovery progress | indeterminate until the discovery phase ends; no invented percentage |
| Corpus progress | C/E ×100; explicit unverified remainder |
| First-source composition | disjoint first_discovery_method counts; old missing provenance remains unknown |
| Also-source composition | every recorded discovery method; deliberately non-additive |
| Website totals | latest run per normalized target hostname (www equivalent); not discovered resource hosts/history sums |
| Boundaries | actual limitation records after excluding historical document/script diagnostics |

Main donut = HTML only. Global verification includes all selected types including
images. Offline compatibility summary excludes images and says so; its coverage
also uses confirmed checks, not an unlabeled lower bound. Group analyses include
eligible selected resources, with HTML-only real navigation depth. Archive years
are URL-inferred evidence, not guaranteed publication dates. Bounds describe only
the observed inventory and cannot bound unseen pages.

Corpus article bodies are unavailable through /v1/held. New runs produce no Corpus
body comparison or document text extraction. Historical text evidence remains in
the database/detail/offline report, labeled historical/not provided by current API.

## D. Active production structure

```text
audit.py / dashboard.py                       CLI / local API entry points
crawler_coverage_auditor/
  main.py, config.py, auditor.py
  analysis/                                  coverage, dashboard, diagnostics, states,
                                             inventory, inventory_segments, gaps,
                                             pagination_gaps, regression, root_cause
  corpus/                                    coverage_api, credentials, errors
  discovery/                                 browser, evidence, filters, max_page,
                                             pagination, resources, sitemap,
                                             static_links, tabs
  validation/                                access, fingerprints, http, network_safety,
                                             normalize, site_session, soft404, text_extract
  storage/                                   database, models
  services/                                  inventory, queries, jobs
  web/                                       api
  reporting/                                 html, excel, templates/coverage.html
frontend/
  src/main.jsx, App.jsx
  src/pages/                                 Overview, Inventory, Analysis, Activity,
                                             ControlRoom
  src/components/{common,layout,coverage,inventory,scan}/
  src/{config,hooks,services,utils,styles}/
  scripts/start.mjs, index.html, vite.config.js, package.json, package-lock.json
```

Each Python package retains __init__.py. Tests/docs/scripts are support, not a second
runtime. Dependencies/builds/caches are generated and ignored. `data/` and `reports/`
are user runtime data, preserved. `services/inventory.list_runs` remains a tested
compatibility/report helper; live polling uses the bounded `queries.history` path.

## E. Deleted source files and replacement verification

- corpus/dashboard_client.py, corpus_browser.py, corpus_search.py, url_match.py:
  no active callers; obsolete dashboard search/auth/browser path. Replacement:
  coverage_api tests for authoritative results, variants, malformed/omitted results,
  no fallback, timeouts, semantic errors, bounded retries and serialized pacing.
- corpus/content_compare.py: unused legacy body comparer. Current API has no bodies;
  stored historical results remain readable. Replaced assertions with live unavailable
  vs empty states and existing resource/historical detail metrics tests.
- discovery/archives.py, documents.py, url_patterns.py; analysis/depth_analysis.py,
  filter_gaps.py: no active imports. Actual archive/resource/depth/filter work remains
  in active shared helpers and auditor, covered by regression suites.
- legacy/corpus_verification.py, legacy/__init__.py; legacy/streamlit/{app.py,
  views.py,__init__.py,README.md,requirements.txt,.streamlit/config.toml,
  tests/test_app.py}: obsolete UI/integration; replaced by API/React control/browser tests.
- tests/test_dashboard_tls.py, test_corpus_recovery.py, test_content_compare.py:
  tests of deleted paths, replaced by active API/TLS/recovery/text evidence coverage.
  Legacy-only tests inside two_stage_workflow and url_only_resources migrated/removed
  after the active API equivalents were verified; meaningful fixture tests retained.
- frontend/src/components/control-room/ControlRoom.jsx: old modal replaced by page.
- frontend/src/utils/inventory.js and frontend/tests/inventory.test.js: obsolete
  client-side filtering/CSV; replaced by backend CSV/filter tests and actual server-query
  unit tests. Removed unused step/type-bar/text-quality/timeline/modal/control-nav CSS.

Generated legacy .pyc cache cleanup was rejected by environment policy. Those ignored
caches/empty directories remain; no alternate deletion bypass was attempted. Installed
.venv/node_modules and built dist remain needed locally and are not source. No saved
audit, production database, historical report or credential was deleted by this cleanup.

## F. Database changes

Additive nullable urls.host, first_discovery_method and discovered_at fields. Existing
host values are backfilled from saved normalized URLs; historical first sources and
discovery timestamps remain NULL, not reconstructed guesses. Added run/state,
run/host/type, run/depth, match-state, validation/HTTP, event cursor/severity and target
indexes. Existing rows remain compatible. Pagination LIMITED/UNVERIFIED updates clear
a previously verified maximum; probe records persist website outcomes/fingerprints.

Read-only URI connections use coherent SQLite read transactions. Older schema read
paths support missing optional columns. AuditDatabase upgrades are additive and tested
on an old fixture. No record wipe, destructive rewrite or migration of Corpus itself.
Control JSON v1 OTHER is upgraded to separate API/feed/media selection without losing
the prior selection. Schema v2 validates all persisted numeric/enum settings.

## G. Local dashboard API

| Endpoint | Contract |
| --- | --- |
| GET /api/state | compact last-100 run identities, active job, saved controls, key configured boolean, labels; no keys or URL rows |
| GET /api/sites | latest run per distinct target site, compact SQL type/HTML/global metrics, latest event |
| GET /api/runs/{id}/summary | selected scan state, aggregate counts/types/hosts/health/progress, latest event, latest 20 real boundaries, historical-note count, session-file availability boolean |
| GET /api/runs/{id} | compatibility alias for summary; no full inventory |
| GET /api/runs/{id}/urls | page default1/page_size default50/max100; search, host, type, corpus_status, validation_status, depth, method, http_status, sort/order; items/total/pages/scoped summary |
| GET /api/runs/{id}/urls/{url_id} | lazy single URL detail, lineage/HTTP/live HTML/Corpus metadata, contextual boundaries |
| GET /api/runs/{id}/urls/export | streamed full filtered CSV, spreadsheet-formula escaping, fixed safe filename |
| GET /api/runs/{id}/activity | limit default80/max200, after_id, severity/stage/kind; items/cursor/has_more; structured whitelisted details |
| GET /api/runs/{id}/live-urls | latest20; type/host/errors/missing filters, no full inventory |
| GET /api/runs/{id}/analysis | expensive on-demand saved-evidence analysis, no per-URL detail array; bounded raw evidence tables, comparable history |
| PUT /api/settings | validated full settings, atomic local persistence, 409 while a job is active |
| POST /api/discover | target URL; saved controls consumed by worker, returns run_id; optional compatible capture override |
| POST /api/runs/{id}/verify | explicit API-only check/resume of saved discovery inventory |
| POST /api/runs/{id}/stop | cooperative current-job stop; results remain persisted |
| GET /api/runs/{id}/reports/{html,xlsx} | fixed reports under configured report directory |

Filters use parameterized SQL, sort columns are whitelisted. `all` means all selected
candidates (including failures); `eligible` separately means valid/URL-collected.
Missing is confirmed NOT_FOUND only; unverified includes pending/review. Literal %/_
in search are escaped, not wildcards. Depth `unknown` means NULL navigation depth.

Polling: state/summary 2s active, 5s idle; latest URL reads 2s active/5s idle;
Inventory only the selected 50-row page (3s active); Activity incremental events
(3s, quicker bounded catch-up); Analysis entry/manual refresh only. Reads never
start discovery or verification. Trusted local host/origin/write-header restrictions,
no-store headers and bounded file paths are preserved. This is a local desktop service,
not a public multi-user deployment/authentication product.

## H. Verification and scale results

Baseline: 131 Python tests and 11 frontend tests passed before changes. Tests were
migrated from obsolete integrations, so final counts reflect active behavior rather
than keeping dead code solely to preserve a count.

Full regression after implementation/cleanup: **145 passed**, one upstream
Starlette/httpx deprecation warning, 63.59s. Fresh-build browser/query rerun:
**31 passed**, 12.36s; includes active discovery's indeterminate progress, exact
75% verification progress, locked controls and keyboard tab/mobile drawer checks.
Frontend `run check`: formatting passed, **11 tests passed**, Vite build succeeded.
Bundle: JS280.92KB (gzip87.84KB), CSS23.91KB (gzip5.99KB). The final whole-suite
confirmation after the last source changes is recorded below.

Final complete `python -m pytest -q --tb=short`: **145 passed, 1 warning in
66.80s**. Additional final host/search scale assertion: **1 passed in 4.82s**.
No skipped browser checks. The warning is upstream Starlette/httpx test-client
deprecation, not a CrawlScope runtime error. No dependency upgrade was introduced
solely to suppress it.

Chromium exercised all 5 views at 1600/1366/820/390 widths: 20 screenshots, zero
page JavaScript errors or document horizontal overflow. Tables scroll inside their
containers. Verified settings persistence, 4 selected capture types, HTML vs PDF
counts, missing vs failed, server queries/CSV, lazy detail, Escape/focus restoration.
Browser fixture never starts a discovery/Corpus job. Screenshots in reports/ui-review
are **isolated fixture screenshots**, not claimed production scan results.
Those generated project screenshots were subsequently removed during cleanup.
Current browser tests save screenshots in pytest's temporary directories; the
preview helper also defaults to a temporary directory outside the project.

20k fixture: 20,000 URLs, 10 hosts, HTML/PDF/document/image/API/feed/media examples,
5,000 events, two comparable older runs, three other target sites. Local full HTTP
response timings (not production SLA; no target/Corpus network):

| Endpoint | Time ms | Payload bytes |
| --- | ---: | ---: |
| state | 8.66 | 4,735 |
| sites | 421.27 | 11,803 |
| summary | 509.11 | 4,291 |
| urls (50 of20k, including host-scoped summary) | 393.23 | 46,850 |
| host + literal search (50-row page) | 162.66 | 46,805 |
| activity (80 events) | 24.23 | 17,241 |
| analysis (on demand) | 2,102.32 | 19,900 |

Summary contains no rows/details/whole-run analysis. Inventory returns 400 pages,
not 20k browser rows. Expensive analysis is not repeated by 2-second polling. Query
ceilings are generous local regression checks (<10s), not throughput promises.

Read-only compatibility check against the existing runtime database succeeded for
both saved runs using the new summary queries. The database file remained 16,322,560
bytes with its pre-existing 7 October modification time; no production audit, network
verification or record rewrite was performed. Historical combined resource coverage
is not substituted for the prominently labeled HTML-only coverage donut.

## I. Honest remaining limits

- A website can contain disconnected/unlinked/private/unsupported states. No finite
  inventory proves every possible page exists or was discovered.
- Robots, login, CAPTCHA, rate limits and network failures remain disclosed boundaries;
  safe handling is not a guarantee of unrestricted access.
- Numeric boundary search assumes contiguous monotonic pagination. Observed
  contradictions are rejected; exhaustive proof of all untested page numbers is not
  possible with binary probing. Opaque API cursors may have no provable maximum.
- DOM-only filters may expose links but no independently verifiable URL state.
  Website-specific adapters may be needed for unsupported controls.
- Files are URL-only. Broken/unavailable PDF/document/image links can still be collected;
  there is deliberately no download, OCR, extraction or file-content guarantee.
- Coverage API supplies URL presence/metadata only, not HTML body completeness.
- Why production missed a URL is a hypothesis until production logs/configuration
  corroborate it. CrawlScope cannot diagnose HDD saturation from HTTP timeouts alone.
- Historical first-source/date/navigation evidence may be missing; UI shows unknown.
- DNS checks are pre-request, not transport-pinned. Harden outbound egress for hostile
  DNS-rebinding environments. Run locally; do not publish this desktop API unauthenticated.
- Source cleanup preserved runtime data; ignored legacy cache deletion was denied.
  Existing running processes need a user-controlled restart to load changed modules.

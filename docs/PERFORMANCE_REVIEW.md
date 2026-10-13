# CrawlScope performance and cleanup review

Review date: 13 October 2026 (Asia/Calcutta).

## Outcome and scope

The current running service was confirmed to use serial automatic scanning, pacing
version 2, one-second website spacing, two-second Corpus spacing, and no fixed
discovery count caps. Its active inventory was #3, news.lockheedmartin.com.
The changes below are saved and tested, but an already-running Python process must
be restarted to load them. The active scan was not interrupted or restarted.

Reviewed the first-party execution path, storage, discovery, browser readiness,
Corpus validation/text comparison, report analysis, API queries, React polling,
startup/configuration and import/dependency usage. This is a targeted correctness
and bottleneck review, not a claim that every optional production-crawler feature
was executed or that third-party dependencies were audited line by line.

## What the scan actually does

For each queued URL, it retrieves public website HTML or records a file link,
discovers children, explores permitted rendered controls, checks URL presence
through Corpus, compares HTML text where available, saves the result, then moves
to the next URL. Pagination probes use this same serial pipeline. A website/Corpus
request can wait for pacing, retry/backoff, DNS, response headers/body or rendering.
These are different from unnecessary CPU/database work.

Selected PDFs, images and documents remain URL-only. Their bodies and text are
not downloaded by CrawlScope. HTML can reference many URLs without each URL being
a distinct article. In the examined inventory, 15,379 URLs had FILTER_GET as their
first discovery source. The selected inventory had 20,000 URLs and the database
had 20,002 records including two traversal-only URLs at the measurement point.
Those values are a snapshot of an active scan, not fixed ceilings or final counts.

## Measured bottlenecks

| Area | Evidence | Change |
| --- | --- | --- |
| Filter analysis | 9,896 definitions / 89,064 values. Four control names occurred on 2,474 source pages each, pointing at one shared action. The old analysis searched all filter URLs for every control. | Index URL states by location and parameter. Cache equivalent group counts, preserving each control's source and all values. |
| Local disk writes | A controlled 250-URL fixture formerly committed each URL and event separately: 500 commits. | Short checkpoints: the same URLs, edges and events use 5 commits. |
| Repeated filter work | The HTTP and rendered copy of the same form could repeat identical writes/events. | Reuse only exact controls on the same normalized source URL. Changed values, defaults and different source/dependency URLs are processed. |
| Snapshot loading | Reporting/verification loaded cleaned HTML snapshots even for files, pending pages, or pages with publication evidence already stored. | Indexed lazy lookup for historical article dates only. Verification/render selection avoids archive enrichment entirely. |
| Live dashboard polling | Recent URL activity called inventory pagination, recalculating two full-inventory aggregates although it only displayed recent rows. | Query just the requested recent rows. Full inventory counters remain available through their normal endpoint. |
| General inventory queries | An unfiltered/host-only query calculated equivalent scoped and host totals twice. | Reuse the same totals when the filters are identical. |
| Browser script notes | Several script warnings could cause the same rendered HTML to be text-parsed repeatedly. | Parse it once and reuse that result for all notes and the saved HTML snapshot. |
| Audit history analysis | Comparable-history metrics also loaded unrelated extraction health, boundaries and host tables. | Use the existing compact summary path for historical metrics. |

Saved event timings from the active service gave these approximate medians:

| Operation | Samples | Median | Maximum |
| --- | ---: | ---: | ---: |
| Website fetch and validation | 56 | 2.005 seconds | 2.754 seconds |
| Browser rendering through saved browser recovery | 53 | 8.825 seconds | 10.320 seconds |

These are event-to-event elapsed times, not pure CPU/network measurements; the
browser measurement includes saving discovered evidence. This sample contained
no complete paired text-check events, so it does not establish current Corpus
latency. An earlier sample showed text checks continuing, but its timings are not
substituted for a current measurement.

The previously slow filter-analysis profile was cancelled rather than allowed to
continue consuming CPU. With indexing, a read-only profile of all 20,000 selected
URLs and all 9,896 controls completed in about 7.0 seconds **including profiler
overhead**. It retained all source definitions. This is not a full-scan benchmark.

## Controlled before/after checks

The existing synthetic 20,000-URL fixture exercises the same local API query paths.
Illustrative measured timings on this desktop:

| Query | Earlier separate run | After changes |
| --- | ---: | ---: |
| First inventory page | 1.415 seconds | 0.285 seconds |
| Filtered inventory page | 0.465 seconds | 0.196 seconds |
| Analysis | 7.599 seconds | 2.265 seconds |

These are single local measurements under different background load, not a service
level guarantee or a claim of equal speedup in website navigation. Correctness
tests additionally prove that 500 equivalent controls need four count operations
(one shared group plus three area assessments), rather than recounting each group.

## What was deliberately preserved

- Every eligible newly discovered URL; no count/depth ceiling was reintroduced.
- Sequential website/Corpus processing, one URL per Corpus request.
- Two-second minimum Corpus spacing, adaptive slowdown and Retry-After handling.
- All filter values, rendered content, same-page tabs and pagination evidence.
- URL normalization, unique keys, source methods, navigation edges and depth.
- Strict API result validation. A timeout/unavailable text is not a confirmed miss.
- Read-only Corpus access and HTML-only text comparison; resources remain URL-only.
- Robots policy, TLS checks for target sites, access boundaries and trap detection.
- Existing inventories, successful matches, snapshots and historical reports.

The checkpoint size of 100 storage operations is **not a collection limit**. It
flushes work periodically while discovery continues. No batch transaction is held
across an asynchronous website/browser/Corpus operation. On failure, earlier
checkpoints are durable, the current chunk rolls back, and the source stays
FETCHING/RENDERING so resume can safely rediscover that page's links. Tests exercise
these interruption cases and normal immediate writes outside a discovery batch.

## Why a large news website can still take hours

Browser exploration is real work. Thousands of pages can each have tabs, filters,
background requests, lazy links and older pagination. They were not disabled just
to improve a speed number. Presence and HTML text are currently separate Corpus
API calls, on the same paced lane.

For example, 10,000 fresh HTML URLs with 80% found in Corpus need about 18,000
requests before alternate URL checks or retries. At two-second minimum spacing,
the request gaps alone are about ten hours, with actual completion also affected
by responses, website exploration and failures. Duplicate local database writes
are now cheaper, but changing those does not remove this deliberate API budget.

A future Corpus-side endpoint that returns presence **and** stored HTML text in
one read could avoid the second request on matched HTML. That requires the Corpus
owner to implement/authorize a contract and evaluate server load. No unsupported
endpoint, bulk checks, parallel requests, local Corpus dump or increased production
traffic was introduced in this review.

## Configuration, dependencies and running-code checks

- `pip check`: no broken requirements.
- Frontend dependency/build inputs unchanged; installed tooling builds successfully.
- The live API confirmed configured Corpus access and current capture/pacing settings.
- Optional `.env` variables may be absent because defaults/protected local credentials
  are used. No keys, passwords, cookies or `.env` secret values were printed.
- Backend source revision is recorded at startup. Source changes cause a restart
  notice in the dashboard and launcher. Only first-party Python source is fingerprinted;
  credentials, databases and reports are excluded. Content hashing is cached while
  metadata is unchanged and the monitoring check is throttled to five seconds.
- No dependency upgrades were made merely to claim the newest version. The existing
  test-client deprecation notice and untouched imported-source syntax warning are
  not scan failures. The vendor source integrity test remains intact.

## Cleanup performed

Removed unused auditor logger/robots/count state, redundant resource-type rewrites,
duplicate row conversions, and the unused document-metadata updater and legacy
HTTP file-text option. Backward-compatible saved configuration/CLI interfaces were
retained. Every remaining first-party import passed an AST usage check; entrypoints,
tests, reports and optional imported features are not treated as disposable merely
because they are outside the default live scan.

The obsolete `legacy/` directory contained only five compiled Python cache files;
`.streamlit/` contained no files. Both were removed from the project root by moving
them into `data/cleanup-20261013-performance/`. They can be recovered from there.
No actual source, scan record, credential, report or imported vendor file was deleted.

## Verification and activation

After the implementation: 299 Python tests passed; 15 frontend tests passed; the
React production build succeeded. Performance/interruption tests cover grouped
commits, durable checkpoints, exact filter counts/provenance, changed filter
dependencies, lazy snapshots, live polling, source state and revision detection.

To activate backend changes without losing data:

1. Pause the scan in CrawlScope and wait until saving/stopping finishes.
2. Stop its backend terminal with Ctrl+C.
3. From the project directory run `npm.cmd --prefix frontend start`.
4. Refresh the browser and resume the same saved inventory.

The frontend has already been rebuilt. Do not kill an active writer or open another
dashboard on a different port as a workaround. A refreshed browser alone cannot
reload Python modules in an already-running server.

## References

SQLite explains the overhead of individual commits and the benefit of grouping
inserts in its [FAQ](https://www.sqlite.org/faq.html#q19). Its
[query-plan documentation](https://www.sqlite.org/eqp.html) explains index scans and
temporary sorting trees. Playwright discourages generic network-idle waits in its
[page reference](https://playwright.dev/python/docs/api/class-page#page-wait-for-load-state);
CrawlScope's existing content-readiness tracking remains in use.

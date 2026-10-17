# CrawlScope

Independent public-website discovery and evidence-based production crawler coverage.
The frontend is React/JSX/CSS. Python runs the crawler, SQLite persistence and local
FastAPI service; it does not render the live dashboard.

## Run on Windows

From this project directory, first-time setup:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\setup_local_browser.py
.\.venv\Scripts\python.exe -m playwright install chromium
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run build
npm.cmd --prefix frontend start
```

Later starts:

```powershell
cd "C:\Users\vijay\Desktop\coverage checker"
npm.cmd --prefix frontend start
```

Open [CrawlScope](http://127.0.0.1:8000). The launcher reuses an existing CrawlScope
server on port 8000 and refuses to replace unrelated services. After source changes,
rebuild the frontend and restart the backend to load changed Python modules. Stop
your old server with Ctrl+C in its terminal first; rebuilding does not restart it.
Direct alternative: `.\.venv\Scripts\python.exe dashboard.py`.

The backend now records its loaded source revision. If Python source changes while
it is running, the dashboard and launcher show restart guidance instead of silently
claiming to use the new code. Pause an active scan and wait for it to save before
stopping its server. Restart on the same port, then resume the saved inventory.
The API also rejects new/resumed scan work until a stale backend is restarted;
reading saved results and safely pausing an active scan remain available.

For development only, run the backend alongside
`npm.cmd --prefix frontend run dev` (Vite port 5173, API proxy to 8000).
Normal use requires only port 8000. Do not run a CLI audit and dashboard as
simultaneous writers to the same database.

## Before committing or pushing

Install the development checks once, then run the release check:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts\prepush_check.py
```

The publication gate checks current files and the current branch's full history.
Use `--all-history` for a separate audit of every local branch and tag. Push only
the reviewed branch, not `--all` or `--mirror`; unrelated backup branches are not
part of a normal single-branch push.

This checks publishable tracked/untracked files and reachable Git history for common secret patterns and
runtime data, dependency consistency, Python tests/static checks, frontend
formatting/tests/build, and Git whitespace. It does not stage, commit, push, start
scans or query production Corpus. Use `--repository-only` for the quick publication
check. Pattern-based scanning is not a guarantee that no secrets exist. If a
historical credential is flagged, revoke/rotate it and explicitly review history
cleanup before publishing; deleting it from the latest file is insufficient.
The Windows GitHub Actions workflow repeats the checks without a
production API key or database; tests use temporary local fixtures/mocked Corpus.

## Workflow

1. In **Control room**, select what to capture and enter the Corpus API key if
   needed. Capture choices are persisted locally and locked while a scan is active.
   Browser rendering, pagination, filters, metadata discovery and Wayback run
   automatically. Existing capture choices and intentional slower pacing are preserved.
2. In **Overview**, enter a public website and click **Start website scan**.
   Each URL is processed serially: discover/validate → browser discovery →
   Corpus presence → HTML text comparison → next URL. There is no separate Corpus
   Check button. A configured key is required before starting a dashboard scan.
3. Website requests are spaced at least 1 second apart per host. Corpus
   requests contain **one URL**, are serialized, and are spaced at least 2 seconds
   apart after responses; slow reads/rate limits increase the delay. Selected
   file links are never downloaded or text-extracted.
   These defaults reduce deliberate waiting from the previous 1.5/3-second
   website/Corpus intervals without parallel workers or skipped discovery/text checks.
4. If scanning pauses, completed checks remain saved. **Resume scan** continues
   unresolved checks before fetching the next queued page. Saved successful
   website snapshots and comparisons are reused, even if their Corpus cache age
   exceeds 24 hours; resume is recovery, not a full freshness refresh. Failed or
   unfinished checks and changed saved HTML text are still handled before the
   next URL. Explicit saved-inventory verification retains the configured
   freshness policy. **Upgrade saved scan** adds the
   new automatic discovery work; a Wayback-only upgrade does not repeat completed
   HTML work. **Update saved analysis** refreshes Corpus comparison from saved
   evidence. A timeout, unavailable service, malformed result
   or incomplete response never becomes a confirmed crawler miss.

**Overview** places six selected-website counts above the page title: valid HTML,
missing HTML, PDF links, missing PDF links, document links and all discovered URLs.
It shows current scan activity, per-type Corpus coverage and the latest 20 URL records.
The removed extraction-health section is not a Corpus text-completeness measure.
Saved websites are a searchable clickable table, not a dropdown; each opens its latest
inventory. Older runs remain accessible through the selected website's Saved scan history.
Discovered CDN/resource hosts are not separate tested websites.

Dashboard summary reads poll automatically (about two seconds during a scan).
The `diagnostics=false` query parameter skips the extra extraction-health query;
it is not a failure flag and does not disable counts or saving. Detailed Analysis
refreshes local saved evidence every 15 seconds while the selected scan is active,
and refreshes once more when it becomes inactive. Completed inventories remain
entry/manual-refresh only. These UI reads do not start scans or query Corpus.

**URL inventory** is a read-only stored-data view, without a scan form or type-filter tabs.
It defaults to all captured types and uses server-side search, host/status/depth/source/HTTP filters,
sorting and 50-row pages. Click a row for evidence. CSV export streams every
matching record, not just the displayed page.

**Text coverage** displays existing saved per-page comparison classifications and
token-set similarity scores when both texts were available. That historical score
is similarity, not an exact percentage of article text captured. The updated
Corpus API supplies stored HTML text through `/v1/text`. New comparisons measure
**estimated distinct-live-word coverage**, plus beginning/middle/ending coverage.
The original token-set similarity is retained as diagnostic evidence. Unavailable,
truncated or non-HTML stored text has no completeness percentage. Different snapshot
dates can cause differences that are not necessarily production-crawler defects.
Non-HTML resources remain URL-only. Activity periods use whole elapsed days
(e.g. `2d ago`; less than one day is `0d ago`) with exact timestamps in tooltips.

Target inventories exclude external hosts for all content types, including PDFs
and images. The saved target's host aliases and configured subdomains define the
scope; a sibling host is not automatically included. External scripts and Wayback
can still provide discovery evidence, but their own URLs are not target pages.
Target HTTP/browser navigations cannot follow redirects into another host's scope.
File links receive paced HEAD-only validation: final response MIME determines
their type; no document body or text is downloaded. A PDF-looking link returning
HTML is processed as HTML only when it stays in scope. Unsupported HEAD, missing
MIME, blocks and errors stay unverified, not confirmed files or crawler misses.

**Analysis** loads on entry or manual refresh, not every polling interval. It shows
coverage by type and depth, source composition, validation outcomes, host counts,
pagination maxima/probes/missing ranges, filters, archive years, sitemap/API evidence
and comparable history. Empty sections are omitted; tables retain exact values.

The coverage-by-area table shows only Area, Valid URLs, Found, Missing,
Unverified and Coverage %. Pagination, filters and archives count unique URLs,
not controls, clicks or years. URL patterns and learned site-specific
filter names contribute even when their original discovery tag was static/API.
Numeric maxima require saved boundary proof; cursor/offset URLs do not imply
a verified last page. DOM-only filters and same-page tabs are not separate URLs.
N/A means unavailable measurement, not zero coverage or proof of no website content.
Areas overlap and must not be summed. Archive years use URL dates or website
publication metadata. Existing snapshots can recover a displayed article date
only when it immediately precedes a corroborated article heading; footer years,
incidental dates and Corpus capture dates are excluded. Browser pagination/filter
actions retain their discovery tags only for independent URL changes, not
same-page sections. Coverage uses confirmed found / (found + missing).

Refresh an inactive saved inventory's HTML/Excel reports after reporting updates,
without rediscovery, Corpus requests or database writes:

```powershell
.\.venv\Scripts\python.exe scripts\refresh_reports.py --run-id 1
```

Use `--output` for a separate export directory. Active scans must finish or pause
before refreshing their report files. Restart the backend after an active scan
finishes to load changed analysis modules; rebuilding React does not restart it.

**Activity** loads bounded incremental saved events with filters and plain-language
failure impact/continuation/recommended actions. Historical script/document notes
are separated from current coverage boundaries, not erased.

## Pause and continue from a checkpoint

Click **Pause scan**, wait until **Saving & stopping…** finishes, then use **Resume
scan** on the same saved inventory. You can also restart the application and resume
that inventory. Do not start a new scan if you want to continue existing work.

Progress is saved automatically in the audit SQLite database: the current URL,
its first unfinished stage, the last completed URL, queued URLs, sitemap files and
entry positions, and completed pagination probes. Finished website discovery and
browser stages are reused. A saved successful Corpus presence check is not repeated
just because its HTML text check was interrupted. Fully completed URLs are skipped.

If a browser interaction was stopped halfway through, the browser may reopen that
**one current page** to reconstruct its DOM. It does not restart the website scan or
repeat completed URLs. After an abrupt process exit, only work not durably committed
can be retried. Browser memory/click state cannot be restored byte-for-byte. Sitemap
resume rereads only the unfinished file; if that file changed, its entry cursor is
reset so new entries cannot be skipped. Numeric probe HTML is saved locally with its
original capture time; blocked responses and file bodies are not cached.

Existing inventories remain compatible. Older completed URL results stay saved;
an older in-flight stage without a checkpoint may need to be retried once. A former
count-limited inventory still uses the documented one-time recovery pass to restore
links discarded by that old limit; normal pause/resume does not replay finished pages.
Pause history becomes a historical note only after pending work has been exhausted;
real website/Corpus restrictions remain visible.

The read-only `/api/runs/<id>/checkpoint` endpoint (also included in the run summary)
returns the saved URL/stage, last completed URL and pause timestamp. It never starts
a scan or exposes cached HTML, session cookies or credentials. See
[checkpoint implementation](docs/CHECKPOINTS.md) for storage and recovery details.

## Discovery and boundaries

### Performance review

See [the measured bottlenecks and changes](docs/PERFORMANCE_REVIEW.md).
Local discovery writes use short durability checkpoints instead of a disk commit
for every link/event. Every URL, source method, edge and event is still stored;
the checkpoint size is not a collection limit. No transaction spans a network or
browser wait. Source pages stay resumable until their links have been saved.
Equivalent filter coverage groups are indexed and counted once while retaining
each source control's evidence. Unchanged static controls are not rewritten twice
for the same HTTP/rendered page; new values and URL dependencies are still explored.
Historical article snapshots are read only when missing publication evidence needs
recovery. Live URL polling does not recalculate whole-inventory totals.

These changes reduce local CPU/disk work, not Corpus safety delays. HTML text still
needs a separate API read after a confirmed URL match. Full rendered discovery on
large news archives can remain time-consuming; there is no promise of a universal
scan duration or of access to blocked content.

Existing capabilities are preserved: recursive static links, navigation/button URLs,
frames, canonical/redirect evidence, sitemap indexes, feeds, structured-data links,
HTTP Link headers, rendered links, observed XHR/API URLs, supported read-only filters,
lazy tabs, Load More, scrolling and numeric/path/observed cursor pagination.
Loaded same-page tabs are content sections, not separate pages or filters.

Browser readiness tracks documents, scripts, styles and content API requests instead
of waiting for every background network request to stop. Known third-party telemetry
does not hold up discovery; same-site and unknown API requests still count, with the
500ms quiet window retained. Unfinished content requests remain disclosed. Unchanged
frame HTML is not repeatedly reparsed, HTTP responses are decoded once, redundant
filter baseline reloads are avoided, and pending URLs use a depth-ordered database
index. These optimizations do not skip rendered discovery or increase Corpus traffic.

No navigation-depth or fixed URL/browser-page/action/filter-value/pagination-number
cutoff is imposed. Old saved count limits are ignored, including on resume.
Recursive sitemap discovery also has no fixed file-count ceiling. Discovery continues
until observed work is exhausted, content stops changing, access is blocked or the user
stops the scan. Duplicate and crawler-trap detection, serial processing, request/body
timeouts and size protection remain in place. Robots policy and request cooldowns
are respected. Sitemaps are discovery hints; robots.txt is permission policy, not
a completeness inventory. Numeric pagination uses exponential probes and binary
search under a contiguous monotonic-series assumption. Blocking, repeated content,
observed non-monotonic behavior leave the maximum unknown. Observed
cursor values are followed; arbitrary cursors are not generated.

Resuming an inventory that recorded an old count cutoff schedules a one-time
rediscovery pass over processed HTML/API/feed source pages. Existing URLs, file links,
snapshots and Corpus results are retained. Unchanged HTML comparisons are reused;
changed live text is rechecked without repeating a successful presence lookup.
After the rediscovery work is exhausted, old count cutoffs become historical notes.
Other access errors remain visible. Interrupted recovery continues without repeatedly
resetting completed work. No files are downloaded or text-extracted by this recovery.

The expanded production ZIP adaptation adds streamed HTML size limits,
HTTP-200 wall detection, typed retry/error policy, quota-header cooldowns, lazy
resource links, conservative response-backed JSON pagination and bounded browser
cleanup. See [feature review and exclusions](docs/ZIP_FEATURE_REVIEW.md); it does
not claim complete 116-feature parity or unrestricted website access.

The complete main-crawler source is now retained under `vendor/main_crawler/`
(311 Python files). Reviewed original challenge helpers are connected to
discovery; HTML/relation/header extraction now uses one CrawlScope-owned pipeline
with tested parity for imported link behavior. The other production features remain optional, preserving this audit's
HTML-only text, URL-only resources, serial processing and read-only Corpus rules.
See [complete source integration](docs/MAIN_CRAWLER_INTEGRATION.md) for the manifest,
active/optional states, browser identity switch and configuration prerequisites.
Inspect or verify without starting services:

```powershell
.\.venv\Scripts\python.exe scripts\main_crawler_features.py
.\.venv\Scripts\python.exe -W ignore scripts\main_crawler_features.py --verify
```

Normal scans remain public-page audits. Account/solver features are retained as
optional source, not automatically executed. Original browser identity helpers
are opt-in; they do not grant protected access. Where authorized, manually prepare
a public session:

```powershell
.\.venv\Scripts\python.exe -m crawler_coverage_auditor.validation.site_session --url https://example.com/
```

Cookies are exact-origin scoped, encrypted for the Windows user and never exposed
in dashboard/API reports. A session file's existence does not guarantee freshness.

## Corpus API and credentials

The dedicated authenticated **POST `/v1/held`** checks URL presence;
**POST `/v1/text`**, with `{"items":[{"url":"https://example.com/"}]}`, returns
stored HTML text. The key needs `text` access. No Corpus dashboard/browser search,
resource download or document-text fallback is used. Although the API permits 500
URLs per presence batch, scans always send **one URL per request**. Dashboard and
CLI Corpus spacing is at least 2 seconds, with a separate 60-second Corpus timeout.
Backoff may increase pacing; a slower configured minimum is never shortened.
Although the text endpoint accepts up to 25 items, CrawlScope checks **one HTML page
per request**, on the same serial paced lane as presence checks, to limit HDD load.
Successful comparisons are saved and skipped on resume; API failures retain URL
presence and leave text unassessed. An old presence-only inventory can be upgraded
without rediscovery if its live HTML snapshots are available. Corpus bodies remain
in memory only; reports store scores, lengths, hashes, versions and capture times.
`not_tracked` resource responses (for example images) are not crawler misses and
are excluded from coverage/verification denominators, but retained in inventory.

Configure/rotate a key locally without embedding it in source or reports:

```powershell
.\.venv\Scripts\python.exe -m crawler_coverage_auditor.corpus.credentials
```

The helper prompts privately. A protected local credential is used on Windows;
`CRAWLSCOPE_CORPUS_API_KEY` can override it at runtime. `CRAWLSCOPE_CORPUS_API_URL`
sets the trusted operator-configured API origin. Credentials never enter AuditConfig,
React state or reports. No Corpus certificate file, CA bundle, certificate switch
or dashboard banner is required. The dedicated Corpus client uses the
operator-approved compatibility connection: HTTPS is used without authenticating
the server identity. This can expose the API key/results to network interception;
the exception is isolated from website and browser connections, whose HTTPS
verification remains unchanged. API-key authentication, response validation,
sequential pacing and checkpoint recovery remain required. Connection errors leave
URLs unverified, never missing. Resume the saved scan rather than starting over.
API redirects are refused
so bearer credentials cannot follow them to another host.

Historical integrations' matches remain visible until a saved-analysis update.
Earlier matches are archived before replacement. Production Corpus data is read-only.

## Metric definitions

HTML inventory means validated HTML, not failed candidates or resource links.
PDF/document/image/media counts mean **collected URLs**, not file availability.
Found/missing require a successful authoritative Corpus result. Other eligible
URLs are unverified, never assumed missing.

For a chosen eligible scope (`E`), found (`F`) and missing (`M`):

| Metric | Exact definition |
| --- | --- |
| Checked | `C = F + M` |
| Unverified | `U = E - C` |
| Verification completion | `100 × C / E`, N/A if E=0 |
| Verified coverage | `100 × F / C`, N/A if C=0 |
| Coverage lower bound | `100 × F / E` |
| Coverage upper bound | `100 × (F + U) / E` |
| Live HTML pages with text | valid HTML with text_length>0 / valid HTML |

If Corpus reports `not_tracked` for `N` URLs, the inventory count stays `E` but
the verification denominator is `E - N` and `U = E - N - C`. No absence is inferred.

Bounds apply to the observed inventory, not undiscovered pages. Coverage is not final
when unverified URLs remain. The main donut is **HTML-only**. Global verification
includes selected resource types that Corpus can track. Offline compatibility reports
continue to identify images separately from page/document coverage.

The updated Coverage API supplies HTML bodies. Per-page word coverage is estimated
from distinct live words present in Corpus, not exact semantic completeness. Repeated
carousel/responsive text does not inflate the denominator. Extra corpus
boilerplate does not reduce that directional score, and reordered words may still
match. HTML extraction health describes only CrawlScope's live text, with
empty, unmeasured, rendering-required and duplicate-fingerprint states separated.
Duplicate fingerprints are clues, not proof of semantic duplicate URLs. All-unmeasured
text is N/A, not a fabricated zero. Archive years are inferred from URL evidence;
they are not guaranteed publication dates.

## Architecture and verification

```text
React pages → local FastAPI → bounded SQLite read queries
Start/resume scan → CoverageAuditor → one website URL → CoverageAPIClient
                → HTML comparison / URL-only files → SQLite → next URL → reports
```

State/summary polling contains no URL inventory. Analysis is requested only on
demand. Detail evidence is fetched only when opened. Changes to storage are additive;
historical first sources and unknown navigation depths are not invented.

Private/loopback/link-local/metadata targets are rejected, including HTTP redirects
and browser subrequests. Local fixture development must deliberately set
`CRAWLSCOPE_ALLOW_PRIVATE_TARGETS=1`; keep this unset in normal use. DNS is checked
before transport, not pinned: hostile DNS rebinding requires a hardened outbound
firewall/proxy. The dashboard binds locally and is not a public authenticated service.

```powershell
npm.cmd --prefix frontend run check
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\preview_react.py
```

See [implementation audit](docs/IMPLEMENTATION_AUDIT.md) for the requirement matrix,
active file structure, deletion list, database changes, API contract, scale measurements,
browser checks and limitations. See [troubleshooting](docs/TROUBLESHOOTING.md) for
interpreting older saved failures. Runtime databases, reports and secrets are ignored
and preserved; Streamlit and obsolete Corpus integration sources were removed.

The CLI is `audit.py --url ...`; browser, linked subdomains and Wayback discovery run automatically. It uses
the same automatic serial workflow. Consult `audit.py --help`. Never run it alongside
an active dashboard job. An internal `discovery_only` compatibility flag and `/verify`
endpoint remain for existing integrations/tests; neither is a dashboard workflow.

## Local configuration and duplicate handling

An actual `.env` file is optional. Copy `.env.example` only if you need overrides.
The backend and launcher load the project-root file; process variables take precedence.
The protected Windows key continues to work without `.env`. Never put secrets in
`VITE_*` variables or commit `.env`. Install updated requirements after pulling changes.

The Control Room now contains only five capture choices and a Corpus API-key form.
Internal discovery, browser, pagination and timing switches are not user-facing.
Normal scans always run the supported discovery methods, including live-validated
Wayback candidates. Corpus requests remain read-only and sequential.
The API-key form encrypts the key for the current Windows user and never returns
it, stores it in scan settings/reports, or validates it by opening the Corpus website.
An environment-managed key takes priority and must be removed from that environment
before the form can replace it. Pause an active scan before changing credentials.

URLs are unique by `(audit run, normalized URL)`, enforced by SQLite. Repeat discoveries
merge methods, parent edges and shortest depth rather than creating entries or queueing
another fetch. Host case, default ports, query ordering, tracking parameters and ordinary
section anchors are normalized; path case, semantic query values and SPA routes remain
distinct. Different audit runs deliberately retain separate historical snapshots. Equal
content fingerprints or canonical/redirect aliases alone do not justify deleting URLs.
Numeric pagination tasks are durable and their probes use the same serial verifier.

See [serial workflow review](docs/SERIAL_WORKFLOW_REVIEW.md) for this update's checks,
cleanup scope and configuration findings.

See [advanced discovery upgrade](docs/ADVANCED_DISCOVERY.md) for the new static
JavaScript analysis, published CMS/ResourceSync adapters, resumable source tasks,
retry evidence preservation, automatic Wayback discovery and compatibility rules.

See [consolidation audit and verification](docs/CONSOLIDATION_AUDIT.md) for shared
snapshot parsing, source-label preservation, filter/result caching, conservative
browser GET-JSON reuse and measured before/after results. These changes require a
backend restart after an active scan is safely paused or finished. They do not
rewrite saved inventories or activate code in an already-running worker.

The dashboard polls summaries with `diagnostics=false` to omit expensive unused
HTML-health calculations (`health` is null in this explicit mode). The default
summary API still returns its existing diagnostics. Summary results are reused for
at most one second only while the database/WAL revision is unchanged.

## Standalone local browser

For the independent Camoufox browser setup, see [docs/LOCAL_BROWSER.md](docs/LOCAL_BROWSER.md).
CrawlScope does not connect to the production crawler's browser service.

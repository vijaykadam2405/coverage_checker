# CrawlScope

Independent public-website discovery and evidence-based production crawler coverage.
The frontend is React/JSX/CSS. Python runs the crawler, SQLite persistence and local
FastAPI service; it does not render the live dashboard.

## Run on Windows

From this project directory, first-time setup:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
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

For development only, run the backend alongside
`npm.cmd --prefix frontend run dev` (Vite port 5173, API proxy to 8000).
Normal use requires only port 8000. Do not run a CLI audit and dashboard as
simultaneous writers to the same database.

## Workflow

1. In **Control room**, select capture types and real discovery/safety settings.
   They are persisted locally, locked while an audit is active and apply to the
   next inventory. Saved inventories retain their original scope and pacing.
2. In **Overview**, enter a public website and click **Start website scan**.
   Each URL is processed serially: discover/validate → render if enabled →
   Corpus presence → HTML text comparison → next URL. There is no separate Corpus
   Check button. A configured key is required before starting a dashboard scan.
3. Website requests are spaced at least 1.5 seconds apart per host. Corpus
   requests contain **one URL**, are serialized, and are spaced at least 3 seconds
   apart after responses; slow reads/rate limits increase the delay. Selected
   file links are never downloaded or text-extracted.
4. If scanning pauses, completed checks remain saved. **Resume scan** continues
   unresolved checks before fetching the next queued page. Saved successful
   website snapshots and comparisons are reused. **Update saved analysis** upgrades
   older inventories without rediscovery. A timeout, unavailable service, malformed result
   or incomplete response never becomes a confirmed crawler miss.

**Overview** places six selected-website counts above the page title: valid HTML,
missing HTML, PDF links, missing PDF links, document links and all discovered URLs.
It shows current scan activity, per-type Corpus coverage and the latest 20 URL records.
The removed extraction-health section is not a Corpus text-completeness measure.
Saved websites are a searchable clickable table, not a dropdown; each opens its latest
inventory. Older runs remain accessible through the selected website's Saved scan history.
Discovered CDN/resource hosts are not separate tested websites.

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

**Analysis** loads on entry or manual refresh, not every polling interval. It shows
coverage by type and depth, source composition, validation outcomes, host counts,
pagination maxima/probes/missing ranges, filters, archive years, sitemap/API evidence
and comparable history. Empty sections are omitted; tables retain exact values.

**Activity** loads bounded incremental saved events with filters and plain-language
failure impact/continuation/recommended actions. Historical script/document notes
are separated from current coverage boundaries, not erased.

## Discovery and boundaries

Existing capabilities are preserved: recursive static links, navigation/button URLs,
frames, canonical/redirect evidence, sitemap indexes, feeds, structured-data links,
HTTP Link headers, rendered links, observed XHR/API URLs, supported read-only filters,
lazy tabs, Load More, scrolling and numeric/path/observed cursor pagination.
Loaded same-page tabs are content sections, not separate pages or filters.

No navigation-depth cutoff is imposed. URL/browser/action/filter/probe safeguards
bound work and report incomplete exploration. Robots policy and request cooldowns
are respected. Sitemaps are discovery hints; robots.txt is permission policy, not
a completeness inventory. Numeric pagination uses exponential probes and binary
search under a contiguous monotonic-series assumption. Blocking, repeated content,
observed non-monotonic behavior or safeguards leave the maximum unknown. Observed
cursor values are followed; arbitrary cursors are not generated.

Public pages only. No automated login, CAPTCHA solving, stealth or access-control
bypass. Where authorized, manually prepare a public session:

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
CLI spacing is at least 3 seconds, with a separate 60-second Corpus timeout.
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
React state or reports. The previously authorized self-signed API origin has a narrow
TLS exception; all website TLS verification remains enabled. API redirects are refused
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

The expert CLI remains `audit.py --url ... --browser --include-subdomains`; it uses
the same automatic serial workflow. Consult `audit.py --help`. Never run it alongside
an active dashboard job. An internal `discovery_only` compatibility flag and `/verify`
endpoint remain for existing integrations/tests; neither is a dashboard workflow.

## Local configuration and duplicate handling

An actual `.env` file is optional. Copy `.env.example` only if you need overrides.
The backend and launcher load the project-root file; process variables take precedence.
The protected Windows key continues to work without `.env`. Never put secrets in
`VITE_*` variables or commit `.env`. Install updated requirements after pulling changes.

URLs are unique by `(audit run, normalized URL)`, enforced by SQLite. Repeat discoveries
merge methods, parent edges and shortest depth rather than creating entries or queueing
another fetch. Host case, default ports, query ordering, tracking parameters and ordinary
section anchors are normalized; path case, semantic query values and SPA routes remain
distinct. Different audit runs deliberately retain separate historical snapshots. Equal
content fingerprints or canonical/redirect aliases alone do not justify deleting URLs.
Numeric pagination tasks are durable and their probes use the same serial verifier.

See [serial workflow review](docs/SERIAL_WORKFLOW_REVIEW.md) for this update's checks,
cleanup scope and configuration findings.

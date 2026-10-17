# CrawlScope discovery upgrade

This is an additive upgrade. React, FastAPI, SQLite, the existing HTML text
comparison and the read-only Corpus API remain in place. Collection has no
arbitrary total-URL, depth, browser-page or filter-value ceiling.

## Implemented behavior

| Feature | Actual behavior |
|---|---|
| Retry evidence preservation | Temporary DNS, transport, TLS, rate-limit and server failures retain a previous valid HTTP result. The latest failed attempt is recorded separately. A genuine 404/soft-404 changes current eligibility. |
| Text preservation | Empty or invalid fetches do not erase saved text. Changed non-empty snapshots retain a previous version in history. |
| Clear progress | URLs saved, processed URLs, saved unique Corpus checks and queued URLs are independent counts. Corpus checks saved includes historical results even if current validation fails. |
| Durable discovery tasks | Advertised metadata sources have versioned tasks, cursors, states and parsed-asset caches. In-flight tasks return to the queue on resume; failed sources get a fresh attempt on explicit resume. Resolved errors remain historical. |
| Streaming sitemaps | Network and gzip bodies spill to temporary files; XML entries are parsed incrementally. Recursive indexes, image/video extension URLs, text lists and RSS/Atom entries are supported. Atom next links are followed. |
| JavaScript analysis | Acorn parses referenced scripts and inline JavaScript without evaluating them. Concrete root-relative/absolute URL literals are candidates; dynamic templates are evidence only. Shared external script results are reused within a run. |
| Framework metadata | Supported JSON hydration containers, including Next/Nuxt-style containers, expose published URLs. This is not universal framework route enumeration. |
| Published metadata | Additional URL attributes, image metadata, manifests, explicit comment/text URLs, template links, AMP links, meta refresh and Content-Location candidates are collected. |
| Filter dependencies | GET states retain repeated dependency parameters. Browser exploration queues option sets observed to change after another read-only selection. Blind Cartesian generation is not used; full combination coverage remains unproven. |
| Public CMS listings | An advertised WordPress REST root can supply its advertised public posts/pages collections. Metadata-only pages are followed using X-WP-TotalPages; item IDs and reported totals are reconciled. Other/custom CMS collections need dedicated adapters. |
| ResourceSync | Advertised descriptions, capability lists, resource lists and change lists are followed. Deleted entries and resource/content dumps are not downloaded. A change list alone is not a complete baseline. |
| Historical indexes | Wayback CDX runs automatically in normal dashboard/CLI scans. Continuation keys supply historical URL candidates, which still need live validation. Common Crawl needs an explicitly configured collection ID. |
| Corpus freshness | Saved results are reused within a configurable 24-hour default lifetime. Expired checks encountered during verification are refreshed serially; prior matches are retained in history. |
| Reconciliation | Analysis distinguishes source-task completion and published collection counts from proof of entire-website completeness. |

## Workflow

1. Load the selected capture types, scope, pacing and checkpoint.
2. Discover starting URLs through the existing sources.
3. All new sources use the same normalization/scope/deduplication gateway.
4. Process one URL: website stage, browser stage, Corpus presence, HTML text
   comparison, then save the checkpoint.
5. Run one pending metadata-source task between URL-processing steps. New links
   join the same durable queue. Continue metadata tasks when the URL queue is empty.
6. Complete discovered URL/pagination/source work or report unresolved boundaries.
7. Produce the existing reports with additional discovery reconciliation.

Metadata listings may contain many links in one response. This does **not** bulk
check Corpus: presence/text calls remain one URL at a time, with one request in
flight. Website pacing and Retry-After remain enforced by the shared client.

PDFs, documents, images and media remain URL-only. Script/XML/JSON/manifest
metadata can be read for URL discovery; it is not document-content extraction.

## Dependencies and start

The existing Python dependencies are sufficient. Acorn is pinned in the frontend
package/lockfile. The backend's static analyzer uses Node.js and a persistent
hidden subprocess, not a Python JavaScript evaluator.

After updating/installing dependencies:

```powershell
cd "C:\Users\vijay\Desktop\coverage checker\frontend"
npm ci
npm run build
npm start
```

If the old service is still running, the launcher deliberately reuses it and
prints a restart notice. Pause any active scan and wait for saved progress before
stopping that service. Starting another launcher does not replace an active scan.

## Old inventories

Existing tables and run IDs are preserved. The migration adds attempt history,
snapshot history, source tasks, caches and latest-attempt fields.

On resume through the dashboard, older scans receive all supported discovery
methods, including Wayback. Normal dashboard scans no longer expose method-off switches. Saved valid
HTML sources and temporary-failure HTML sources are requeued for the new discovery
pass when needed; matches and snapshots remain saved. A Wayback-only upgrade
adds source work without re-fetching already completed HTML sources. HTML/browser stages may need another
fetch because stored plain text cannot reconstruct original scripts or DOM state.

The preservation fix prevents future overwrites; it cannot reconstruct text that
was already erased before this upgrade. Affected pages need a successful refetch.

## Backend configuration

Wayback is enabled automatically in normal scans. To also use Common Crawl, configure an
explicit collection ID, for example `CRAWLSCOPE_COMMONCRAWL_INDEX=CC-MAIN-2026-39`.
This selects a crawl collection, not a maximum page count. Index services may be
unavailable or incomplete; those boundaries remain visible.

`CRAWLSCOPE_NODE_PATH` optionally selects a Node executable when it is not on PATH.
No new Corpus key, paid provider, login account or distributed service is required.
Saved Corpus credentials are never returned to the frontend or placed in metadata
caches. The Control Room sends only the key that the user enters to the protected
local save endpoint; it never reads the existing key back.

## Deliberate boundaries

- No automatic login, CAPTCHA solving, security testing or protected-content bypass.
- No guessing private files, sequential IDs, arbitrary date ranges or user accounts.
- No fabricated total-page count or weighted completeness percentage.
- Sitemap bodies use the protocol's 50 MB per-file size; other response/time
  safeguards still apply. A safeguard reports incomplete exploration, not success.
- ResourceSync, public APIs and framework data only help where a website exposes
  suitable public evidence. Route patterns alone are not counted as existing pages.
- Metadata caches are run-scoped. A later audit observes assets again; a mid-scan
  website change can still require reconciliation.
- Root-cause patterns require production logs/configuration before being called
  confirmed production-crawler causes.

## Verification

Run `python -m pytest -q` in the project environment and `npm run check` in frontend.
Fixtures cover real Playwright-dependent filters, static parsing without execution,
serial Corpus ordering, PDF-body exclusion, large/gzip/text/feed sitemaps, XML
safety, archive continuations, cache expiry and old-inventory compatibility.
Tests use local fixtures/mocked Corpus; they do not consume production API quota.

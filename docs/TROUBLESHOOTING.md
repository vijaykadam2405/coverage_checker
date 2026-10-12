# Corpus and activity troubleshooting

## Current API-only workflow (11 October 2026)

CrawlScope uses authenticated POST `/v1/held` for presence and `/v1/text` for HTML
comparison, never dashboard search or file text. Every website URL is processed
serially through discovery, presence and text comparison before the next URL.
Corpus requests always contain one URL, with at least 3 seconds between responses
and the next request. Increase spacing in Control room if necessary; no batch or
concurrency control is exposed. A Corpus outage pauses further discovery, and
Resume scan retries unfinished checks from saved snapshots. Persistent failures require the Corpus
operator to investigate logs/storage; CrawlScope cannot infer an HDD bottleneck from
a timeout alone. Failed or incomplete checks remain unverified, never missing.

Restart the backend after code changes. Frontend build alone does not reload Python.
Private website targets are rejected by default. The local-development opt-in is
only for authorized fixtures, not normal production use.

The sections below document **historical investigation**, not the current API contract.
Older evidence is preserved. See IMPLEMENTATION_AUDIT.md for current contracts/tests.

## Evidence from the saved Tata inventory

Run 2 records repeated Corpus responses reporting `timed out` or `database unreachable`. A single read-only metadata request on 6 October 2026 also returned HTTP 200 after approximately 20 seconds with `reachable: false`, `error: timed out`, and an empty `rows` list. This was not a successful empty search. No missing-page conclusions can be drawn from it.

The reviewed crawler ZIP's `ingest_api/pgstore.py` search uses `(title ILIKE %s OR url ILIKE %s)` with `%query%`, ordered by ingestion time. `body=false` avoids full-text body searches but does not make this metadata query an exact indexed URL lookup. Client-side spacing can reduce pressure; it cannot diagnose disk saturation, repair connectivity, or prove deployed indexes are present. The ZIP is a source snapshot, not proof of the VPS's deployed configuration.

If failures persist after paced recovery, the Corpus operator should inspect service/PostgreSQL logs, connection availability, query timeout settings, disk latency, and the deployed search plan/indexes. Do not launch a repeated broad-search loop. A URL-specific indexed lookup would require a separately coordinated Corpus API/server change; CrawlScope does not invent an unsupported exact-search parameter.

Reference: PostgreSQL documents [trigram indexes for LIKE/ILIKE](https://www.postgresql.org/docs/current/pgtrgm.html). Rate-limited services can specify cooldowns through [Retry-After](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Retry-After); CrawlScope does not retry ahead of that schedule.

## Historical integration changes (superseded where API-specific)

- One request at a time, including stored HTML text and recovery attempts.
- Dashboard spacing of at least two seconds, adaptive slowdown and bounded recovery of HTTP and JSON database errors.
- A separate Corpus timeout; public website request limits remain unchanged.
- API discovery reuses the first inventory query rather than an arbitrary probe query.
- Resume retries missing text without repeating a confirmed URL-presence search.
- The image-extension classifier definition is restored; referring to an undefined constant otherwise crashes resource discovery.
- Surrounding URL whitespace is trimmed before link resolution, deduplication, saving and fetching. Of the saved Tata scan's 110 HTTP 404 HTML records, 109 contain trailing spaces (for example `/about `); these are historical malformed requests, not proof that `/about` is unavailable. Percent-encoded spaces and meaningful query parameters are preserved. A new scan is required to validate the corrected inventory; old failures are not rewritten.
- Bounded browser retries for transient DNS/connection/timeout failures; persistent failures remain boundaries.
- Known media lightboxes are closed before further expansion. Consent, login and CAPTCHA dialogs are not automatically dismissed, and clicks are not forced through overlays.

## Interpreting historical activity

Run 2 contains 118 repeated `.top` website-script warnings and 50 document extraction errors from older code. Current discovery records readable-page script failures as diagnostics and no longer extracts document text. Existing scan records are preserved: source-site JavaScript, real HTTP 404s, network failures and historical logs are not deleted or claimed repaired.

The dashboard distinguishes collected HTML candidates, validated HTML URLs, URL-only resources, and Corpus-unverified entries. File links are not downloaded or HTTP-validated. A website's broken link or inaccessible page is not counted as a production-crawler miss. Matching text fingerprints can indicate aliases/fallback pages and do not independently prove semantic page identity.

Restart the dashboard process to load changed worker modules, then use **Resume scan**
or **Update saved analysis** on an existing inventory. A new scan is needed to exercise
updated discovery behavior on previously completed inventories; historical audit
evidence is not rewritten.

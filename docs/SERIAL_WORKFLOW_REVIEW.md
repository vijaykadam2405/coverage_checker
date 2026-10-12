# Serial automatic workflow review — 11 October 2026

## Implementation

- One selected URL at a time: website response, optional rendering, authoritative
  Corpus URL check, then HTML text comparison. No parallel URL workers or bulk
  Corpus requests. One shared Corpus client preserves pacing across presence/text.
- Dashboard and CLI website spacing: 1.5 seconds minimum per host. Corpus spacing:
  3 seconds minimum after responses, plus bounded retries, Retry-After and adaptive
  slowdown. Existing slower settings are preserved, including values above 12 seconds.
- Files/images/PDFs/documents stay URL-only; no file text is requested or downloaded.
- Corpus errors pause further website exploration, not mark URLs missing. Resume
  retries unresolved text/presence from saved snapshots before the next website URL.
  A partial/stopped page render is revisited; completed comparisons are skipped.
- Old completed inventories can be updated from saved snapshots. Read-only API polling
  never starts scans. The legacy `/verify` endpoint/internal discovery-only mode remain
  for compatibility, but the frontend has no separate Corpus Check action.
- Numeric pagination work is saved as link metadata, not raw DOM. Probes use the same
  serial presence/text pipeline; completed probe URLs are not queued for a second crawl.
  Exhaustive discovery is not promised: blocking, traps, safeguards and inaccessible
  filter states remain disclosed limitations.

## URL identity and stored data

Read-only checks of the existing local database found **1 run, 237 URL rows, zero
duplicate `(audit_run_id, normalized_url)` groups**; `PRAGMA quick_check` returned `ok`.
No audit records or reports were deleted, and no full production scan was started.

SQLite's unique key prevents duplicate rows. Rediscovery merges source methods,
navigation depth and parent edges. Duplicate URLs at the safeguard are still merged.
A repeated image/download hint cannot overwrite a previously classified HTML record.
Distinct path case, semantic queries and SPA routes remain distinct. Separate runs
intentionally retain URL history; equal content is not proof of URL equivalence.

## Configuration and API verification

- Dependency consistency checked with `pip check`.
- No actual `.env` was present. It is optional, not a missing required file. Added
  `.env.example`, backend/launcher loading with process-variable precedence, and
  `python-dotenv` in requirements. Windows DPAPI remains the configured key source.
- The protected local credential loads successfully without exposing it. A minimal
  read-only live `/v1/me` check returned HTTP 200 and confirmed the `text` scope.
  Presence/text payloads, response validation and failures are covered by isolated
  transports; tests cannot contact the production Corpus origin.
- Old performance preferences are migrated to one worker/one URL per request and
  safe minimum spacing. Settings reject attempts to re-enable bulk/parallel checking.
- The launcher detects an older running backend. The React UI disables new scans
  and shows restart instructions until that backend is restarted. No other service
  was stopped and no extra application port was started.

## Review and cleanup scope

Detailed review focused on the executing paths: scheduler, discovery/rendering and
pagination, deduplication/storage, Corpus client/text comparison, jobs/resume, local
API/settings, credentials/environment and React scan/status controls. Static name/import
checks cover every project-owned Python source/test/helper; formatting, module resolution,
tests, production bundling and browser checks cover the frontend. This is not a claim
that third-party dependencies, every possible website or every runtime branch have
been exhaustively reviewed or proven error-free.

Removed the unreferenced React `Metric.jsx`, its obsolete metric styles, unused test
imports and duplicated MIME-classification branches. Corrected depth-gap analysis to
use actual navigation depth rather than minimum sitemap-discovery depth. Obsolete
legacy/UI folders contain only compiled caches; their cleanup command was blocked by
execution policy and they remain untouched. Active package files, dependencies, runtime
sessions, credentials, audit databases and reports were preserved. Caches can be recreated;
the removed unused component can be reconstructed from this task's recorded edit.

The Sites skill was used only for local React build/preview guidance; nothing was
published and no hosted site or external asset dependency was introduced.

## Verification commands

Final full suite: **189 Python tests passed**, **12 frontend tests passed**.
Frontend formatting and production build, Python static name/import checks and
compilation, and dependency consistency checks passed. Browser screenshots were
visually inspected; inactive/unknown scan progress does not show a fabricated 100%.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest -q
npm.cmd --prefix frontend run check
```

The Python suite includes an actual Chromium QA pass at 1600, 1366, 820 and 390px
across all five dashboard views. New regressions assert per-URL ordering (including
rendering), pause/restart durability, duplicate merging and absence of file-text calls.
An upstream Starlette test-client deprecation warning is separate from application
errors; it does not indicate a broken production integration.

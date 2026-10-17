# CrawlScope pre-push readiness

Reviewed: 18 October 2026 (Asia/Calcutta).

Current connection-policy update: at the user's explicit request, the Corpus CA
module, certificate environment options and dashboard banner have been removed.
Corpus now uses the approved compatibility connection without certificate setup;
API-key checks and website HTTPS verification are unchanged. A single read-only
API check already succeeded under that policy on 18 October. The preparation
record below includes earlier release checks. See README's Corpus connection
section for the current behavior and
its server-identity tradeoff.

## Decision

**Main-branch publication review corrected on 17 October 2026.** The user confirmed
the exposed credential was revoked. The flagged commit belongs only to the
unrelated local `local-import-backup` branch: it is not an ancestor of `main`, and
the remote advertises only `main`. The earlier all-ref check incorrectly treated
that backup as part of the intended push. No history rewrite is required for a
normal `main:main` push. Do not publish the backup branch with `--all` or `--mirror`.

The release check now inspects HEAD ancestry by default, with `--all-history` for
an explicit wider local audit. A regression verifies both scopes. Publication
still requires the final tests/build and current-tree/history checks to pass.
The latest Corpus connection test succeeded with the configured API key.

## Changes in this preparation

- Removed the Corpus certificate module, settings and dashboard banner at the
  user's request. The Corpus-only compatibility policy requires no CA setup.
- HTTPS handshake failures stop repeated attempts and identify a connection
  failure instead of appearing as an unspecified HTTP 0 / overload failure.
  Other transport failures report their sanitized failure category. URL presence
  and saved text evidence are not converted into missing results on failure.
- Aligned the example Corpus endpoint port with the application's default.
  Added Node executable and optional historical-index configuration notes.
- Added API guards against starting/resuming verification on stale backend code;
  forced source revision checks do not wait for the dashboard polling cache.
  Reading stored results and pausing existing work remain available.
- Added `scripts/prepush_check.py`, development requirements, regression tests
  and a Windows GitHub Actions workflow with full-history checkout. The check
  does not stage/commit/push or contact the production Corpus.
- Extended Git exclusions for local certificate/key material. Publication checks
  inspect publishable tracked/untracked files plus common secret patterns in
  HEAD ancestry. `--all-history` additionally audits unrelated local refs. Pattern
  scanning is a safeguard, not an exhaustive secret audit.

## Earlier preparation checks

| Check | Observed result |
| --- | --- |
| Full backend regression run | 456 passed; two existing non-failing warnings |
| Latest TLS/publication/revision focused run | 29 passed, including the additional history-detection regression added after full-suite collection |
| Python dependency consistency | Passed |
| Python undefined/unused-name checks | Passed |
| Current working-tree publication scan | Passed: no flagged secrets or runtime data among publishable files |
| Historical credential scope | Only unrelated local backup; not in the history being pushed |
| Frontend dependency advisories | npm audit reported zero advisories |
| SQLite integrity / foreign keys | `quick_check` OK; zero foreign-key errors |
| Latest read-only Corpus API check | Passed under the approved compatibility policy |

The full regression run and the latest focused run overlap; their totals must not
be added together. The final frontend checks and read-only API smoke results are
recorded in the completion summary below.

Known warnings remain: Starlette TestClient/httpx deprecation, and an invalid
escape sequence in retained vendor test source. Vendor source was not modified.
No blanket dependency major-version upgrades or live production capacity tests
were performed.

## Preserved saved inventories

| Inventory | Saved state | URL entries |
| --- | --- | ---: |
| 1 | Limited | 236 |
| 2 | Cancelled | 638 |
| 3 | Corpus paused | 38,646 |
| 4 | Limited | 869 |

No inventories, checkpoints, Corpus records or protected credentials were deleted,
replaced or resumed during this preparation.

## Publication scope and remaining live-operation setup

1. The user confirmed that the Corpus API administrator revoked the exposed key.
   The current local key passed a single read-only API check. Keep working keys
   in protected local storage or `.env`, not example files, commits or reports.
2. Keep `local-import-backup` local. Its historical finding is in commit
   `ae48f865409e`, file `.env.example`. No rewrite or deletion of this backup is
   needed or authorized. Only the reviewed `main` branch may be pushed.
3. Corpus requires its HTTPS API endpoint and a working API key. No certificate
   file or certificate configuration is required. Website connections keep their
   existing HTTPS verification; this exception applies only to the Corpus client.
4. Use the protected credential helper or Control Room to rotate keys. Restart
   the backend after source changes, then resume the existing saved inventory.
5. Rerun the complete pre-push check and review the actual staged file list before
   the authorized GitHub push. Credentials in HEAD ancestry still fail the gate;
   an unrelated local backup is not part of the single-branch publication scope.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe scripts\prepush_check.py
```

## Completion summary

Frontend formatting, all 18 frontend tests and the production build passed.
Fresh-process read-only API smoke checks returned HTTP 200 for the dashboard,
state, summary, paginated inventory, checkpoint and analysis while external HTTPX
requests were forbidden. Final static-name and Git-whitespace checks passed.

Earlier all-ref checks failed on the backup credential, although current files
passed. The ancestry review above corrects that overly broad publication result.
The final release run must be checked again after this correction. No backup
history rewrite, deletion or force-push is needed.

## Subsequent code/image cleanup

Python imports were organized and import-order checking added to the release
check. Generated screenshots were moved to a recovery folder outside the project;
browser tests and the preview helper now default to external temporary folders.
At the user's explicit request, the two imported binary image fixtures and their
fixture-index entries were also removed. The import manifest and policy retain
complete accounting: 311 Python sources, 483 retained verified artifacts and
41 exclusions. Scan inventory image URLs and all saved audit data are unchanged.

## Final single-branch release verification — 18 October 2026

The complete `scripts/prepush_check.py` run passed after the history-scope
correction: current-file secret/runtime checks, HEAD-ancestry secret checks,
dependency consistency, Ruff import/name checks, **459 backend tests**, frontend
formatting, **18 frontend tests**, the production frontend build and Git whitespace
checks. The backend suite reported only the two known warnings described above.
These results supersede the earlier test totals and pending release statements.

Only `main:main` is approved for publication; the unrelated backup remains local.
Local inventories, reports, `.env`, protected credentials and dependency/build
folders are excluded. Test success is not a guarantee of continued production
API availability or complete website discovery.

## Latest connection simplification checks — 18 October 2026

After removing the certificate configuration and banner, 89 targeted backend
tests plus 8 browser/recovery/revision tests passed. All 18 frontend tests,
frontend formatting, production build, Ruff and Git whitespace checks passed.
A single read-only production Corpus request returned `found`; no scan was
started/resumed and no stored inventory was modified. The existing backend needs
a restart to load the source changes. This update has not been pushed.

## Resume and live-count pipeline verification — 18 October 2026

Discovery recovery no longer refreshes completed Corpus results solely because
their 24-hour cache lifetime elapsed. Unfinished/failed checks and changed saved
text still recover; explicit inventory verification retains the freshness policy.
Analysis reads local saved evidence every 15 seconds for the selected active
scan and refreshes once when it becomes inactive. `diagnostics=false` only skips
optional extraction-health work; HTTP 200 indicates a successful summary request.

Verification: all **463 backend tests** passed (the same two known warnings),
including a real-browser test proving database writes reach the summary cards and
detailed Analysis without manual refresh. All **20 frontend tests**, formatting,
production build, Ruff, dependency consistency and Git whitespace checks passed.
Live database read-only checks found integrity OK, zero duplicate URL groups,
zero foreign-key violations and zero orphaned Corpus matches. The running scan
was not paused, restarted or modified; a safe backend restart is needed to load
the updated worker logic. This update has not been pushed.

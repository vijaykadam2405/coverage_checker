# CrawlScope pre-push readiness

Reviewed: 18 October 2026 (Asia/Calcutta).

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
The separate live Corpus TLS configuration issue below remains unresolved.

## Changes in this preparation

- Removed the IP/port-specific Corpus TLS verification bypass. All origins now
  verify certificates; an optional trusted CA PEM can be configured through
  `CRAWLSCOPE_CORPUS_CA_BUNDLE`. Hostname validation remains enabled. This follows
  the [HTTPX SSL context configuration](https://www.python-httpx.org/advanced/ssl/).
- TLS failures now stop repeated attempts and explicitly explain certificate
  trust instead of appearing as an unspecified HTTP 0 / overload failure.
  Other transport failures report their sanitized failure category. URL presence
  and saved text evidence are not converted into missing results on failure.
- Aligned the example Corpus endpoint port with the application's default.
  Added local CA, Node executable and optional historical-index configuration notes.
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

## Verification

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
| Production Corpus verified TLS | **Blocked**: issuer trust could not be established |

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
   A replacement still needs to be configured locally for live operation.
   Do not paste the replacement into source code, commits, reports or chat.
2. Keep `local-import-backup` local. Its historical finding is in commit
   `ae48f865409e`, file `.env.example`. No rewrite or deletion of this backup is
   needed or authorized. Only the reviewed `main` branch may be pushed.
3. For live secure API operation, ask the administrator for a valid HTTPS hostname
   with a complete trusted certificate chain, or a trusted CA PEM. An API key is
   authentication, not a substitute for TLS server verification. The application
   will not automatically trust an unverified server certificate.
4. Save the replacement key through the existing protected credential helper or
   Control Room, configure the trusted API endpoint/certificate locally, and
   restart the backend before resuming the saved inventory.
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
folders are excluded. Test success does not establish live Corpus availability:
configure a replacement key and resolve the API certificate trust issue before
resuming live Corpus verification.

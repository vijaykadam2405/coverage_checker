# Complete main crawler source integration

## What changed

On 12 October 2026, the complete application source from the supplied `Crawler.zip`
was imported under `vendor/main_crawler/`. This supersedes the earlier selected-
adaptation decision in `ZIP_FEATURE_REVIEW.md`.

The user explicitly chose: **keep CrawlScope's current rules and retain other
main-crawler features as optional**. Copying code is not the same as running every
service or enabling every production behaviour.

| Import result | Count |
| --- | ---: |
| Python source files, including engine, services, scripts and tests | 311 |
| Files copied unchanged | 484 |
| Benchmark fixture with a credential-like token redacted | 1 |
| Git metadata, environment values and runtime/generated state excluded | 39 |
| Imported artifacts with checksum verification | 485 |

All 311 Python files are present without code modifications. This includes
`captcha.py`, `captcha_solver.py`, `siteauth.py`, `stealth.py`, `camofox_client.py`,
`hostsolver.py`, `paid_proxy.py`, `async_engine.py`, document extractors, ingest
clients, fleet logic, the orchestrator and supporting services. Original imports,
dependencies, service contracts and deployment definitions are retained.

`vendor/main_crawler/IMPORT_MANIFEST.json` accounts for **every non-directory ZIP
entry**: copied, redacted or excluded with a reason. It records archive and per-file
hashes. Exclusions are not discarded feature implementations: all Python source
is included. The archive SHA-256 is
`4356d544ed167ef56e97ad4bbbc45062d9c355a8ab6e09b8e32e6ce907853d59`.

There is still no identified numbered 116-feature manifest. The complete source
import avoids guessing which code files those 116 items refer to; it does not
invent a verified runtime feature count. See the source test-module index for
its 118 crawler test modules, which are not 118 independent features.

## Architecture

```text
React dashboard
    |
Local CrawlScope API and serial CoverageAuditor
    |
    +-- Existing independent discovery / validation / Corpus comparison
    |
    +-- Reviewed main-crawler bridge
            |
            +-- Original parse helpers              active
            +-- Original vendor/challenge detector  active
            +-- Original browser identity helpers   opt-in

vendor/main_crawler/
    +-- crawler/       complete original engine, account/solver/proxy code
    +-- orchestrator/  original control plane and schedules
    +-- healthcheck/   original production checks
    +-- scripts/       original operator/deployment tools
    +-- deploy/        original deployment definitions
    +-- ...            original docs, static data and fixtures
    +-- IMPORT_MANIFEST.json
```

The rest of the source remains optional and is **not launched by a normal
CrawlScope scan**. Its original entrypoints are retained, not replaced with dummy
functions. They require an isolated deployment, dependencies, configuration and
explicit activation appropriate to the operation. Source presence is not a claim
that CamoFox, a GPU solver, paid vendors, Postgres or MinIO are running locally.

## What is connected to normal discovery

`crawler_coverage_auditor/integrations/main_crawler.py` loads reviewed pure modules
with a private package name (`_crawlscope_main_crawler`). It verifies their hashes
before loading them and refuses to load account creation, full-engine or ingest
entrypoints in the coverage process. It does not add the vendor tree to the global
Python path or replace the existing `crawler_coverage_auditor` package.

- Static discovery uses the original `parse.extract_links_with_text()` and
  `parse.extract_rel_links()` alongside CrawlScope's resource, SPA and button-link
  handling. Original extraction is supplementary: its asset/depth/relevance
  exclusions do not replace CrawlScope's independent inventory.
- HTTP Link discovery also uses original `parse.links_from_link_header()`.
- Access checks also use original `captcha.detect_http()`, accepting only a
  blocking verdict, not merely an embedded widget/vendor. Rate limiting, login,
  legal restrictions and dead pages cannot be reclassified as successful content.
- Optional browser identity mode calls the original launch-argument, default-arg
  and initialization-script helpers. Website TLS verification, robots checks,
  request pacing, resource blocking and actual content validation remain in place.

The complete upstream `errors`, `canonicalize`, solver, proxy and account modules
are retained. The normal audit retains its existing conservative retry,
normalization and Corpus rules rather than wholesale replacing them with
production gates, conditional skips or deletion logic.

## Internal switches

No technical controls were added to the main dashboard.

```dotenv
CRAWLSCOPE_MAIN_CRAWLER_HELPERS=1
CRAWLSCOPE_MAIN_CRAWLER_STEALTH=0
```

The first is enabled by default and connects local parsing/detection helpers.
The second is off by default; set it to `1` in the local backend environment to
use the original browser identity helpers after restart. The original
`CRAWLER_STEALTH=0` also disables its identity profile. This mode does not start
CAPTCHA vendors, account registration, proxy services or a remote host solver,
and it does not guarantee access to every site.

Set `CRAWLSCOPE_MAIN_CRAWLER_HELPERS=0` to disable these bridges without deleting
the retained source. Never put credentials in frontend `VITE_*` variables.

## Features retained as optional, not activated

| Feature family | Source location | What is needed before use |
| --- | --- | --- |
| CAPTCHA classification, token solving, OCR and verification | `crawler/crawler/captcha*.py`, `camofox_client.py`, solver scripts | The specific supported provider/runtime and keys/model resources; explicit activation and budget. No paid tasks were started. |
| CamoFox / C4 host solver | `crawler/crawler/camofox_client.py`, `hostsolver.py`, `crawler/scripts/host_solver.py`, deployment files | Compatible running services, service authentication and host/GPU/browser configuration. No remote service was contacted. |
| Account creation, login and session recipes | `crawler/crawler/siteauth.py`, `tempmail.py` | A separately authorised account workflow and credentials/session or signup approval. Upstream credential storage must be reviewed/hardened before activation. No accounts were created or login attempted. |
| Proxy routing and spend budgets | `crawler/crawler/paid_proxy.py`, `camofox_client.py` | Configured services/credentials, host allowlist and spend limits. No proxies purchased or rotated. |
| Complete engine, browser pool and governors | `crawler/crawler/async_engine.py`, `dedup.py`, `fleet.py` | Isolated engine environment and runtime paths; not the current serial audit worker. |
| Document/PDF text and tables, media/attachments | `crawler/crawler/pdfextract.py`, `extract.py`, `storage.py` | Separate opt-in workflow. Never used by normal coverage: files stay URL-only. |
| Postgres/MinIO ingestion | `crawler/ingest_api/`, `crawler/crawler/ingest_client.py` | Separate authorised write targets and credentials. Normal CrawlScope never writes to production Corpus. |
| Scheduling, categories, sharding and fleet control | `orchestrator/src/mallory_orchestrator/` | Isolated database/service topology and operator configuration. Not launched by the dashboard. |
| Wayback/archive replay and specialised harvesters | `crawler/crawler/c2.py`, `sipri_harvester/`, `special_request_crawler/` | Their original dependencies and explicit workflow. Historical replay is not substituted for live website evidence. |
| Original service APIs, dashboards and health checks | `crawler/crawler_api/`, `healthcheck/`, deployment definitions | Service configuration, access controls and isolated runtime paths. Not mounted as CrawlScope routes. |

Installing the original pinned dependencies into CrawlScope's existing `.venv`
could downgrade working libraries. Use a separate environment/deployment when
activating optional production components; do not run the original setup scripts
or production tests against your friend's services by accident. Source copying
alone is not complete runtime integration of these optional services.

## Preserved coverage rules

1. Only meaningful HTML text is fetched/extracted for text coverage.
2. PDFs/documents/images/media remain URL-only inventory and Corpus-presence checks.
3. URLs are fully processed one at a time; Corpus requests stay paced and read-only.
4. Validity is established from actual response/content evidence. CAPTCHA/login
   forms, unknown HTTP status, stale archive replay and failed fetches are not
   declared valid pages or confirmed crawler misses.
5. Existing saved audits and the running scan are preserved. No service/process
   was stopped and no source ZIP entrypoint was executed as part of importing it.

## Local inspection and verification

Verified in this checkout on 12 October 2026: **248 CrawlScope Python tests
passed**, **12 frontend tests passed**, frontend formatting/production build,
Python symbol lint and Git whitespace checks passed. All **485 imported artifacts**
passed integrity verification, with no missing/changed files or Python syntax
errors. There are two non-failing warnings: an upstream Starlette/HTTPX deprecation
and an original test module's invalid escape sequence. The original production
test suite was not executed; source checks do not prove every external service works.

```powershell
.\.venv\Scripts\python.exe scripts\main_crawler_features.py
.\.venv\Scripts\python.exe -W ignore scripts\main_crawler_features.py --verify
```

The first prints what is active/optional. The second checks every imported artifact
and parses all Python source without running the original engine. It recognises
line-ending-only Git changes separately from code changes. It does not contact
websites, solvers or Corpus, start a scan, submit forms, or write database state.

The read-only local API `GET /api/main-crawler/features` returns the same feature
states and source counts, without keys, service URLs or configuration values.

`pytest.ini` scopes the default test run to CrawlScope's `tests/`, so copying
original test files cannot accidentally execute production integration tests.
`tests/test_main_crawler_integration.py` verifies source completeness/integrity,
safe import/exclusions, malformed archive refusal, actual discovery hooks,
vendor-wall distinctions, optional browser identity, private module loading and
read-only inspection. Existing Corpus, resource and serial-flow tests still apply.

Finish the active audit before stopping/restarting the backend. New code applies
to subsequent scans after restart; retained historical blocks are not retroactively
marked successful. The normal start command remains:

```powershell
npm.cmd --prefix frontend start
```

# Production crawler ZIP: integration review

**Historical selected-adaptation review.** The subsequent user-approved complete
source import now includes all 311 Python source files and retains the previously
excluded feature code as optional. See [MAIN_CRAWLER_INTEGRATION.md](MAIN_CRAWLER_INTEGRATION.md)
for current connected/optional states. “Not copied” statements below describe the
earlier adaptation, not the current source tree; optional services are still not
activated merely by importing their files.

Reviewed 12 October 2026. Source: user-supplied `C:\Users\vijay\Desktop\Crawler.zip`.
SHA-256: `4356d544ed167ef56e97ad4bbbc45062d9c355a8ab6e09b8e32e6ce907853d59`.

## Scope and source counts

The inspected ZIP has a **27-item operator problem checklist**, **25 reported
edge-case benchmark categories**, and **118 crawler test modules**. No numbered
116-feature manifest was identified. Test modules are not independent features;
none of these numbers establishes complete feature parity.

[ZIP_SOURCE_INDEX.md](ZIP_SOURCE_INDEX.md) indexes every crawler test module and its
test functions by reading Python syntax without executing it. It is a source
inventory, not a claim that the original tests were run or every behaviour ported.
The friend's definitive numbered 116-item list is still needed to verify that claim.

Embedded instructions and historical benchmark results were treated as reference
data. No ZIP scripts, Git hooks, Docker services, production tests, production
credentials, database cleanup, account creation or registration were executed.

## Compatible adaptations in this update

| ZIP mechanism | CrawlScope module | Implemented behaviour |
| --- | --- | --- |
| `fetcher._http_fetch` response budget | `validation/http.py` | Declared-length and decoded streamed-body caps, 8 MiB by default. Oversized bodies are discarded, not accepted as complete HTML. Bounds retained content, not all transport/browser peak allocations. |
| `errors` status/transport policy | `validation/failure_policy.py`, `validation/http.py` | Bounded retries for 408/425/429 and 5xx except 511; named 522–526 origin errors; no repeated certificate-failure or redirect-loop retry. Website TLS remains verified. |
| Retry-After and quota detection | `validation/failure_policy.py`, `validation/http.py` | Numeric/date Retry-After; finite-number checks; remaining/reset and structured `r=0;t=...` headers. A successful final allowed response stays valid while the next request waits. |
| `edgecase` body-driven walls | `validation/access.py` | HTTP-200 CDN/throttle/region/subscription/age/explicit JSON-auth walls and redirected consent detection. Do not mistake normal security prose, contact CAPTCHA widgets or cookie-policy pages for walls. Heuristic, not universal. |
| `_is_js_shell_signal` / rendering | `validation/access.py`, `auditor.py` | Empty/loading script-driven mounts request normal browser rendering; validate actual returned content. An HTTP failure does not permanently mark its host JavaScript-only. |
| Lazy images and malformed links | `discovery/static_links.py` | Lazy image URLs, responsive variants and video posters; malformed href/base isolation so other links survive. No resource content downloads. |
| `parse.links_from_json` | `discovery/api_pagination.py`, `discovery/resources.py` | Explicit next URLs/cursors, preserved repeated filter values, bounded next-page/offset inference only with populated results and continuation evidence. No invented cursor, unchanged-cursor loop, empty-page continuation or page-index guess based solely on total items. |
| Sitemap/robots resource safety | `discovery/sitemap.py`, `auditor.py` | Decoded gzip sitemap cap with an explicit boundary. Failed/oversized robots content cannot become allow-all policy. |
| `_bounded_close` | `discovery/browser.py` | Five-second owned page/context/browser/driver close deadlines; continue subsequent cleanup and report failure. No broad process killing or claim of full browser-pool/process-reaper parity. |
| Named failure reporting | `analysis/diagnostics.py` | Explain size limits, redirect chains, certificate trust and access gates in the existing Activity view; no new frontend controls. |

These are compatible adaptations, not a drop-in import of the production engine.

## All 27 entries in the ZIP's operator checklist

“Retained” means the relevant implementation exists, not that every source
benchmark variant passed locally. “Partial” explicitly identifies an unresolved
boundary. Source numbers intentionally have gaps.

| # | Source problem | CrawlScope disposition |
| --- | --- | --- |
| 1 | Robots blocking | Retained permission/crawl-delay checks; unknown 5xx/network/oversize policy is a disclosed boundary. No robots bypass. |
| 2 | IP blocking/rate limiting | Improved serial pacing, shared HTTP/browser cooldown, retries and quota/reset headers. Persistent refusal remains incomplete discovery. |
| 3 | CAPTCHA | Detection and manually prepared public-cookie session reuse retained. OCR, challenge clicker, paid solving and token injection not copied. User action or owner allowlisting may be needed. |
| 4 | Login/authentication | Detection improved, including explicit JSON auth and network login. Public-only/no-account scope retained; no password guessing, login or signup. Login forms never count as content. |
| 5 | Geography restriction | Detection improved. No proxy/geographic bypass; restricted pages remain excluded/unverified. |
| 6 | WAF/bot detection | Retained provider/header/structure detection, ordinary browser/session recovery; added HTTP-200 CDN-wall checks. C3/C4 stealth/host-solver parity not implemented. |
| 7 | JavaScript-rendered pages | Improved empty app-mount recognition; retained Chromium, script diagnostics and post-render validity checks. |
| 8 | Lazy loading/infinite scroll | Retained bounded controls, scroll, observed XHR and tab discovery; lazy resource URLs improved. Partial: arbitrary/custom interactions need site adapters. |
| 9 | Broken HTML | Retained recovering lxml parsing; invalid individual href/base no longer discards all static/JSON links. Other extractors remain subject to site-specific failures. |
| 10 | Redirect chains/loops | Retained HTTPX hop cap; classified and stopped without repeating the entire failed chain. |
| 11 | 404/dead/soft-404 | Retained validation and saved outcomes. No cross-audit permanent-gone cache: restored pages must be testable again. |
| 12 | Server errors | Improved bounded 5xx retry policy and named origin failures. Failure is never a confirmed Corpus miss. |
| 13 | Timeouts | Retained per-I/O plus whole-attempt budget, cancellable waits, bounded retries and visible limits. |
| 14 | SSL/TLS | Typed certificate failure; keep verification enabled, stop repeat retries. No proceed-anyway/insecure-host bypass. |
| 15 | DNS | Typed resolver failure and bounded retries. No guessed domain rewrite that changes the target website. |
| 16 | Duplicates | Retained normalized unique-URL storage and fingerprint diagnostics/trap checks. Do not delete valid aliases solely for matching text hashes: each may differ in Corpus. |
| 17 | Encoding | HTTP charset retained; BOM/HTML meta fallback added. Ambiguous/unsupported encodings remain possible. |
| 18 | Non-HTML files | URL collection/Corpus presence only, by requirement. No PDF extraction, OCR, downloads, MinIO storage or document text checks. |
| 19 | Pagination | Retained numeric/path probes, observed links/cursors, browser transitions; improved JSON continuation. Partial: non-monotonic/custom series have no guaranteed maximum. No production depth/page ceilings copied. |
| 20 | Tracking/session URL variations | Retained tracking normalization, semantic filter/pagination queries and session-trap checks. Cookie secrets stay outside reports. |
| 21 | Calendar/facet/trap spaces | Partial: repetition checks and disclosed URL/action/filter/probe safeguards. Blanket future-year/locale/depth exclusions are not copied because they can hide real content. |
| 23 | Canonical mismatch | Retained actual canonical/redirect Corpus evidence. Canonical tags do not automatically merge all distinct URLs. |
| 24 | URL normalization | Retained scheme/host/default-port/query/tracking handling, case-sensitive paths and SPA hash routes. No blind HTTP/HTTPS or www/apex collapse. |
| 27 | Server overload | Retained one URL at a time, website spacing and one-URL paced Corpus requests. No production fleet/governor concurrency copied. |
| 28 | Stale/changing content | Retained capture dates, versions and hashes. No production conditional-304 body omission in fresh discovery audits. |
| 29 | A/B variants | Partial: capture the observed session and dates/fingerprints; cannot claim discovery of every personalised variant. |
| 30 | Inconsistent structures | Retained generic static/structured/feed/browser extraction. Partial: arbitrary layouts/controls need adapters. No production keyword relevance gate copied. |

## Explicit exclusions and prerequisites

- CamoFox, GPU/host solver, paid CAPTCHA vendors and proxy tiers require separate
  infrastructure/configuration; copying Python does not enable them. They were not
  installed/configured/contacted, and bypass parity is not claimed.
- `siteauth.establish()` creates disposable accounts, confirms emails and submits
  login forms. These are external writes outside the established public-only scope.
  They were not copied. A future account workflow needs explicit authorisation and
  an account/session the user is entitled to use; existing production cookies and
  passwords were not imported.
- CAPTCHA OCR, automated interactive solving and token injection were not copied.
  An embedded verification widget on a readable page is not itself blocked content.
- Automatic Wayback substitution would turn old snapshots into supposed live
  website evidence. It is not used to fabricate discovery success.
- Keyword gates, fixed depth caps and hash-based corpus deletion would reproduce
  production omissions or destroy comparison data. They are intentionally excluded.
- Fleet/ingest/Postgres/MinIO writes, distributed frontier moves, category cadence,
  SIPRI/tender extraction, cgroup governors, shared multi-batch browser pools and
  force-restart/process-tree watchdogs are a different architecture. CrawlScope's
  serial desktop pacing/cleanup is not represented as equivalent.
- The ZIP's historical benchmark scores are not CrawlScope results or access guarantees.

## Tests and rollout

Verification for this checkout on 12 October 2026: **233 Python tests passed**;
**12 frontend tests passed**, formatting checks and production React build passed;
Python undefined/unused-symbol lint and Git whitespace checks passed. Python emits
one upstream Starlette/HTTPX deprecation warning. These are CrawlScope results,
not results from running the ZIP's source test suite.

`tests/test_zip_resilience.py` exercises policy/cooldowns, typed failures, body caps,
meta encoding, wall-vs-article classification, JS shells, malformed/lazy links,
JSON continuation, oversized robots, gzip sitemap bounds and owned cleanup using
isolated fixtures/mocks. Existing public access, URL-only resource, serial workflow,
Corpus API and frontend regression tests remain applicable. No third-party challenge
or production Corpus load test was performed.

Existing saved scans/data are preserved. No extra UI controls were added. Only HTML
text is compared; Corpus remains read-only and paced one URL at a time. Historical
blocked URLs are not retroactively marked successful. New discovery/validation logic
applies to new scans after backend restart. Finish the active scan before restarting.

```powershell
npm.cmd --prefix frontend start
```

The launcher reuses a running server; it does not reload changed Python modules.
After the active scan finishes, stop its server with Ctrl+C in its terminal and
start again. Existing public-session helper remains available for legitimate manual
verification; it does not promise to clear every challenge or grant account access.

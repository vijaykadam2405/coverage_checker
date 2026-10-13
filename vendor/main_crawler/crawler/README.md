# Layer 1 — KSSL Defence CI Crawler (acquisition engine)

A **job-driven crawler** for the KSSL competitive-intelligence platform. It takes a
*crawl job* (URL + keywords + budget), **harvests** raw web assets, **filters** them
down mechanically (keyword relevance), and **POSTs one raw page bundle per
kept page** to the Ingest API — source URL, HTML, main text, images, PDFs, screenshot,
a mechanical summary, and informational detection tags. It acquires and normalizes
only — it never scores, ranks, judges relevance-to-strategy, or classifies pages into
typed business records (that's all Layer 2, operating on the raw bundle).

Built to `docs/01_CRAWLER_CONTRACT.md`. This is the **testing-phase** build: the fixed
~12-job set from §8, run **once**, proving the 7 exit criteria end-to-end. No
scheduling / cadence / full-universe sweep (those are a later production phase).

```
 docs/seed/*.json ─▶ [job generator] ─▶ crawl jobs ─▶ ┌─────────── CRAWLER ───────────┐
                                                       │ HARVEST → FILTER → EXTRACT     │ ─▶ POST /ingest/v1/page
                                                       └────────────────────────────────┘        (Layer 2 stub)
```

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python run.py testing      # run the §8 batch ONCE, offline, and check the 7 exit criteria
python run.py gen          # generate ~144 scoped jobs from the seed -> jobs/generated_jobs.json
python run.py run jobs/testing_batch.json   # run any job file end-to-end (in-process ingest)
python run.py serve        # serve the stub Ingest API + dashboard on http://127.0.0.1:9090
python run.py crawler-api  # serve the Crawler API (job in -> page bundles out) on http://127.0.0.1:8099
pytest -q                  # unit + integration tests
```

`run.py testing` is the headline command — it prints a per-job table and a PASS/FAIL
line for each of the 7 exit criteria, and writes accepted page bundles to
`data/output/ingested.ndjson`.

### See it in a browser

```bash
python run.py testing      # produces data/output/ingested.ndjson
python run.py serve        # then open the dashboard
```

- **http://127.0.0.1:9090/** — dashboard: every ingested page as a card (stream badge,
  source+tier, cleaned title, detection tags, resolved-entity chips, and the actual
  screenshot / image / PDF / video links, served from local storage).
- **http://127.0.0.1:9090/v1/docs** — Swagger for the Ingest API.
- **http://127.0.0.1:9090/stats** — accept-rate per source.

## Crawler API (what Layer 2 / a job generator calls)

`python run.py crawler-api` → **http://127.0.0.1:8099**

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/crawl` | run one job → raw page bundles |
| `POST` | `/v1/crawl/batch` | run `{ "jobs": [ … ] }` |
| `GET`  | `/v1/schema` | JSON schema for the job input + the document output |
| `GET`  | `/v1/docs` | Swagger |

**Input** = a crawl job (§2). **Output** = one `document` bundle per kept URL, each with
an `accepted` / `failing_rule` verdict in the summary.

```bash
curl -X POST http://127.0.0.1:8099/v1/crawl -H "Content-Type: application/json" -d '{
  "job_id":"j1","job_type":"news","seed_urls":["https://idrw.org/lt-k9-vajra-followon/"],
  "keywords":["L&T","K9 Vajra","artillery"],"target_entity":"LT",
  "capture":["html","text","images","screenshot"]}'
# -> { job_id, summary{...}, documents:[{url, html, main_text, images, attachments,
#      screenshot, stream, detected_competitor, detected_countries, ...}] }
```

Two flows, both supported:
- **Pull** (default) — page bundles are returned inline in the response. Best for Layer 2 testing.
- **Push** — add `"forward_to_ingest": true` and each bundle is also `POST`ed to the
  Ingest API (`INGEST_BASE_URL`, default `:9090`) — the production flow.

### Handing this to Layer 2 for testing

Any of three ways (all use the exact same contract shape):
1. **Sample file** — hand them `data/output/ingested.ndjson` (real `{document}`
   bundles). Zero infra; they load it straight into their scoring/classification.
2. **Call the Crawler API** — they `POST /v1/crawl` and consume the returned bundles
   (pull). `GET /v1/schema` gives them the shape to code against.
3. **Push to their ingest** — set `INGEST_BASE_URL=https://their-host` and run with
   `forward_to_ingest:true`; we `POST /ingest/v1/page` to *their* endpoint. Our
   `ingest_api/` doubles as a reference implementation of a compliant receiver.

## The three stages

1. **HARVEST** (`harvest.py`, `fetcher.py`) — a BFS frontier expands from `seed_urls`
   within `max_depth`/`max_pages`, respecting `same_domain_only`, `crawl_delay`, and
   robots defaults. httpx fast-path with an optional Playwright render for `render_js`.
   Conditional GET (`If-None-Match`/`If-Modified-Since`) → a `304` short-circuits the
   page before download. Live fetches honor **robots.txt** (per-host cache, fail-open)
   and record **video/audio as metadata-only links** (never downloaded, §4).
2. **FILTER** (`gate.py`) — the *mechanical* keyword-relevance gate: keep a page if it
   matches ≥1 keyword (plus freshness, `freshness_days`). Entity resolution runs here
   too but is info-only — it labels the keep reason and fills the `detected_*` tags, it
   never drops a page. No threat judgment.
3. **EXTRACT** (`extract.py`) — build one `document` bundle (clean `main_text` via
   trafilatura, raw `html`, `content_hash`, language + `main_text_en`, every non-junk
   image, PDF attachments + extracted text, one full-page screenshot, tables,
   `entities_detected`, and flat `stream`/`detected_*` tags), then POST it.

**Self-dedup (§7A)** sits between FILTER and EXTRACT: a kept page is sent only when its
URL is new **or** its `content_hash` changed since our last crawl (SQLite
`crawl_pages`), and a same-run duplicate (two URLs rendering to byte-identical content —
e.g. an SPA redirect-to-home) is also skipped. Re-crawling unchanged content sends
nothing — it 304s or skips. The crawler dedups against *itself*; cross-source
clustering (5 outlets, one event) is L2's job, so each distinct URL is sent with its
own hash.

## Record classification moved to Layer 2

L1 no longer constructs the six typed records (`competitive_signal`, `tender`,
`partnership`, `geo_footprint`, `innovation`, `company_event`) with bespoke field
extraction (money/date/quantity parsing, deal-stage cues, partner-of lookups). That
classification now happens in Layer 2, reading the raw `main_text`/`html` plus the
informational `entities_detected`/`stream`/`detected_*` tags this crawler still
computes (see below) — see `docs/01_CRAWLER_CONTRACT.md` §5.

## Source identity + trust tier

Every document gets a `source_id` + `source_tier` (1 primary / 2 trade press / 3 aggregator)
plus provenance flags `source_known` and `source_resolved_by`. Resolution precedence:

1. **Job-stamped** — the orchestrator stamps `source_id`/`source_tier`/`source_type`/
   `source_region` on the job (it owns the master Source Catalog); used verbatim. → `job`
2. **Registry** — curated domains in `source_registry.json` (Janes=1, MoD=1…). → `registry`
3. **Heuristic** — classify an unknown domain: `*.gov*/.mil`→tier 1, manufacturer/defence-org
   sites→tier 1, curated trade-press/think-tank→tier 2. → `heuristic`
4. **Fail-safe** — nothing matched → `aggregator`, **tier 3**, `source_known=false`. → `fallback`

`source_id` is the public-suffix-aware eTLD+1 (via `tldextract`), so `www./m./news.`
subdomains collapse to one id (`raksha-anirveda.com` → `RAKSHAANIRVEDA`). **Unknown domains
never get tier 1** (the old `COMPANY_IR` catch-all is gone), and **tier never affects
keep/drop** — it's metadata; Layer 2 tunes tiers dynamically from `/stats` accept-rates.

## Entity resolution (§6) — kept in L1, informational tags only

Alias/string matching (flashtext trie) against the seed (`watchlist_entities`,
`watchlist_products`, `watchlist_tech_domains`, tender countries) → `entities_detected[]`
with `resolved_id` + confidence. This does **not** gate keep/drop (the keyword gate does
that); its output is surfaced as flat tags on the document — `stream`,
`detected_competitor`, `detected_products`, `detected_countries`, `detected_tech_domains`
— rather than being fanned out into typed records. Detected items are ordered by first
appearance so a page's primary subject is selected. A defence-company-shaped name not
in the seed is flagged `resolved_id:null, type:"unknown_company"` (discovery signal —
flagged, never dropped; also listed in `detected_unknown_companies`).

## Stub Ingest API (§9)

`POST /ingest/v1/page` with a `{document}` bundle (no separate "record" — one raw page
bundle per kept page). Enforces the two acceptance rules (valid document, ISO dates) and
returns `422 {failing_rule}` on rejection. `/stats` reports accept-rate per source and a
`by_stream` breakdown. The validation logic is a pure function
(`ingest_api/validation.py`) reused by the API and the tests.

## Live fetch with fixtures fallback

Many defence sources (Janes, MoD portals) are paywalled / JS-gated / bot-blocked, so
the §8 batch ships with realistic fixtures (`tests/fixtures/`) keyed by canonical URL.
The fetcher serves a fixture when one exists; otherwise it goes to the network
(`CRAWLER_ALLOW_NETWORK=1`). `run.py testing` runs **offline** for determinism.

| Env | Default | Effect |
|---|---|---|
| `CRAWLER_PREFER_FIXTURES` | `1` | try a fixture before the network |
| `CRAWLER_ALLOW_NETWORK` | `1` (`0` in the test harness) | allow live fetch |
| `CRAWLER_SEED_DIR` | `../docs/seed` | seed location |
| `CRAWLER_DATA_DIR` | `./data` | sqlite + storage + output |

## Layout

```
crawler/         seed · canonicalize · resolver · fetcher · harvest · dedup · gate ·
                 textextract · pdfextract · images · screenshot · extract · pipeline ·
                 ingest_client · jobgen · testing_batch · storage · translate · models
ingest_api/      app.py (FastAPI stub) · validation.py (page acceptance rules)
tests/           unit + integration + fixtures (HTML/PDF/JPG + index.json)
scripts/         gen_fixtures.py (regenerates the PDF/image fixtures)
run.py           CLI · run_testing_batch.py  exit-criteria harness
```

## Design notes & honest caveats

- **Reuse:** URL canonicalization, the change-detection classifier, main-text/content-
  hash extraction, and the httpx→render fetch pattern are adapted from a proven
  reference crawler. The defence-specific layer (seed resolution, the mechanical gate,
  the ingest acceptance rules) is bespoke to this contract.
- **Translation** (`main_text_en`): production plugs a real MT provider via
  `translate.set_provider`. Offline, the test build serves a fixture translation for the
  one non-English source (keyed by canonical URL) — it never fabricates a translation.
- **Screenshots:** Playwright full-page PNG when installed; otherwise a Pillow-rendered
  "evidence card" (clearly labelled as a text fallback) so capture + storage always work
  offline. `data/storage/` is a local stand-in for `s3://mallory-raw/`.
- **Detection tags are informational, not classification** — `stream`/`detected_*` are
  flat, mechanical summaries of `entities_detected`, included for L2's convenience; deep
  business-record construction (deal value, deadlines, partner identification, event
  typing) is entirely Layer 2's job now, operating on the raw `main_text`/`html`.
```

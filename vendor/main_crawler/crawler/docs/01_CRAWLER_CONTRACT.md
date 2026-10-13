# LAYER 1 — Crawler Build Spec (standalone)

> **Read this alone.** This document is self-contained so a fresh session can build the crawler with no
> other context. It defines the crawler as a **job-driven engine**: it receives a *crawl job*
> (URL + keywords + budget), harvests raw web assets, **filters** them by keyword relevance, and
> returns **one raw page bundle per kept page** (source URL, HTML, main text, images, PDFs, screenshot,
> a mechanical summary, plus informational detection tags) to our Ingest API. The real seed data it
> targets lives in `docs/seed/*.json` (already populated — not placeholders).
>
> **L1 does not construct typed records.** Deep classification (tender/partnership/geo_footprint/
> innovation/company_event/competitive_signal field extraction — money/date/quantity parsing, deal-stage
> cues, partner-of lookups, etc.) is Layer 2's job, operating on the raw text this crawler hands over.
> L1 does keep its own keyword relevance gate and its seed-alias entity resolver (§6) — the keyword gate
> decides *whether* a page is worth sending; the resolver never drops a page, its output rides along as flat informational tags
> on the page bundle (`stream`, `detected_competitor`, `detected_products`, `detected_countries`,
> `detected_tech_domains`, `detected_unknown_companies`) for L2's convenience, not as a record
> classification.

---

## 0. Context (what this crawler feeds)

We are building a defence competitive-intelligence platform for **KSSL** (Kalyani Strategic Systems Ltd),
an Indian defence manufacturer. KSSL is the fixed **anchor**: everything is read "vs KSSL". The platform
has 3 layers; **this crawler is Layer 1 (acquisition).** It feeds Layer 2 (data engineering), which scores
and ranks everything, which feeds Layer 3 (the client UI). The crawler's entire world is:

```
   our seed (who/what to watch)          your output (raw page bundles)
   docs/seed/*.json   ───▶  [ CRAWLER ]  ───▶  POST /ingest/v1/page  ───▶  Layer 2
```

**The crawler acquires and normalizes. It never scores, ranks, or judges relevance-to-strategy, and it
does not construct typed business records.** "Is this a threat?" and "what kind of event is this?" are
Layer 2's calls. "Is this page about a watched entity and does it parse?" is yours.

---

## 1. The crawler is a job engine (the correct mental model)

A web crawler can bring back anything. We constrain it with **jobs**. A *crawl job* is the unit of work.
**We generate jobs from the seed** (our logic, see §7) and hand them to you; **you execute them** and
return raw page bundles.

```
            ┌──────────── one CRAWL JOB ─────────────┐
  seed ──▶  │ seed_urls[], keywords[], max_pages,     │ ──▶ [ HARVEST ] ──▶ raw assets
 (logic)    │ max_depth, capture[], render_js, ...    │        (html/js/img/pdf/shot)
            └─────────────────────────────────────────┘              │
                                                                      ▼
                                              [ FILTER + EXTRACT ] ──▶ one raw page bundle
                                              (relevance gate, clean,   + detection tags
                                               resolve, dedup)               │
                                                                              ▼
                                                                    POST /ingest/v1/page
```

Three stages, all yours to build:
1. **Harvest** — fetch within the job's budget; capture the raw asset types the job asks for.
2. **Filter** — drop anything that doesn't match a keyword (mechanical, not strategic). Entity resolution runs here too but only generates info tags — it never drops a page.
3. **Extract** — assemble the raw page bundle (text/html/images/pdf/screenshot + detection tags) and POST it.

---

## 2. JOB INPUT — what a crawl job contains (we give you this)

```jsonc
{
  "job_id":        "job_2026-06-29_LT_news_01",
  "job_type":      "news",            // news | tender | profile | spec | patent_aux
  "seed_urls":     ["https://idrw.org/?s=L%26T+defence", "https://janes.com/search?q=L%26T"],
  "keywords":      ["L&T", "Larsen & Toubro", "K9 Vajra", "artillery"],   // must-match (any)
  "target_entity": "LT",              // watchlist competitor id, or null for tenders
  "max_pages":     40,                // hard crawl budget (pages fetched)
  "max_depth":     2,                 // link-follow depth from each seed_url
  "same_domain_only": true,           // don't wander off the seed domain
  "render_js":     false,             // true → use a headless browser (JS-heavy sites)
  "freshness_days":120,               // ignore content published older than this
  "capture":       ["html","text","images","screenshot"]  // raw asset types to grab (see §4)
}
```

**You own everything inside the job; we own which jobs to send.** If a field is absent, use the
`source_registry.json → global_capture_defaults`.

### Recommended job parameters (our suggestion — tune from results)

| job_type | seed example | max_pages | max_depth | render_js | capture |
|---|---|---|---|---|---|
| `news` (signals/events) | news-site search URL for an entity | 30–50 | 2 | only if JS-gated | `html, text, images, screenshot` |
| `tender` | procurement portal listing/search | 50–80 | 2 | often true (portals) | `html, text, pdf, screenshot` |
| `profile` (partnerships/geo) | company IR/press page | 20–40 | 2 | sometimes | `html, text` |
| `spec` (product specs) | OEM product page / datasheet | 5–15 | 1 | sometimes | `html, text, images, pdf, screenshot` |
| `patent_aux` (corroborate) | a patent landing page | 1–3 | 0 | no | `html, text` |

> Depth ≥3 rarely helps and explodes cost — keep it at 1–2. `max_pages` is a hard stop, not a target.

---

## 3. JOB OUTPUT — the raw page bundle (you give us this)

This is the heart of the contract. Raw harvest is huge and messy; **the gate keeps only relevant pages,
and each kept page becomes one `document` bundle — the raw material, not a business record.** The gate is
**mechanical**: keep a page if it matches ≥1 keyword (plus the freshness window, if set). Entity
resolution is not a keep condition — it runs on every kept page and its result is packed on as
informational tags for Layer 2. No typed record is constructed — that classification is Layer 2's job.

### 3.1 Raw → Kept (what you keep vs throw away)

| Raw harvest (you fetch) | Kept output | Thrown away |
|---|---|---|
| Full HTML with nav/ads/scripts | `main_text` (cleaned article body) **and** raw `html` (both sent) | nothing structural — nav/footer stripped only from `main_text`, `html` stays complete |
| Every `<img>` on the page | Every non-junk image (product, event, chart, map, etc.) | logos, avatars, tracking pixels, tiny/decorative icons |
| All linked files | **All** PDFs/downloaded files (+ extracted plain text) | unrelated non-document downloads |
| Inline JS / API responses | Not captured (out of scope) | the JS itself |
| A screenshot of the page | 1 **full-page screenshot** as audit evidence | — |
| Every outbound link | — (links are for crawling, not output) | all of them |

L2 does its own relevance/importance judgment on the full raw materials — L1 no longer pre-curates down
to "0–3 meaningful images" or extracts money/date/spec fields; it sends everything non-junk it found.

### 3.2 The `document` object (the raw page bundle — one per kept URL)

```jsonc
{
  "url":            "https://janes.com/defence/india/lt-k9-followon",  // canonical, dedup key, REQUIRED
  "content_hash":   "sha256:9af1...",                                  // hash of main_text, REQUIRED
  "fetched_at":     "2026-06-29T14:30:00Z",                            // REQUIRED
  "source_id":      "JANES",                                          // from source_registry, REQUIRED
  "source_tier":    1,
  "title":          "L&T secures ₹4,500 cr K9 Vajra follow-on order",  // REQUIRED
  "author":         "Staff Reporter",
  "published_at":   "2026-06-28T00:00:00Z",
  "date_precision": "exact",                  // exact | approx | unknown
  "language":       "en",
  "access":         "open",                   // open | paywalled | partial
  "main_text":      "The Ministry of Defence has...",   // cleaned body, REQUIRED — Layer 2 runs NLP on this
  "main_text_en":   null,                     // English translation if language != en
  "html":           "<!doctype html>...",     // raw source HTML of the page
  "summary":        "L&T won ₹4,500 cr for 100 K9 Vajra guns.",  // mechanical extractive 1–2 lines
  "images": [
    { "url":"https://.../k9.jpg", "storage_path":"s3://mallory-raw/img/abc.jpg",
      "caption":"K9 Vajra-T on trials", "role":"product", "width":1200, "height":800 }
  ],
  "attachments": [
    { "url":"https://.../rfp.pdf", "storage_path":"s3://mallory-raw/doc/xyz.pdf",
      "type":"pdf", "extracted_text":"REQUEST FOR PROPOSAL ..." }
  ],
  "screenshot":   { "storage_path":"s3://mallory-raw/shot/abc.png", "captured_at":"2026-06-29T14:30:05Z" },
  "tables": [ { "title":"Order breakdown", "rows":[{"item":"K9 Vajra","qty":"100","value":"₹4,500 cr"}] } ],
  "entities_detected": [
    { "surface":"L&T",      "resolved_id":"LT",      "type":"competitor", "confidence":0.98 },
    { "surface":"K9 Vajra", "resolved_id":"K9VAJRA", "type":"product",    "confidence":0.95 },
    { "surface":"India",    "resolved_id":"India",   "type":"country",    "confidence":0.99 }
  ],
  "stream":                  "competitive",   // informational tag: competitive | tender | market | technology
  "detected_competitor":     "LT",            // informational tag, from entities_detected
  "detected_products":       ["K9VAJRA"],
  "detected_countries":      ["India"],
  "detected_tech_domains":   ["artillery"],
  "detected_unknown_companies": []            // surfaced, not dropped — how L2 discovers new competitors
}
```

That's the entire output — no separate typed record is attached. `stream`/`detected_*` are flat,
informational summaries of `entities_detected` (see §6), included as a convenience for L2, not a
classification of the page.

---

## 4. Capture types — when to grab html / js / images / screenshots / media / pdf

This answers "what should the crawler bring?" per asset type. Grab only what the `capture[]` list asks for.

| Capture type | Grab when | Keep in output? | Notes |
|---|---|---|---|
| `html` | always (it's the page) | YES → `html` | full raw HTML sent alongside `main_text`, no longer discarded |
| `text` | always | YES → `main_text` | the cleaned body; **the single most important field** |
| `images` | product/spec/event/innovation jobs | YES → `images[]` (all non-junk) | download to storage; tag `role`; skip only logos/icons/tracking pixels |
| `pdf` | tender jobs (RFP), primary docs | YES → `attachments[]` + extracted plain text | fetch + store + extract text for every linked PDF |
| `screenshot` | news + tender + spec jobs | YES → `screenshot` | **audit evidence** — sources change/disappear; one full-page shot proves what we saw |
| `media` (video/audio) | only if explicitly requested | metadata only (url, title) | do NOT download large media; just record the link |
| `js` | only when data is in a JS payload | NO (extract data, drop js) | use for sites that render content client-side |

**Screenshots are evidence, not decoration.** Because defence sources frequently edit or pull stories, a
timestamped screenshot is our audit trail behind every signal the CEO sees. One per kept document.

---

## 5. Record classification — now Layer 2's job

Earlier revisions of this contract had L1 construct six typed records (`competitive_signal`, `tender`,
`partnership`, `geo_footprint`, `innovation`, `company_event`) with bespoke field extraction — money/date/
quantity parsing, deal-stage cues, partner-of lookups, tender ref/issuer detection, etc. **That
classification now happens in Layer 2**, operating on the raw `main_text`/`html` and the informational
detection tags (§3.2, §6) this crawler hands over. L1 sends exactly one `document` bundle per kept page —
no separate record, no `document_id`-linked side objects.

If a future L2 needs a lighter-weight signal than "the whole raw page," build that classification as an
L2-side service reading `POST /ingest/v1/page` bundles — do not re-add per-type extraction into L1.

---

## 6. Entity resolution (mechanical, kept in L1 — informational tags, not a gate)

L1 still runs a mechanical seed-alias resolver, but it does **not** decide whether a page is kept — the
keyword gate (§3) does that. The resolver's job is to surface flat informational tags on the document
(which watched competitor / product / country / tech-domain a kept page mentions) for L2's convenience,
rather than fanning the page out into typed records.

For every kept page, match surface forms against `docs/seed/watchlist_entities.json` (aliases) and
`watchlist_products.json`. Populate `entities_detected[]` with `resolved_id` + `confidence`, and derive
the flat tags:
- `detected_competitor` — the resolved competitor id (job's `target_entity` preferred if it resolves).
- `detected_products` / `detected_countries` / `detected_tech_domains` — straight lists from
  `entities_detected`.
- `detected_unknown_companies` — a defence company **not** in the seed, recurring across docs, surfaced
  as `resolved_id:null, type:"unknown_company"` in `entities_detected` and listed here too. This is how
  we discover new competitors to add. (Don't drop it; flag it.)
- `stream` — a coarse page classification (`competitive` | `tender` | `market` | `technology`), not a
  business-event type.

This is string/alias matching — **not** a judgment about importance, and not a substitute for L2's own
deeper NLP over `main_text`.

---

## 7. How jobs are generated from the seed (our side — for your awareness)

You don't build this, but knowing it clarifies the contract. Our **job generator** reads
`docs/seed/*.json` and emits jobs:
- For each **competitor** × each **source** → a `news` job (seed_url = that source's search URL for the entity's aliases).
- For each **tender source** × **keyword set** → a `tender` job.
- For each **competitor IR page** → a `profile` job (partnerships, geo).
- For each **product** needing specs → a `spec` job.

So the seed flows: `watchlist_entities/products/tenders/tech_domains` → job generator → crawl jobs → you.
We hand you fully-formed jobs; you never read the seed strategy, only execute the job's URL+keywords+budget.

---

## 7A. Re-crawl, change detection & dedup — YOUR job vs L2's job

Continuous crawling means the same URLs and the same stories recur every day. Split the work cleanly so
you don't reprocess unchanged data and don't try to do L2's clustering:

**You DO (against your OWN crawl history — your Postgres `crawl_pages` remembers the last run):**
- **Conditional re-fetch.** Send `If-None-Match` / `If-Modified-Since`. A `304 Not Modified` → skip, don't even re-download. (Saves crawl cost on daily sweeps.)
- **Self-dedup by content_hash.** If a URL's `content_hash` equals what you stored last run → the page didn't change → **do not re-emit it.** This is exactly what stops "daily crawl, no change" from flooding L2.
- **Emit only when** a URL is new, OR its `content_hash` changed since your last crawl.

**You do NOT (that's L2 — it has the full corpus + embeddings):**
- **Don't merge different URLs that cover the same event** (5 outlets reporting one K9 order). Emit each with its own `content_hash` + `canonical_url`; L2 clusters them into one signal and elects a primary (service S-08).
- **Don't decide which duplicate is "the important one."**

> **The rule in one line:** the crawler dedups against *itself* (same URL, unchanged → idempotent re-crawl);
> L2 dedups across *sources* (different URLs, same event → semantic merge). Two problems, two layers.

**Pinpoint precision at volume:** you never search the open web blindly. Every job is pre-scoped to
**one source + one entity + its keywords**, and the keyword relevance gate (pipeline stage 3) drops any
page that matches no keyword. Precision comes from *scoping the job*, not from sifting a giant pile
afterward. Large volume is handled by many small scoped jobs + the gate, not by one big crawl.

---

## 8. TESTING PHASE — scope for the first build (NOT production)

> **Production cadence (P0/P1 every-6h schedules) does NOT apply during testing.** In testing we are only
> proving the engine works end-to-end on a fixed, tiny job set, run **once, manually.** No scheduling,
> no priorities, no full-universe sweep.

### 8.1 Testing job set (run once)
A fixed ~12-job batch, hand-made, to exercise harvest + gate + asset capture:

| # | job_type | target | proves |
|---|---|---|---|
| 1–3 | `news` | L&T, Adani, KNDS (one source each) | keyword gate pass + resolved-entity tags (`stream`/`detected_competitor`) |
| 4–5 | `tender` | MoD India + SAM.gov (one query each) | tender keyword gate + PDF extraction + screenshot |
| 6–7 | `profile` | NIBE, Solar IR/press pages | keyword gate pass + detected countries/products tags |
| 8 | `spec` | one KNDS product page (CAESAR) | table + product image capture |
| 9–10 | `news` (market) | "Armenia artillery tender", "India defence budget" | market-stream gate pass |
| 11–12 | `news` (tech) | "ramjet 155mm", "loitering munition" | technology-stream gate pass |

### 8.2 Testing exit criteria (all must pass)
1. Every job runs within its `max_pages`/`max_depth` budget without crashing.
2. Each produces ≥1 valid `document` with non-empty `main_text` + canonical `url` + `content_hash`.
3. Every job's kept page(s) are **accepted** by the stub Ingest endpoint (`POST /ingest/v1/page`).
4. PDF extraction works on at least one real tender (the RFP text appears in `attachments[].extracted_text`).
5. Screenshot capture works (one full-page PNG per document, stored, path returned).
6. Entity resolution links ≥80% of obvious mentions to the right seed id.
7. Non-English handling: one non-English source returns both `main_text` and `main_text_en`.

Hit all 7 → the crawler is "pipeline-proven." **Only then** do we add scheduling/cadence/full-universe
sweeps (a later, production phase — out of scope for the first build).

---

## 9. Acceptance rules (the Ingest API enforces these)

A page bundle is accepted only if:
1. It has non-empty `main_text`, a non-empty canonical `url`, and a real `content_hash`.
2. `published_at`, if present, parses as ISO (or is null with a `date_precision` flag).
Rejected → `422 { failing_rule }`. Track accept-rate per source; L2 uses it to tune source tiers.

(The gate — §3 — already enforces keyword relevance before a page is ever sent, so there's no
separate relevance re-check at ingest time; that would be redundant. Entity resolution is informational,
not a keep condition, so ingest never re-checks it either.)

## 10. Hard "do nots"
- ❌ Don't classify pages into typed business records (tender/partnership/geo_footprint/innovation/
  company_event/competitive_signal field extraction). That's Layer 2, operating on the raw bundle.
- ❌ Don't judge threat/score/relevance-to-strategy or write "so what" analysis. That's Layer 2.
- ❌ Don't merge different URLs about the same event — send each with its `content_hash`; **L2 clusters** them (§7A). But DO skip re-sending a URL unchanged since *your* last crawl (self-dedup, §7A), and DO skip a same-run duplicate (two URLs, byte-identical rendered content).
- ❌ Don't fabricate. Unknown = `null`. Never guess a value, date, or quantity.
- ❌ Don't wander off the seed/registry (except following a link out from a registry page, within `max_depth`).
- ❌ Don't download large media. Record video/audio links as metadata only.
- ❌ Don't apply production cadence in the testing phase — run the fixed §8 set once.

---

## 11. Seed files (already populated — your real targets)

| File | Contents |
|---|---|
| `docs/seed/watchlist_entities.json` | 32 tracked competitors (KSSL anchor + globals + Indian rivals) with aliases, HQ, priority; partner nodes |
| `docs/seed/watchlist_products.json` | 27 KSSL products + tracked competitors' products, by category |
| `docs/seed/watchlist_tech_domains.json` | 8 tech domains + keyword sets |
| `docs/seed/watchlist_tenders.json` | tender keywords + 25 target countries + portal sources |
| `docs/seed/source_registry.json` | approved sources with trust tiers + global capture defaults |

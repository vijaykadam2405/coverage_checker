# Crawl error handling

Every failure the crawler can hit is **named** (a typed reason) and **deliberately handled**
(a policy). Reasons show up per-job in `summary.errors_by_reason`; the dashboard renders an
`errors N` chip whose tooltip is the breakdown. Traps dropped at the frontier show as `traps N`
(`summary.trap_skipped`).

Ethics ceiling: the strongest "disguise" is a **browser User-Agent retry** — no proxy/IP
rotation, no CAPTCHA solving. Broken TLS is **never** bypassed. `.gov`/`.mil` are crawled
*slower*, never more aggressively.

## Per-status policy

| Status | Action | What happens |
|---|---|---|
| 400 | drop | bad request (our link) — no retry |
| 401 | drop | auth wall — no credentials to offer |
| 403 | disguise | ONE retry with a real-browser UA; still 403 → drop. **Careful hosts skip disguise.** |
| 404 | gone | recorded; skipped on future runs after **2** consecutive 404s |
| 410 | gone | recorded; skipped from the very next run (server said "gone forever") |
| 429 | cooldown | honor `Retry-After` (capped 300s); cool the whole host + one in-run retry |
| 500 / 502 / 504 | retry_later | one in-run retry (no host cooldown — likely page-specific) |
| 503 | cooldown | same as 429 |
| other 4xx | drop | |
| other 5xx | retry_later | |

## Network / structural reasons

| Reason | Cause | Action |
|---|---|---|
| `dns` | name won't resolve | drop + feeds the per-host **circuit breaker** |
| `conn_refused` | host refused / reset the connection | drop + circuit breaker |
| `ssl` | cert/handshake failure | drop + circuit breaker — **never** `verify=False` |
| `timeout` | connect/read/nav timed out | one in-run retry |
| `render_crash` | Playwright tab/nav/content crash | one in-run retry (tab recycled) |
| `too_large` | body over the size cap | drop before it's read whole into RAM |
| `parse_error` | malformed HTML crashed the parser | drop (lxml→html.parser fallback tries first) |
| `blocked_by_robots` | robots.txt disallow | drop |
| `trap` | loop/calendar/facet/oversize URL | dropped at the frontier (see `trap_skipped`) |
| `skipped_host_down` | host tripped the circuit breaker this run | its remaining URLs skipped |
| `skipped_gone` | known-404(×2)/410 from a prior run | skipped before budget is spent |

**Circuit breaker:** after `CRAWLER_HOST_HARD_FAILS` (default 3) consecutive DNS/refused/SSL
failures, a host is marked dead for the rest of the run — its queued URLs are skipped, not
hammered. A single success resets the count.

## Traps

- **URL shape** (stateless): dropped if longer than `CRAWLER_MAX_URL_LEN` (2000), more than 12
  path segments, or any path segment repeats ≥3× (`/a/b/a/b/a/b` loops).
- **Query explosion** (stateful, per host+path): after `CRAWLER_MAX_QUERY_VARIANTS` (20)
  distinct query-only variants of the same path, the rest are dropped — collapses calendar
  (`?date=…`) and facet traps without touching legitimate `?id=` pages.

## Size safety

- httpx (pages + assets): streamed, aborted on `Content-Length > cap` **or** a streamed overrun.
  Caps: `CRAWLER_MAX_HTML_BYTES` (20MB), `CRAWLER_MAX_ASSET_BYTES` (50MB).
- Render path: giant binary extensions (zip/exe/iso/mp4/…) are aborted at the network layer;
  a runaway DOM over `CRAWLER_MAX_HTML_BYTES` is dropped after `content()`.

## Careful mode (`.gov` / `.mil`)

Hosts whose domain contains a `CRAWLER_CAREFUL_HOSTS` segment (default `.gov,.mil`) get:
concurrency **1**, delay ≥ `CRAWLER_CAREFUL_DELAY_S` (5s), and **no UA disguise**. Force it for
any job with `"careful": true`.

## Hunt modes

Presets over existing knobs (they fill only values you leave unset):

```jsonc
// Exhaustive — deep dig on a meaty defense site
{ "job_id":"j1", "job_type":"news", "seed_urls":["https://…"],
  "keywords":["artillery","contract"], "render_js":true, "hunt_mode":"exhaustive" }
//   → max_pages 750, max_depth 5

// Focused — probe first, then crawl only the relevant corner
//   1) POST /v1/suggest-job {"url":"https://…","hunt_mode":"focused","render_js":true}
//   2) take the returned .job and POST it to /v1/crawl or /v1/crawl/batch
//   → max_pages 60, max_depth 2, skip_irrelevant_seed_links, link_relevance_keywords=keywords
```

## Config (env)

| Var | Default | Meaning |
|---|---|---|
| `CRAWLER_MAX_HTML_BYTES` | 20971520 | page/DOM size cap |
| `CRAWLER_MAX_ASSET_BYTES` | 52428800 | image/PDF size cap |
| `CRAWLER_MAX_QUERY_VARIANTS` | 20 | query-explosion cap per host+path |
| `CRAWLER_MAX_URL_LEN` | 2000 | max URL length before it's a trap |
| `CRAWLER_HOST_HARD_FAILS` | 3 | consecutive hard fails → host dead this run |
| `CRAWLER_COOLDOWN_BASE_S` | 30 | default host cooldown when no Retry-After |
| `CRAWLER_COOLDOWN_CAP_S` | 300 | max host cooldown |
| `CRAWLER_INRUN_RETRIES` | 1 | in-run retries for transient failures |
| `CRAWLER_CAREFUL_HOSTS` | `.gov,.mil` | domain segments crawled "quiet quiet" |
| `CRAWLER_CAREFUL_DELAY_S` | 5 | min per-request delay for careful hosts |

Per-job: `hunt_mode` (`exhaustive`/`focused`), `careful` (bool).

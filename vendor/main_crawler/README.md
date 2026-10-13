# Mallory Crawler

A defence competitive-intelligence web crawler built on a **four-tier fetch architecture**, with two
operator dashboards. Everything here is one deployable stack: `docker compose up -d --build`.

See **[RUNNING.md](RUNNING.md)** for step-by-step run instructions.

## The four tiers

Each page is fetched by the cheapest tier that can actually serve it; the crawler escalates only
when a tier is refused, because each step up costs roughly 50× the one below it.

| Tier | Transport | When it is used |
|---|---|---|
| **C1 — live** | `httpx` + curl_cffi Chrome TLS/JA3 impersonation | Default. Fast, cheap, no browser. |
| **C2 — Wayback** | Internet Archive | The origin is dead, blocked, or gone (404/410). |
| **C3 — CamoFox** | Camoufox stealth browser (Firefox) | The origin refuses everything else: JS-only pages, bot walls, captchas. |
| **C4 — Host solver** | Browser on the operator's GPU workstation, reached over a reverse SSH tunnel + socat relay | The one thing C3 can't clear: an *interactive* Cloudflare / Incapsula wall. The last rung. |

A per-host circuit breaker, adaptive per-host concurrency, and a render-permit pool decide which
tier runs and how hard each host is pushed. A page that no tier can serve is recorded as a failure
rather than silently dropped.

**C4 is opt-in and off by default** — it needs a real GPU the crawl VM does not have, so it runs on
the operator's workstation and is only reached when `CRAWLER_HOST_SOLVER_URL` is set (see
`docker-compose.solver.yml` + `scripts/c4_up.ps1`). Once a C4 solve clears one page of an
interactive-Cloudflare host, that whole host is marked so its other pages take the C4 rung directly.

## Services

| Service | Port | What it is |
|---|---|---|
| **orchestrator** | 8090 | Sources, schedule, category switches, run history — and **Verify data** / **Push to data centre** |
| **crawler-api** | 8099 | Submit crawl jobs, live batch metrics, crawl dashboard |
| **ingest** | 9090 | Document landing → Postgres + MinIO, plus the **Live Data** dashboard at `/live` |
| postgres | 5433 | The `documents` table (the structured dump) |
| minio | 9000 / 9001 | Blob store `mallory-raw` (PDFs and attachments) + console |
| camofox | — | C3 stealth browser. Profile-gated: `--profile camofox` |
| camofox-paid | — | C3 through a paid residential proxy. Profile-gated: `--profile paid` |
| hostsolver-relay | 9379 (internal) | C4 relay — socat hop to the workstation solver. Opt-in: `-f docker-compose.solver.yml` |

## Dashboards

- **`:8090`** — the operator panel: what to crawl, when, and whether a category is running at all.
  It is also where a machine's data is sent: **Verify data** compares this machine against the data
  centre (including checking that rows marked delivered are *still there*), then **Push to data
  centre** ships them. Nothing is sent on a timer or at startup.
- **`:8099`** — the crawler: submit a batch, watch pages/sec, pool utilisation, per-host behaviour.
- **`:9090/live`** — **Live Data**: what is *actually* in the store right now. Every number resolves
  to one live source, labelled on the page — a Postgres query, a MinIO listing, or the running
  batch from crawler-api. No in-process counters (they reset on restart and read as an empty store),
  no job-run history that can drift from the data. It carries a built-in reconciliation check:
  Σ pages-per-source must equal total documents, shown green or red.
- **`:9090`** — the original ingest dashboard: browse captured document bundles.

## Layout

```
crawler/crawler/        the engine: async tab pool, the four fetch tiers, keyword gate,
                        dedup, extraction, per-host politeness and circuit breaking
crawler/crawler_api/    :8099 job API + crawl dashboard
crawler/ingest_api/     :9090 landing, Postgres writer, Live Data dashboard
crawler/edgecases/      the robustness benchmark: 462-URL corpus, harness, and the xlsx report
crawler/tests/          self-checks — plain `python tests/test_x.py`, no pytest
orchestrator/           :8090 control plane + operator dashboard
vendor/camofox-browser/ build context for the C3 stealth browser
```

## Running it at the data centre

The stack runs ON the centre and writes straight into the `mallory-data` Postgres and MinIO that
already hold the corpus — no local landing, no push step, no tunnel for the data itself. The
dashboard binds to loopback there and is reached over an SSH tunnel.
**[DATACENTRE.md](DATACENTRE.md)** is the setup guide.

There used to be a separate fleet dashboard for watching several crawling machines report in. With
one machine, sitting on the centre, it showed nothing the crawler's own dashboard does not — so it
is gone. The heartbeat writer survives behind `FLEET_HEARTBEAT=0` if a second machine is ever added.

## Configuration

Every knob is an environment variable read by `docker-compose.yml`; see `.env.example`. The defaults
run entirely against the bundled Postgres + MinIO, so an absent `.env` is a working local stack.

**Before any real deployment**, override the bundled credentials — the local defaults
(`MINIO_ROOT_PASSWORD`, `POSTGRES_PASSWORD`, `CAMOFOX_API_KEY`) are placeholders, not secrets.

Most compose comments record a *measured* reason for a value (why the item watchdog is 240s, why one
host may hold only 8 render permits). Read them before changing a number.

## Tests

```bash
cd crawler && PYTHONPATH=. python tests/test_units.py     # …and the other tests/test_*.py
```

Each suite is a standalone script with asserts — no pytest, no fixtures.

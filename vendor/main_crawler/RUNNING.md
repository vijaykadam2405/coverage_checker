# Running the crawler

Step by step, from a clean checkout to a finished crawl you can verify.

## 0. Prerequisites

- Docker + Docker Compose
- ~8 GB free RAM for the default browser pool (12 browsers × 16 tabs). Lower it in `.env` if needed:
  `CRAWLER_BROWSERS=4`, `CRAWLER_TABS_PER_BROWSER=8`.
- Disk: Postgres stores full page HTML. Plan ~15 GB per 70k pages.

## 1. Configure (optional for local)

```bash
cp .env.example .env
```

Every value has a working local default, so an empty `.env` runs fine against the bundled Postgres
and MinIO. **For anything beyond local, change the credentials** — the defaults are placeholders:

```
MINIO_ROOT_USER=...        MINIO_ROOT_PASSWORD=...
POSTGRES_USER=...          POSTGRES_PASSWORD=...
CAMOFOX_API_KEY=...
```

## 2. Start the stack

```bash
docker compose up -d --build
```

Starts postgres, minio, minio-init, crawler-api (:8099), ingest (:9090) and the operator dashboard
(:8090). First build takes a few minutes (Playwright browsers). Check it came up:

```bash
curl -s localhost:8099/v1/metrics | head -c 200     # crawler alive
curl -s localhost:9090/health                       # ingest alive
curl -s localhost:8090/health                       # dashboard alive
```

Optional — the C3 stealth tier (needed only for sites that refuse plain HTTP):

```bash
docker compose --profile camofox up -d camofox
```

## 3. Run a crawl

Submit a batch to the crawler. `keyword: false` keeps every page; set it `true` to keep only pages
matching the built-in defence keyword corpus.

```bash
curl -X POST localhost:8099/v1/crawl/batch \
  -H 'Content-Type: application/json' \
  -d '{
    "background": true,
    "forward_to_ingest": true,
    "jobs": [
      {"job_id": "example_1", "job_type": "profile",
       "seed_urls": ["https://www.stimson.org/"],
       "keyword": false, "render_js": false,
       "max_pages": 400, "max_depth": 3,
       "same_domain_only": true, "capture": ["html","text","pdf"]}
    ]
  }'
```

Response: `{"started":true,"background":true,"total":1}`

**Job fields that matter**

| Field | Meaning |
|---|---|
| `max_pages` | Hard cap per job. `0` = unlimited. |
| `max_depth` | Link depth from the seed. `-1` = unlimited. |
| `render_js` | `false` = httpx-first, escalate only if needed (fast). `true` = always render. |
| `keyword` | `true` applies the global defence keyword gate; `false` keeps everything. |
| `same_domain_only` | Keeps the crawl on the seed's registered domain. |
| `incremental` | `true` skips pages whose content hash is unchanged since the last run. |

## 4. Watch it run

- **`http://localhost:8090`** — the operator dashboard: sources, schedule, category switches, run
  history, and the **Verify data** / **Push to data centre** buttons (see §8).
- **`http://localhost:8099`** — pages/sec, pool utilisation, per-host behaviour, live batch.
- **`http://localhost:9090/live`** — the Live Data dashboard: what is actually in Postgres and
  MinIO right now, per source, refreshing every 3 s.

Or from the shell:

```bash
curl -s localhost:9090/api/live/summary | python -m json.tool
```

## 5. Stop a crawl

```bash
curl -X POST localhost:8099/v1/crawl/stop
```

Cooperative — it finishes the pages in flight rather than abandoning half-written documents. Wait
for `batch.running` to go `false` before restarting or rebuilding.

> **Do not rebuild `crawler-api` while a batch is running.** The frontier is in memory; a restart
> discards it. Always stop first and confirm `running: false`.

## 6. Verify what you captured

The dashboard should agree exactly with the database. Check it directly:

```bash
# What the dashboard reports
curl -s localhost:9090/api/live/summary | python -c "import sys,json;print(json.load(sys.stdin)['postgres'])"

# What Postgres actually holds
docker compose exec postgres psql -U mallory -d mallory -c "select count(*) from documents;"

# Per site
docker compose exec postgres psql -U mallory -d mallory \
  -c "select source_id, count(*) from documents group by 1 order by 2 desc limit 10;"
```

The two must match. If you compare them *while a crawl is running* they can differ by a page or two
— that is read skew from a moving target, not a fault. Stop the crawl and they agree exactly.

Read the stored pages one row per URL through the `documents_current` view (it keeps the best
capture per URL, not merely the newest — a re-crawl truncated by a bot wall must not overwrite a
good earlier copy):

```sql
SELECT url, title, length(main_text) FROM documents_current ORDER BY fetched_at DESC LIMIT 20;
```

## 7. Tests

```bash
cd crawler
PYTHONPATH=. python tests/test_units.py        # and the other tests/test_*.py
```

Each file is a standalone script of asserts — no pytest, no fixtures.

## 8. Running it at the data centre

Full guide: **[DATACENTRE.md](DATACENTRE.md)**.

The stack runs ON the centre now, so the multi-machine machinery this section used to describe —
local landing, push to the centre, tunnelled Postgres/MinIO, a fleet dashboard showing every
reporting node — no longer has a job. `ingest` writes the `documents` table and the MinIO bucket
directly, which means there is no backlog to watch and nothing to deliver: a page is either stored
in the corpus or it failed, and the dashboard says which.

```bash
# on the server
cd ~/mallory-crawler
docker compose -f docker-compose.yml -f docker-compose.datacentre.yml up -d
```

The overlay is what makes it safe on a shared box: the bundled Postgres and MinIO stay down (the
corpus lives in `mallory-data` and a second empty database would look perfectly healthy while the
real one sat untouched), every port binds `127.0.0.1`, and the four services are capped at 30 CPUs
and 496 GB between them because the box has co-tenants who were there first.

**Reaching the dashboard.** Nothing is exposed to the internet — the provider drops inbound traffic
to everything except SSH, and the dashboard has a Flush button on it. Forward it instead:

```bash
ssh -i ~/.ssh/<deploy-key> -p <ssh-port> -N -L 8090:127.0.0.1:8091 <user>@<host>
# then open http://localhost:8090/
```

Server-side it listens on **8091**, because 8090 on that host belongs to a co-tenant that has had it
for weeks. The tunnel maps it back, so the address you type does not change.

**Fleet telemetry is off** (`FLEET_HEARTBEAT=0`). The writer is still in the image; set it to 1 and
point `FLEET_DSN` at the centre if a second crawling machine is ever added.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Dashboard shows 0 documents | `DATABASE_URL` unset or Postgres unreachable — the ingest logs print `[pgstore] connect failed`. |
| Crawl runs but nothing is stored | `forward_to_ingest` was omitted from the batch. |
| Throughput collapses mid-crawl | Usually one slow host holding the render pool. `:8099` shows per-host concurrency; `CRAWLER_HOST_RENDER_MAX` caps one host's share. |
| Many `render_empty` / timeouts on one site | That origin is slow or refusing. Lower that host's concurrency — for slow origins, parallelism is strictly negative. |
| A site returns 403 to everything | Try the C3 tier: `docker compose --profile camofox up -d camofox`. |

# Mallory crawler — daily health check

One command that exercises the **live** crawler stack end to end and tells you, in a
single per-area report, whether the crawler is healthy — and if not, **which subsystem
broke**. It runs once a day on the data-centre box.

This is **not** the unit-test suite (`crawler/tests/`, `orchestrator/tests/`). Those
prove the code is correct in isolation. This proves the *running system* is doing its
job right now: the tiers actually fetch, sites are being crawled, the store reconciles,
the dashboards serve real data, and the control plane is in a sane state.

## Why it exists

When something goes wrong ("the dashboard is empty", "nothing's crawling"), the first
question is always *where*. This suite answers that up front: each check is small and
names the exact thing it verified, so a red line points straight at the fault instead
of sending you into the logs.

## What it checks (by area)

| Area | Proves |
|---|---|
| **services** | Every process is up and reachable: ingest, orchestrator, crawler-api, Postgres, MinIO, CamoFox. The critical liveness floor. |
| **tiers** | Each of the four fetch tiers works and is used. **C1** (httpx/curl_cffi) — the cheap default; **C2** (Wayback) — for dead/blocked origins; **C3** (CamoFox) — for JS/bot-walls/captchas; **C4** (host solver) — for interactive Cloudflare/Incapsula walls C3 can't clear. Active canary probe + passive "served recently" evidence. |
| **coverage** | Sites are being crawled *properly*: pages landing recently, crawl goes beyond the seed, text/html captured (not empty shells), and the failure classes are caught — one-page sites, zero-page sources, stalled freshness. |
| **delta** | The push/verify machinery and the store's own accounting are correct: the local↔centre delta computes coherently, no "delivered-but-missing" rows, and Σ pages-per-source == total documents (the dashboard's reconciliation invariant). |
| **dashboards** | Every dashboard-backing endpoint serves real, self-consistent data within a sane time — including the slow ones that used to time out. |
| **logic** | The control plane is behaving: maintenance is off, the category buckets are right, sharding is spread across the fleet, the daily lane and scheduler are sane, all instances report. Optionally folds in the unit suites (guarded — see below). |

## Running it

On the box, as a one-shot overlay on the live stack (shares its network + `.env`):

```bash
docker compose -f docker-compose.yml -f docker-compose.healthcheck.yml run --rm healthcheck
```

Or the wrapper (adds timestamped logging + a `-latest` symlink + non-zero exit on failure):

```bash
./healthcheck/run_daily.sh
```

Run a single area while debugging:

```bash
docker compose -f docker-compose.yml -f docker-compose.healthcheck.yml \
  run --rm healthcheck python -m healthcheck.run tiers coverage
```

**Exit code:** `0` = healthy, `1` = at least one check FAILed. `SKIP` is not a failure
(it means a feature is off or a dependency isn't wired here).

## Scheduling it daily

**systemd** (recommended — gives journald logs + `--failed` visibility):

```bash
sudo cp healthcheck/deploy/mallory-healthcheck.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mallory-healthcheck.timer
systemctl list-timers mallory-healthcheck.timer     # confirm next run
```

**cron** (alternative):

```cron
0 6 * * *  /opt/mallory-crawler/healthcheck/run_daily.sh >> /var/log/mallory/healthcheck-cron.log 2>&1
```

**docker sidecar** (when the box has no cron / user-systemd / sudo — this is the data
centre's case): a tiny always-on container that sleeps to 06:00 UTC and runs the check
in-process. It forks via runc only once (to start), so the daily runs never hit the
fork pressure a daily `docker compose run` would.

```bash
docker compose -f docker-compose.yml -f docker-compose.healthcheck.yml up -d healthcheck-cron
docker logs -f mallory-crawler-healthcheck-cron-1     # watch it
```

Change the hour with `HC_CRON_UTC_HOUR` (default 6). Reports land in `healthcheck/logs/`.

## Reading the report

```
[tiers]
  PASS c1_live_active                    0.4s  example.com 200, 1256 bytes via httpx/impersonate
  PASS c1_recent_served                  0.1s  18,432 docs via c1_live in last 48h
  SKIP c4_host_solver_active             0.0s  C4 disabled: CRAWLER_HOST_SOLVER_URL unset
...
UNHEALTHY: 41 passed, 1 failed, 6 skipped in 73.2s
```

A JSON copy is written to `HC_JSON_OUT` (the wrapper timestamps it under
`healthcheck/logs/`) for trending or alert hooks.

## Configuration (env)

All optional — the defaults target the compose network. `HC_*` wins; the plain names
(`DATABASE_URL`, `MINIO_*`, `CAMOFOX_URL`) fall back to what the stack already sets.

| Var | Default | Meaning |
|---|---|---|
| `HC_INGEST_BASE` | `http://ingest:9090` | ingest base URL |
| `HC_ORCH_BASE` | `http://orchestrator:8090` | orchestrator base URL |
| `HC_CRAWLER_API_BASE` | `http://crawler-api:8099` | crawler-api base URL |
| `HC_CAMOFOX_URL` / `CAMOFOX_URL` | `http://camofox:9377` | CamoFox pool (first entry probed) |
| `DATABASE_URL` | compose default | the `documents` Postgres (read-only) |
| `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` / `MINIO_BUCKET` | compose defaults | blob store |
| `HC_RECENT_HOURS` | `48` | window for "recent"/freshness checks |
| `HC_JSON_OUT` | — | write the JSON report here |
| `CRAWLER_HOST_SOLVER_URL` | — | set → C4 checks run; unset → they SKIP |
| `HC_C4_LIVE_SOLVE` | `0` | `1` → do a real (expensive) C4 solve in the probe |
| `HC_RUN_UNIT` + `HC_TEST_DATABASE_URL` | — | opt into the unit suites against a **separate** DB (see below) |

## Two safety rules baked in

1. **Never detoast the corpus.** `hc.pg()` refuses any query that selects `main_text`
   or `html` — decompressing those TOAST columns over the whole corpus is a known
   outage. Checks use `text_len` / `html_len`. (Single-row detoast is available via
   `allow_body=True`, reserved for `WHERE document_id = %s LIMIT 1`.)
2. **The unit suites share the production DB**, so they are **not** run by default.
   The `logic` area will only run them when you set both `HC_RUN_UNIT=1` **and**
   `HC_TEST_DATABASE_URL` to a database that is *not* the production one.

## Adding a check

Drop a function in the right `checks/<area>.py`:

```python
from .. import hc

@hc.check(area="coverage", name="pdfs_captured_recently")
def _pdfs():
    n = hc.pg_scalar("SELECT count(*) FROM documents WHERE pdfs > 0 "
                     "AND ingested_at > now() - interval '%s hours'" % int(hc.RECENT_HOURS))
    hc.need(n and n > 0, "no PDFs captured recently")
    return f"{n} docs with PDFs in last {hc.RECENT_HOURS:.0f}h"
```

Return a short detail string on success, `raise hc.Skip("reason")` to skip, or let any
assertion/`hc.need` failure mark it FAILed. Keep it cheap and read-only.

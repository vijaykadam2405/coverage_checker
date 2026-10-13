# Mallory — Layer 1.5 (Acquisition Orchestrator)

Owns the **seed → job matrix**, the **Source Catalog** (source_id + trust tier, fully automatic),
the **scheduler** (in-process cron over per-job cadence), the **coverage ledger** (no-miss guarantee),
and the **control-panel dashboard**. Reads the static seed, generates crawl jobs, and dispatches them
to the Layer 1 crawler API — which persists crawled docs to its own Postgres + MinIO (jobs carry
`forward_to_ingest`). Separate deployable (built for a high-end crawl VPS).

## Design invariants
- **Only human inputs: `{url, frequency, category}`.** Everything else is automatic — no review queue.
- **Tier ≠ relevance.** `frequency` = how often we crawl (human); `tier` = confidence weight for
  downstream ranking (auto). Tier never gates crawling/ingestion and never hides a signal — a new
  tier-3 blog can rank #1 on merit.
- **Fail to low trust.** Unknown domains → tier 3, `source_known=false`, never tier 1.

## Quickstart
```bash
pip install -e .            # or reuse a venv with fastapi+sqlalchemy+httpx+uvicorn
python -m mallory_orchestrator.scripts.init_db
python -m mallory_orchestrator.scripts.seed_sources     # seed catalog from registry + tender portals
uvicorn mallory_orchestrator.main:app --port 8090 --app-dir src   # control panel → http://localhost:8090
```
Run with a **single worker** — the scheduler runs in-process, so two workers = double dispatch.
The lifespan auto-creates the scheduler/keyword/settings tables, so `init_db` is only needed the
first time (to create the source catalog before `seed_sources`). Requires the crawler API (:8099)
running to dispatch. The master pipeline switch defaults **OFF** — arm it in the Control Panel to
let scheduled jobs fire.

## End-to-end offline test (orchestrator → crawler)
```bash
rm -f ../cralwer/data/crawl_history.sqlite          # let fixtures re-emit
python -m mallory_orchestrator.scripts.run_once --test
```
Dispatches the 7 fixture jobs to the crawler; each JobRun is recorded and coverage marked.

## Components
```
src/mallory_orchestrator/
  sources.py     source_id + tier classifier (eTLD+1, taxonomy, fail-to-tier-3) — automatic
  seed.py        loads the watchlist (entities/products/keywords/countries)
  jobgen.py      coverage-complete job matrix (all sources × all competitors × countries), stamps source_*
  orchestrate.py control loop: build jobs → dispatch → record run → mark coverage
  scheduler.py   in-process asyncio cron: next-run/retry/backoff, master-switch gate, busy-gate
  crawler_client.py  POST /v1/crawl (dispatch) + /v1/metrics · /v1/crawl/stop · /v1/batch/status
  models.py      Source catalog · CoverageCell · JobRun · ScheduledJob · ScheduleLog · Keyword · Setting
  api.py/main.py control-plane API + single-page control panel (static/index.html)
```

## Control panel (the human control surface)
`http://localhost:8090` — a 7-page dashboard: Dashboard, Sources (add/import/delete),
Job Generator (`/api/jobs/generate`), Job Schedule (cadence + retry + start time), Control Panel
(master switch, per-job enable/pause, live crawler metrics + Stop), Logs (schedule + execution),
and Keywords (job-gen keyword bank). "Run due jobs" dispatches the production matrix on demand;
"Run offline test batch" exercises the chain with fixtures.

## Taxonomy → default tier
gov_primary / manufacturer_ir / defence_org / tender_portal = **1** · trade_press / think_tank /
business_press = **2** · aggregator / blog_forum_social / unknown = **3**. Curated registry entries
(e.g. Janes = 1) override the category default.

## Still to wire
- **S-28 dynamic tiering** off the crawler's `/stats` + dedup (auto-demote freely; auto-promote to ≤2).
- Postgres for the control store at internet-scale (SQLAlchemy URL swap).
- Scheduler graduation to APScheduler / an external worker if multi-worker or cron expressions are needed.

"""FastAPI app — admin API, the single-page control panel, and the crawl scheduler."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import __version__, auth, autopilot, daily, refill, scheduler, tiering
from .api import router
from .config import get_settings
from .db import Base, SessionLocal, engine
from .models import Setting

# THE APPLICATION LOG WAS ENTIRELY INVISIBLE UNTIL THIS EXISTED.
#
# uvicorn configures its OWN loggers and leaves the root logger alone, so a module logger created
# with logging.getLogger("mallory.refill") had no handler and fell through to logging.lastResort —
# which emits WARNING and above and drops everything below it. Every log.info in the scheduler, the
# refill watcher, the autopilot and the mailer went nowhere. `docker logs` showed four uvicorn
# startup lines and nothing else, for the life of the deployment.
#
# That is not a cosmetic gap. The auto-refill watcher sat at capacity for days resuming nothing,
# and the one signal that would have said so — its own log line — was being discarded before it
# reached stdout. Diagnosing it needed a live call to refill_once() from a shell.
#
# basicConfig is a no-op if handlers already exist, so this cannot fight a caller that configured
# logging first (tests, a future structured-logging setup).
logging.basicConfig(
    level=os.environ.get("ORCH_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
# httpx logs a line per REQUEST at INFO. Every dashboard poll and every refill tick fans out across
# twelve instances, so turning the root logger on immediately buried the scheduler and refill lines
# it was turned on for — 8 of the first 10 lines were "GET .../v1/batch/status 200 OK". Quieting
# the client is what makes the application's own log readable; ORCH_LOG_LEVEL=DEBUG brings it back.
if os.environ.get("ORCH_LOG_LEVEL", "INFO").upper() != "DEBUG":
    for _noisy in ("httpx", "httpcore"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Create any new tables (scheduled_jobs, schedule_logs, keywords, app_settings) — idempotent,
    # so no separate init_db run is needed for an existing deployment.
    Base.metadata.create_all(engine)
    # Creates/updates the single dashboard account from the environment. Raises if DASH_AUTH=1 but
    # the credentials are missing — refusing to boot beats booting an unprotected console.
    auth.ensure_admin()
    # Master pipeline defaults OFF: nothing auto-crawls until the operator arms it.
    with SessionLocal() as db:
        if db.get(Setting, "pipeline_enabled") is None:
            db.add(Setting(key="pipeline_enabled", value="false"))
            db.commit()
        # Backfill an adaptive-cadence policy for every catalog source. Idempotent, and an
        # already-crawled source starts in `steady`, so an existing deployment never redoes a census.
        try:
            tiering.ensure_site_schedules(db)
        except Exception:  # noqa: BLE001 — never block startup on the backfill
            pass
        # The daily lane's URL list lives in a file the operator edits (seed_data/daily_urls.txt),
        # so the table is re-synced from it on every boot. Idempotent; a URL removed from the file
        # is disabled, never deleted, so its run history survives an edit.
        try:
            daily.sync_from_file(db)
        except Exception:  # noqa: BLE001 — never block startup on the sync
            pass
        # A campaign row still 'running' has no thread behind it after a restart: close it and
        # return to normal mode rather than leave a ghost holding the lanes (campaigns.recover).
        try:
            from . import campaigns
            ghosts = campaigns.recover(db)
            if ghosts:
                logging.getLogger("mallory.campaigns").warning(
                    "campaign(s) %s were running when the process died — closed; re-run from the "
                    "saved plan", ghosts)
        except Exception:  # noqa: BLE001 — never block startup
            pass
        # Say what the control plane is at boot. A fleet that comes up and dispatches nothing is
        # behaving if it is stopped, in campaign mode, or has every timer toggled off — and the
        # first place anyone looks is this log.
        try:
            from . import policy
            st = policy.state(db)
            logging.getLogger("mallory.policy").info(
                "control plane: %s, %s mode, lanes open %s, toggles on %s",
                st["run"], st["mode"], sorted(st["lanes"]["open"]) or "none",
                sorted(k for k, v in st["auto"].items() if v["on"]) or "none")
        except Exception:  # noqa: BLE001 — a log line must never block startup
            pass
    stop = asyncio.Event()
    app.state.scheduler_stop = stop
    app.state.scheduler_task = asyncio.create_task(scheduler.loop(stop))
    # Keeps an idle crawler instance working. The fleet splits a batch by domain and domains are
    # wildly unequal, so instances finish at very different times — measured live, two of seven sat
    # idle holding ~12,000 pending URLs because their share had ended while work remained.
    app.state.refill_task = asyncio.create_task(refill.loop(stop))
    # Presses the health buttons on a timer. Every action it takes already existed as a button that
    # only fired when somebody happened to look, and the faults they fix are quiet by design.
    app.state.autopilot_task = asyncio.create_task(autopilot.loop(stop))
    # The daily lane. Its own loop, not a branch inside scheduler.loop(), because both dispatch
    # calls BLOCK for the length of a crawl — sharing one loop would put the daily news sites right
    # back behind the six-hour catalogue crawl that reserving instances exists to escape.
    # No-ops immediately when CRAWLER_DAILY_INSTANCES is unset, so this is safe to start always.
    app.state.daily_task = asyncio.create_task(daily.loop(stop))
    try:
        yield
    finally:
        stop.set()
        for t in (app.state.scheduler_task, app.state.refill_task,
                  app.state.autopilot_task, app.state.daily_task):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(t, timeout=5)


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(title="Mallory — Acquisition Orchestrator", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in s.cors_origins.split(",")],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(auth.router)

    # Reachable WITHOUT a session. Everything not listed here is behind the gate — an allowlist, not
    # a blocklist, so a route added later is protected by default instead of by remembering to.
    #   /health   the container healthcheck runs before anyone logs in
    #   /login    the form itself, and the endpoints it posts to
    _OPEN = {"/health", "/login", "/api/auth/login", "/api/auth/verify", "/api/auth/me",
             "/api/auth/logout", "/favicon.ico"}

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if not auth.enabled() or request.url.path in _OPEN:
            return await call_next(request)
        with SessionLocal() as db:
            user = auth.current_user(request, db)
        if user:
            return await call_next(request)
        # An unauthenticated XHR must get a 401 it can act on, not an HTML login page parsed as
        # JSON — the dashboard polls constantly, and "Unexpected token <" is a useless symptom for
        # what is really just an expired session.
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse("/login", status_code=302)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "mallory-orchestrator", "version": __version__}

    _login = Path(__file__).parent / "static" / "login.html"

    @app.get("/login", response_class=HTMLResponse)
    def login_page():
        if not auth.enabled():
            return RedirectResponse("/", status_code=302)
        return HTMLResponse(_login.read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-store, must-revalidate"})

    _ui = Path(__file__).parent / "static" / "index.html"

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        body = _ui.read_text(encoding="utf-8") if _ui.exists() else "<h1>admin UI missing</h1>"
        # NO-STORE. This route sent no cache headers at all, so browsers applied their own heuristic
        # freshness and kept serving a deployed-over copy of the page. The whole dashboard — every
        # panel, every number — is JavaScript inside this one file, so a stale copy is not a stale
        # asset, it is the ENTIRE UI running last week's logic against today's API. Measured twice:
        # after fixing the site-completeness rule the API returned 192 while the operator's screen
        # still read 618, from a cached script computing it the old way. There is nothing to gain
        # from caching a single small always-changing document that is the app itself.
        return HTMLResponse(body, headers={"Cache-Control": "no-store, must-revalidate"})

    # Digital twin: NOT a separate page any more. It is a section OF the one dashboard at "/"
    # (Watchfloor · Corpus Explorer · Control panel · Digital Twin), served by that page's JS
    # against the /api/twin/* routes — one console, no second URL to remember.

    return app


app = create_app()

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from crawler_coverage_auditor.analysis.states import LABELS, METHOD_LABELS, RUN_LABELS
from crawler_coverage_auditor.config import (
    AUTOMATIC_DISCOVERY_SOURCES,
    CORPUS_INTERVAL_SECONDS,
    DISCOVERY_ENGINE_VERSION,
    WEBSITE_DELAY_SECONDS,
    AuditConfig,
    automatic_scan_settings,
    paced_scan_settings,
)
from crawler_coverage_auditor.corpus.credentials import (
    api_url,
    public_key_status,
    save_key,
)
from crawler_coverage_auditor.services import queries
from crawler_coverage_auditor.services.jobs import AuditJobs
from crawler_coverage_auditor.services.revision import BackendRevision
from crawler_coverage_auditor.validation.text_coverage import TEXT_API_VERSION

CaptureType = Literal['HTML', 'PDF', 'DOCUMENT', 'IMAGE', 'API', 'FEED', 'MEDIA', 'OTHER']
DEFAULT_CAPTURE = ['HTML', 'PDF', 'DOCUMENT']
ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseModel):
    schema_version: Literal[2] = 2
    pacing_version: Literal[2] = 2
    discovery_version: Literal[3] = DISCOVERY_ENGINE_VERSION
    capture_types: list[CaptureType] = Field(default_factory=lambda: DEFAULT_CAPTURE.copy(), min_length=1, max_length=8)
    discovery_sources: list[Literal['SITEMAP', 'STATIC', 'RENDERED', 'API', 'STRUCTURED_DATA', 'HTTP_LINK', 'FEED', 'FILTER_GET', 'FILTER_BROWSER', 'PAGINATION', 'TABS', 'LOAD_MORE', 'JS_SOURCE', 'METADATA', 'PUBLIC_API', 'RESOURCESYNC', 'EXTERNAL_INDEX']] = Field(default_factory=lambda: sorted(AUTOMATIC_DISCOVERY_SOURCES), max_length=17)
    include_subdomains: bool = True
    external_resources: Literal[False] = False
    use_browser: bool = True
    reuse_public_session: bool = True
    browser_challenge_wait_seconds: float = Field(default=12, ge=0, le=30)
    max_concurrency: Literal[1] = 1
    website_delay_seconds: float = Field(default=WEBSITE_DELAY_SECONDS, ge=WEBSITE_DELAY_SECONDS, le=30)
    request_timeout_seconds: float = Field(default=25, ge=5, le=120)
    max_retries: int = Field(default=2, ge=0, le=4)
    corpus_batch_size: Literal[1] = 1
    corpus_interval_seconds: float = Field(default=CORPUS_INTERVAL_SECONDS, ge=CORPUS_INTERVAL_SECONDS, le=30)
    corpus_timeout_seconds: float = Field(default=60, ge=60, le=180)


class Discovery(BaseModel):
    target_url: str = Field(min_length=8, max_length=2048)
    capture_types: list[CaptureType] | None = Field(default=None, min_length=1, max_length=8)


def public_run(run: dict) -> dict:
    # Configuration/server addresses are not user-facing controls.
    return {key: run.get(key) for key in ('id', 'target_url', 'status', 'started_at', 'completed_at',
            'discovery_completed_at', 'verification_started_at', 'verification_completed_at', 'discovered_urls', 'phase')} | {
        'capture_types': run.get('config', {}).get('capture_types', ['HTML', 'PDF', 'DOCUMENT', 'IMAGE', 'OTHER']),
        'status_label': RUN_LABELS.get(run.get('status'), 'Unknown'),
        'corpus_provider': run.get('config', {}).get('corpus_provider', 'historical'),
        'discovery_upgrade_available': run.get('discovery_engine_version',1) < DISCOVERY_ENGINE_VERSION,
        'discovery_work_remaining': run.get('discovery_work_remaining',False),
        'needs_api_check': run.get('config', {}).get('corpus_provider') != 'coverage_api' or run.get('corpus_url', '').rstrip('/') != api_url().rstrip('/') or run.get('config', {}).get('corpus_text_api_version') != TEXT_API_VERSION}


def create_app(database: Path | None = None, reports: Path | None = None,
               frontend: Path | None = None, jobs: AuditJobs | None = None) -> FastAPI:
    database = Path(database or Path(os.getenv('CRAWLSCOPE_DATA_DIR', str(ROOT / 'data'))) / 'coverage.sqlite').resolve()
    reports = Path(reports or os.getenv('CRAWLSCOPE_REPORT_DIR', str(ROOT / 'reports'))).resolve()
    frontend = Path(frontend or ROOT / 'frontend' / 'dist').resolve()
    settings_path = database.parent / 'capture_settings.json'
    jobs = jobs or AuditJobs()
    settings_lock = threading.Lock()
    revision = BackendRevision(ROOT)
    app = FastAPI(title='CrawlScope', docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]', 'testserver'])
    app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:5173', 'http://127.0.0.1:5173'],
                       allow_methods=['GET', 'POST', 'PUT'], allow_headers=['Content-Type', 'X-CrawlScope-Client'])

    @app.middleware('http')
    async def local_writes(request: Request, call_next):
        if request.method in {'POST', 'PUT', 'DELETE', 'PATCH'}:
            origin = request.headers.get('origin')
            allowed = {str(request.base_url).rstrip('/'), 'http://localhost:5173', 'http://127.0.0.1:5173'}
            if request.headers.get('x-crawlscope-client') != '1' or (origin and origin not in allowed):
                from fastapi.responses import JSONResponse
                return JSONResponse({'detail': 'Use the local CrawlScope dashboard to perform this action.'}, status_code=403)
        response = await call_next(request)
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    def saved_runs():
        return queries.history(database)

    def get_run(run_id):
        run = queries.run(database, run_id) if database.is_file() else None
        if not run:
            raise HTTPException(404, 'Saved scan not found')
        return run

    def active_job():
        job = jobs.active()
        return {'run_id': job.run_id, 'action': job.action, 'target': job.target,
                'stopping': job.stop_event.is_set()} if job else None

    def read_settings():
        if settings_path.is_file():
            try:
                saved = json.loads(settings_path.read_text(encoding='utf-8'))
                if saved.get('schema_version', 1) < 2 and 'OTHER' in saved.get('capture_types', []):
                    saved['capture_types'] = sorted(set(saved['capture_types']) | {'API', 'FEED', 'MEDIA'})
                saved['schema_version'] = 2
                saved = automatic_scan_settings(paced_scan_settings(saved, upgrade_legacy=True))
                return Settings.model_validate(saved).model_dump()
            except (ValueError, TypeError, OSError):
                pass
        return Settings().model_dump()

    @app.get('/api/state')
    def state():
        return {'application': 'CrawlScope', 'ui': 'react', 'scan_workflow': 'serial-automatic',
                'backend_revision': revision.loaded, 'backend_update_available': revision.update_available(),
                'runs': [public_run(run) for run in saved_runs()], 'active': active_job(), 'settings': read_settings(),
                'corpus_api': public_key_status(), 'status_model': LABELS, 'run_labels': RUN_LABELS, 'discovery_methods': METHOD_LABELS}

    @app.post('/api/corpus/key')
    async def save_corpus_key(request: Request):
        # Do not use a validation error containing the submitted secret as input.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 4096:
                raise HTTPException(413, 'API-key request is too large.')
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(422, 'Enter a valid Corpus API key.') from None
        key = body.get('key') if isinstance(body, dict) else None
        if not isinstance(key, str) or not 1 <= len(key.strip()) <= 512:
            raise HTTPException(422, 'Enter a valid Corpus API key.')
        with settings_lock:
            if jobs.active():
                raise HTTPException(409, 'Pause the scan and wait for it to save before changing the API key.')
            if os.getenv('CRAWLSCOPE_CORPUS_API_KEY', '').strip():
                raise HTTPException(409, 'The API key is managed by the backend environment. Remove CRAWLSCOPE_CORPUS_API_KEY from that configuration and restart before saving a key here.')
            try:
                save_key(key)
            except ValueError:
                raise HTTPException(422, 'Could not save this key. Use a valid API key on Windows, or configure the backend environment on other systems.') from None
            except OSError:
                raise HTTPException(503, 'Could not write the protected local credential file.') from None
        return public_key_status()

    @app.get('/api/main-crawler/features')
    def main_crawler_features():
        from crawler_coverage_auditor.integrations.main_crawler import status
        try:
            return status()
        except (OSError, ValueError, RuntimeError) as error:
            raise HTTPException(503, 'Main crawler source feature pack is unavailable; verify the local import') from error

    @app.put('/api/settings')
    def save_settings(value: Settings):
        with settings_lock:
            if jobs.active():
                raise HTTPException(409, 'Capture selection is locked until the current scan finishes.')
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = settings_path.with_suffix('.tmp')
            settings = automatic_scan_settings(value.model_dump())
            settings['capture_types'] = sorted(set(value.capture_types))
            temporary.write_text(json.dumps(settings), encoding='utf-8')
            temporary.replace(settings_path)
        return read_settings()

    @app.post('/api/discover', status_code=202)
    def discover(value: Discovery):
        require_current_backend()
        try:
            from crawler_coverage_auditor.validation.network_safety import (
                validate_target,
            )
            validate_target(value.target_url.strip())
            options = read_settings()
            options.pop('schema_version')
            if value.capture_types is not None:
                options['capture_types'] = value.capture_types
            config = AuditConfig(target_url=value.target_url.strip(), **options)
            job = jobs.start(config.target_url, database, reports,
                             api_url(), capture_types=config.capture_types, settings=options)
            if job.error:
                raise HTTPException(500, job.error)
            return {'run_id': job.run_id, 'action': job.action}
        except ValueError as error:
            raise HTTPException(409 if jobs.active() else 422, str(error)) from error

    @app.post('/api/runs/{run_id}/resume', status_code=202)
    def resume(run_id: int):
        require_current_backend()
        get_run(run_id)
        try:
            job = jobs.resume(run_id, database, reports)
            if job.error:
                raise HTTPException(500, job.error)
            return {'run_id': job.run_id, 'action': job.action}
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post('/api/runs/{run_id}/verify', status_code=202)
    def verify(run_id: int):
        require_current_backend()
        get_run(run_id)
        try:
            job = jobs.start_corpus(run_id, database, reports)
            if job.error:
                raise HTTPException(500, job.error)
            return {'run_id': job.run_id, 'action': job.action}
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    def require_current_backend():
        # Reads and pausing remain available; never launch a worker using stale
        # imported modules after the operator has updated source files.
        if revision.update_available(force=True):
            raise HTTPException(409, 'Backend code has changed. Pause any active scan, wait for it to save, then restart CrawlScope before starting or resuming a scan.')

    @app.post('/api/runs/{run_id}/stop', status_code=202)
    def stop(run_id: int):
        job = jobs.active()
        if not job or job.run_id != run_id:
            raise HTTPException(409, 'This scan is not currently running in this dashboard.')
        job.stop_event.set()
        return {'stopping': True}

    @app.get('/api/runs/{run_id}')
    def snapshot(run_id: int, diagnostics: bool = True):
        run = get_run(run_id)
        return queries.summary(database, run_id, diagnostics=diagnostics) | {'run': public_run(run), 'active': active_job()}

    app.get('/api/runs/{run_id}/summary')(snapshot)

    @app.get('/api/sites')
    def sites():
        return {'items': [item | {'run': public_run(item['run'])} for item in queries.sites(database)]}

    def filters(request):
        allowed = {'search', 'host', 'type', 'corpus_status', 'validation_status', 'depth', 'method', 'http_status', 'sort', 'order'}
        return {key: value for key, value in request.query_params.items() if key in allowed}

    @app.get('/api/runs/{run_id}/urls')
    def urls(run_id: int, request: Request, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=100)):
        get_run(run_id)
        return queries.url_page(database, run_id, filters(request), page, page_size)

    @app.get('/api/runs/{run_id}/urls/export')
    def export(run_id: int, request: Request):
        get_run(run_id)
        kind = request.query_params.get('type', 'all').lower()
        if kind not in {'all', 'html', 'pdf', 'document', 'image', 'api', 'feed', 'media', 'other'}:
            kind = 'all'
        return StreamingResponse(queries.export_csv(database, run_id, filters(request)), media_type='text/csv',
            headers={'Content-Disposition': f'attachment; filename="crawlscope-{run_id}-{kind}.csv"'})

    @app.get('/api/runs/{run_id}/urls/{url_id}')
    def detail(run_id: int, url_id: int):
        get_run(run_id)
        result = queries.url_detail(database, run_id, url_id)
        if result is None:
            raise HTTPException(404, 'URL not found in this inventory')
        return result

    @app.get('/api/runs/{run_id}/activity')
    def activity(run_id: int, limit: int = Query(80, ge=1, le=200), after_id: int = Query(0, ge=0), severity: str = '', stage: str = '', kind: str = ''):
        get_run(run_id)
        return queries.activity(database, run_id, limit, after_id, severity, stage, kind)

    @app.get('/api/runs/{run_id}/checkpoint')
    def checkpoint(run_id: int):
        get_run(run_id)
        return queries.checkpoint(database, run_id)

    @app.get('/api/runs/{run_id}/live-urls')
    def live_urls(run_id: int, type: str = '', host: str = '', errors: bool = False, missing: bool = False):
        get_run(run_id)
        return {'items': queries.live_urls(database, run_id, type=type, host_filter=host, errors=errors, missing=missing)}

    @app.get('/api/runs/{run_id}/analysis')
    def analysis(run_id: int):
        get_run(run_id)
        return queries.analysis(database, run_id)

    @app.get('/api/runs/{run_id}/reports/{kind}')
    def report(run_id: int, kind: Literal['html', 'xlsx']):
        get_run(run_id)
        directory = reports / f'run_{run_id}'
        # Fixed names only; never accept a caller-supplied file path.
        candidates = sorted(directory.glob(f'*.{kind}'))
        if not candidates or not candidates[0].resolve().is_relative_to(reports):
            raise HTTPException(404, 'Report is not ready yet')
        return FileResponse(candidates[0], filename=f'crawlscope-{run_id}.{kind}')

    if (frontend / 'assets').is_dir():
        app.mount('/assets', StaticFiles(directory=frontend / 'assets'), name='assets')

    @app.get('/')
    def index():
        if not (frontend / 'index.html').is_file():
            raise HTTPException(503, 'Build the React frontend first: npm.cmd --prefix frontend run build')
        return FileResponse(frontend / 'index.html', headers={'Cache-Control': 'no-cache'})

    return app

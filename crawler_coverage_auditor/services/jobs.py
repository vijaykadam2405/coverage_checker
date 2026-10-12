"""Framework-independent audit workers and one-at-a-time job lifecycle."""
from __future__ import annotations

import asyncio
import threading
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

from crawler_coverage_auditor.auditor import CoverageAuditor
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.corpus.credentials import api_url, load_key
from crawler_coverage_auditor.validation.text_coverage import TEXT_API_VERSION


@dataclass
class AuditJob:
    target: str
    action: str = 'discovery'
    run_id: int | None = None
    error: str | None = None
    done: bool = False
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    ready: threading.Event = field(default_factory=threading.Event)


class AuditJobs:
    """One writer at a time per desktop service, shared across browser sessions."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: list[AuditJob] = []

    def active(self) -> AuditJob | None:
        with self._lock:
            return next((job for job in self._jobs if not job.done), None)

    def start(self, target: str, database: Path, reports: Path, corpus: str, *, capture_types: set[str] | None = None, settings: dict | None = None) -> AuditJob:
        load_key()  # Fail before discovery if automatic verification is not configured.
        values = dict(use_browser=True, include_subdomains=True, max_concurrency=1, website_delay_seconds=1.5, corpus_interval_seconds=3.0, max_retries=2)
        values.update(settings or {})
        values.update(max_concurrency=1, corpus_batch_size=1,
            website_delay_seconds=max(1.5, values.get('website_delay_seconds', 1.5)),
            corpus_interval_seconds=max(3.0, values.get('corpus_interval_seconds', 3.0)))
        config = AuditConfig(target_url=target, database_path=database, output_dir=reports, corpus_url=corpus,
            discovery_only=False, **values)
        if capture_types is not None:
            config.capture_types = set(capture_types)
            config.__post_init__()
        return self._launch(config, reports, 'discovery')

    def resume(self, run_id: int, database: Path, reports: Path) -> AuditJob:
        """Continue interrupted discovery, or update an already saved inventory."""
        from crawler_coverage_auditor.services.queries import run
        saved = run(database, run_id)
        if not saved:
            raise ValueError('Saved scan not found')
        if saved.get('discovery_completed_at'):
            return self.start_corpus(run_id, database, reports)
        load_key()
        values = {name: value for name, value in saved['config'].items() if name in {item.name for item in fields(AuditConfig)}}
        values.update(target_url=saved['target_url'], corpus_url=api_url(), database_path=database,
            output_dir=reports / f'run_{run_id}', discovery_only=False, resume=True,
            seed_hosts=set(values.get('seed_hosts', [])) if isinstance(values.get('seed_hosts', []), (list, set, tuple)) else set(),
            max_concurrency=1, corpus_batch_size=1,
            website_delay_seconds=max(1.5, values.get('website_delay_seconds', 1.5)),
            corpus_interval_seconds=max(3.0, values.get('corpus_interval_seconds', 3.0)))
        return self._launch(AuditConfig(**values), reports, 'discovery', run_id)

    def start_corpus(self, run_id: int, database: Path, reports: Path) -> AuditJob:
        from crawler_coverage_auditor.services.queries import run
        saved = run(database, run_id)
        if not saved or not saved.get('discovery_completed_at'):
            raise ValueError('Complete URL discovery before starting corpus verification')
        if saved.get('status') not in {'AWAITING_CORPUS', 'CORPUS_PAUSED', 'CANCELLED', 'FAILED', 'RUNNING'} and saved['config'].get('corpus_provider') == 'coverage_api' and saved['corpus_url'].rstrip('/') == api_url().rstrip('/') and saved['config'].get('corpus_text_api_version') == TEXT_API_VERSION:
            raise ValueError('This inventory has already been verified')
        load_key()  # Local check only, before launching a worker. No secret enters config.
        values = {name: value for name, value in saved['config'].items() if name in {item.name for item in fields(AuditConfig)}}
        if 'discovery_sources' not in values and 'OTHER' in values.get('capture_types', []):
            values['capture_types'] = set(values['capture_types']) | {'API', 'FEED', 'MEDIA'}
        seed_hosts = values.get('seed_hosts', [])
        values.update(target_url=saved['target_url'], corpus_url=api_url(), corpus_provider='coverage_api', database_path=database,
                      output_dir=reports / f'run_{run_id}', seed_hosts=set(seed_hosts) if isinstance(seed_hosts, (list, tuple, set)) else set(),
                      corpus_interval_seconds=max(3.0, values.get('corpus_interval_seconds', 3.0)),
                      corpus_timeout_seconds=max(60.0, values.get('corpus_timeout_seconds', 60.0)))
        return self._launch(AuditConfig(**values), reports, 'corpus', run_id)

    def _launch(self, config: AuditConfig, reports: Path, action: str, run_id: int | None = None) -> AuditJob:
        with self._lock:
            if any(not job.done for job in self._jobs):
                raise ValueError("An audit is already running. Wait for it to finish or stop it first.")
            job = AuditJob(config.target_url, action=action)
            self._jobs.append(job)
        def run():
            auditor = None
            try:
                kwargs = {'stopped': job.stop_event.is_set}
                if run_id is not None:
                    kwargs['run_id'] = run_id
                auditor = CoverageAuditor(config, **kwargs)
                job.run_id = auditor.run_id
                config.output_dir = reports / f"run_{auditor.run_id}"
                # Save the actual report location for history downloads.
                import json
                from dataclasses import asdict
                auditor.db.connection.execute("UPDATE audit_runs SET config_json=?,corpus_url=? WHERE id=?", (json.dumps(asdict(config), default=lambda value: sorted(value) if isinstance(value, set) else str(value)), config.corpus_url, auditor.run_id))
                auditor.db.connection.commit()
                job.ready.set()
                # Server loop policies must not prevent Windows browser subprocesses.
                loop_factory = asyncio.ProactorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
                with asyncio.Runner(loop_factory=loop_factory) as runner:
                    runner.run(auditor.verify_inventory() if action == 'corpus' else auditor.run())
            except Exception as error:
                job.error = f"{type(error).__name__}: {error}"
            finally:
                job.done = True
                job.ready.set()
        job.thread = threading.Thread(target=run, name="CrawlScope-audit", daemon=True)
        job.thread.start()
        job.ready.wait(timeout=5)
        return job

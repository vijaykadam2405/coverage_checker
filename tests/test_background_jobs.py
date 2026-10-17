from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from crawler_coverage_auditor.services.jobs import AuditJobs
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL


def test_background_worker_supports_subprocesses_and_separate_reports(tmp_path: Path, monkeypatch):
    """Chromium requires subprocess support even if a Windows server changes loop policy."""
    class SubprocessAuditor:
        def __init__(self, config, **kwargs):
            self.config = config
            self.db = AuditDatabase(config.database_path)
            self.run_id = self.db.create_run(config.target_url, config.corpus_url, {})

        async def run(self):
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", "print('worker ok')", stdout=asyncio.subprocess.PIPE)
            output, _ = await process.communicate()
            assert output.strip() == b"worker ok"
            assert self.config.output_dir.name == f"run_{self.run_id}"
            self.db.finish_run(self.run_id)
            self.db.close()
    monkeypatch.setattr("crawler_coverage_auditor.services.jobs.CoverageAuditor", SubprocessAuditor)
    manager = AuditJobs()
    job = manager.start("https://example.test/", tmp_path / "coverage.sqlite", tmp_path / "reports", "https://corpus.test/")
    job.thread.join(timeout=10)
    assert job.done and job.error is None
    assert manager.active() is None


def test_corpus_worker_reopens_saved_inventory_without_discovery(tmp_path, monkeypatch):
    database_path = tmp_path / 'coverage.sqlite'
    database = AuditDatabase(database_path)
    target = 'https://www.example.test/'
    run_id = database.create_run(target, 'https://corpus.test/', {'seed_hosts': "{'www.example.test'}", 'include_subdomains': True, 'discovery_only': True})
    database.add_url(run_id, DiscoveredURL(target, target, 'SEED', 0))
    database.inventory_ready(run_id)
    # A completed dashboard-era inventory must be upgradeable without discovery.
    database.finish_verification(run_id, 'COMPLETED')
    database.close()
    called = []
    class SavedInventoryAuditor:
        def __init__(self, config, **kwargs):
            assert config.seed_hosts == {'www.example.test', 'example.test'}
            assert config.corpus_provider == 'coverage_api'
            assert config.corpus_url.endswith(':8445/')
            assert kwargs['run_id'] == run_id
            self.db = AuditDatabase(database_path)
            self.run_id = run_id
        async def run(self):
            raise AssertionError('Corpus action must not discover the website again')
        async def verify_inventory(self):
            called.append('verified')
            self.db.finish_verification(run_id, 'COMPLETED')
            self.db.close()
    monkeypatch.setattr('crawler_coverage_auditor.services.jobs.CoverageAuditor', SavedInventoryAuditor)
    manager = AuditJobs()
    job = manager.start_corpus(run_id, database_path, tmp_path / 'reports')
    job.thread.join(timeout=10)
    assert job.done and job.error is None
    assert job.action == 'corpus' and called == ['verified']

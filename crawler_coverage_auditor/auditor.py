from __future__ import annotations

import asyncio
import csv
import json
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import asdict
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

from crawler_coverage_auditor.analysis.coverage import coverage_by_depth, coverage_summary, is_inventory_url, is_found
from crawler_coverage_auditor.analysis.gaps import missing_rows
from crawler_coverage_auditor.analysis.root_cause import root_causes
from crawler_coverage_auditor.analysis.inventory_segments import archive_coverage, js_only_rows, sitemap_coverage
from crawler_coverage_auditor.analysis.regression import compare_runs
from crawler_coverage_auditor.config import AuditConfig
from crawler_coverage_auditor.corpus.errors import CorpusUnavailable
from crawler_coverage_auditor.corpus.coverage_api import CoverageAPIClient
from crawler_coverage_auditor.discovery.browser import BrowserExplorer
from crawler_coverage_auditor.discovery.filters import detect_filters, get_filter_urls
from crawler_coverage_auditor.discovery.max_page import find_maximum_page, PaginationUnverified
from crawler_coverage_auditor.discovery.pagination import detect_pagination, numeric_page_parameter, pagination_series_key, replace_page_parameter
from crawler_coverage_auditor.discovery.resources import json_links, resource_type, trap_reason, URL_ONLY_TYPES, link_resource_hint
from crawler_coverage_auditor.discovery.evidence import embedded_links, feed_links, header_links
from crawler_coverage_auditor.discovery.sitemap import discover_sitemaps
from crawler_coverage_auditor.discovery.static_links import count_listing_items, extract_links
from crawler_coverage_auditor.reporting.excel import write_excel
from crawler_coverage_auditor.reporting.html import write_html
from crawler_coverage_auditor.storage.database import AuditDatabase
from crawler_coverage_auditor.storage.models import DiscoveredURL
from crawler_coverage_auditor.validation.fingerprints import sha256_text
from crawler_coverage_auditor.validation.text_coverage import compare_html_text, ASSESSED_TEXT_STATUSES, TEXT_API_VERSION
from crawler_coverage_auditor.validation.http import PoliteHttpClient
from crawler_coverage_auditor.validation.normalize import normalize_url, same_scope
from crawler_coverage_auditor.validation.soft404 import assess_page_validity
from crawler_coverage_auditor.validation.text_extract import extract_text
from crawler_coverage_auditor.validation.access import detect_access
from crawler_coverage_auditor.validation.site_session import SiteSessionStore

LOGGER = logging.getLogger(__name__)


class CoverageAuditor:
    """Independent website inventory, conservative corpus verification, and persisted live evidence."""

    def __init__(self, config: AuditConfig, *, on_event=None, stopped=None, run_id: int | None = None) -> None:
        self.config = config
        self.db = AuditDatabase(config.database_path)
        self.run_id = run_id or (self.db.latest_resumable_run(config.target_url) if config.resume else None)
        if run_id:
            existing = self.db.connection.execute("SELECT target_url,discovery_completed_at FROM audit_runs WHERE id=?", (run_id,)).fetchone()
            if not existing or existing['target_url'] != config.target_url:
                self.db.close()
                raise ValueError("Select a saved scan for this website before resuming")
        if self.run_id is None:
            self.run_id = self.db.create_run(config.target_url, config.corpus_url, asdict(config))
        self._inventory_was_ready = bool(self.db.connection.execute('SELECT discovery_completed_at FROM audit_runs WHERE id=?', (self.run_id,)).fetchone()[0])
        self.on_event = on_event
        self.stopped = stopped or (lambda: False)
        self.phase = "Starting"
        self._pagination_series_seen: set[str] = set()
        self._browser_seen: set[str] = set()
        self._limited: set[tuple] = set()
        self._last_successful: str | None = None
        self._robots: RobotFileParser | None = None
        self._robots_by_origin: dict[str, RobotFileParser] = {}
        self._robots_locks: dict[str, asyncio.Lock] = {}
        self._website_client = None
        self._report_status = "RUNNING"
        self._content_seen: dict[tuple, dict[str, str]] = {}
        self._paused_hosts: set[str] = set()
        self._public_session = None
        self._corpus_api = None
        self._browser = None
        self._api_prepared = False
        self._pagination_probing = False
        self._processing_probe = False
        self._url_count = self.db.connection.execute("SELECT COUNT(*) FROM urls WHERE audit_run_id=?", (self.run_id,)).fetchone()[0]
        self._limited.update((row['stage'], row['url'], row['reason']) for row in self.db.connection.execute("SELECT stage,url,reason FROM audit_limits WHERE audit_run_id=?", (self.run_id,)))

    def _event(self, kind: str, message: str, url: str | None = None, depth: int | None = None,
               severity: str = "INFO", **details) -> None:
        if kind in {'CORPUS_BACKOFF', 'CORPUS_RECOVERED'}:
            self.phase = 'Recovering corpus API · paced retry' if kind == 'CORPUS_BACKOFF' else 'Checking corpus API · one URL at a time'
            self.db.set_phase(self.run_id, self.phase)
        self.db.event(self.run_id, self.phase, kind, message, url, depth, severity, **details)
        if self.on_event:
            self.on_event({"run_id": self.run_id, "stage": self.phase, "kind": kind, "message": message,
                           "url": url, "depth": depth, "severity": severity, **details})

    def _phase(self, phase: str) -> None:
        self.phase = phase
        self.db.set_phase(self.run_id, phase)
        self._event("PHASE", phase)

    def _limit(self, stage: str, url: str | None, reason: str) -> None:
        key = (stage, url, reason)
        if key not in self._limited:
            self._limited.add(key)
            self.db.limit(self.run_id, stage, url, reason, self._last_successful)
            self._event("LIMITED", reason, url, severity="WARNING", last_successful_url=self._last_successful)

    async def _allowed(self, url: str) -> bool:
        if not self.config.respect_robots or not same_scope(url, self.config.seed_hosts, self.config.include_subdomains):
            return True
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        async with self._robots_locks.setdefault(origin, asyncio.Lock()):
            if origin not in self._robots_by_origin:
                robots_url = origin + '/robots.txt'
                response = await self._website_client.get(robots_url, check_policy=False, discovery_metadata=True)
                parser = RobotFileParser(robots_url)
                if response.status_code == 200:
                    parser.parse(response.text.splitlines())
                    delay = parser.crawl_delay(self.config.user_agent)
                    if delay:
                        self._website_client.set_host_interval(url, float(delay))
                elif 400 <= response.status_code < 500 and response.status_code != 429:
                    parser.allow_all = True
                    if response.status_code not in {404, 410}:
                        self._event("ROBOTS_UNAVAILABLE", f"robots.txt returned HTTP {response.status_code}: no rules available. Trying public content per RFC 9309; this is not permission to bypass page access controls.", robots_url, severity="WARNING")
                else:
                    parser.disallow_all = True
                    self._limit("Robots", robots_url, f"Robots policy unavailable: HTTP {response.status_code}; {response.error or ''}; host exploration paused")
                self._robots_by_origin[origin] = parser
            return self._robots_by_origin[origin].can_fetch(self.config.user_agent, url)

    async def run(self) -> dict:
        if self.config.resume and self._inventory_was_ready:
            return await self.verify_inventory()
        try:
            seed = normalize_url(self.config.target_url)
            self._event("STARTED", "CrawlScope audit started", seed)
            if self.config.incremental:
                previous = self.db.latest_completed_run(self.config.target_url)
                for row in self.db.report_rows(previous) if previous else []:
                    if is_inventory_url(dict(row)) and row["match_status"] in {"NOT_FOUND", "REVIEW_REQUIRED"}:
                        self._add_discovery(row["url"], "INCREMENTAL_RECHECK", row["min_depth"], None, "")
                if not previous:
                    self._add_discovery(seed, "SEED", 0, None, "")
            else:
                self._add_discovery(seed, "SEED", 0, None, "")
            async with AsyncExitStack() as stack:
                if not self.config.discovery_only:
                    self._corpus_api = await stack.enter_async_context(self._new_api_client())
                    self._prepare_api_verification()
                    # Recover interrupted operations, not completed website fetches.
                    self.db.connection.execute("UPDATE urls SET state='PENDING' WHERE audit_run_id=? AND state IN ('FETCHING','RENDERING')", (self.run_id,))
                    self.db.connection.commit()
                    if await self._verify_all(api=self._corpus_api, ready_only=True) == 'CORPUS_PAUSED':
                        raise CorpusUnavailable('Saved URL checks are incomplete; resume when Corpus is available')
                client = await stack.enter_async_context(PoliteHttpClient(1,
                    self.config.request_timeout_seconds, self.config.max_retries, self.config.user_agent,
                    on_event=self._event, allowed=self._allowed, min_interval=self.config.website_delay_seconds, stopped=self.stopped))
                self._website_client = client
                try:
                    directory = Path(os.environ.get("CRAWLSCOPE_SESSION_DIR", str(Path(self.config.database_path).parent / "sessions")))
                    self._public_session = SiteSessionStore(directory).load(self.config.target_url) if self.config.reuse_public_session else None
                    if self._public_session:
                        client.use_browser_session(self.config.target_url, self._public_session['state']['cookies'], self._public_session.get('user_agent'))
                        self._event("SESSION_LOADED", "Reusing locally prepared public-session cookies; values are not exported", seed)
                except (OSError, ValueError, KeyError, TypeError):
                    self._limit("Public session", seed, "Saved session cannot be loaded; prepare a fresh session. Continuing without it.")
                self._phase("Sitemap discovery")
                if self.config.respect_robots:
                    await self._allowed(seed)
                if self.config.discovers('SITEMAP'):
                    await discover_sitemaps(seed, client, on_sitemap=self._on_sitemap, on_url=self._on_sitemap_url,
                                           on_error=lambda url, reason: self._limit("Sitemap", url, reason), stopped=self.stopped)
                self._phase("Discovering website URLs")
                if self.config.use_browser and self.config.discovers('RENDERED') and not self.stopped():
                    browser = await stack.enter_async_context(BrowserExplorer(self.config.max_browser_actions,
                        on_event=self._browser_event, allowed=self._allowed, stopped=self.stopped, pace=client.wait_turn,
                        storage_state=self._public_session['state'] if self._public_session else None,
                        challenge_wait_seconds=self.config.browser_challenge_wait_seconds,
                        on_response=client.observe_response,
                        user_agent=self._public_session.get('user_agent') if self._public_session else None,
                        sources=self.config.discovery_sources, max_filter_values=self.config.max_filter_values))
                    if browser.error:
                        self._limit("Browser", seed, browser.error)
                    else:
                        self._browser = browser
                elif not self.config.use_browser:
                    self._limit("Browser", seed, "Rendered JavaScript discovery disabled for this run")
                await self._crawl_static(client)
            if self.stopped():
                self._limit("Audit", seed, "Stopped by user; pending inventory and corpus checks remain")
            self._phase("Generating analysis")
            self.db.reconcile_navigation_depth(self.run_id)
            if not self.stopped():
                self.db.inventory_ready(self.run_id)
                self._event("INVENTORY_READY", f"Inventory saved: {len(self.db.report_rows(self.run_id)):,} selected URLs; each selected valid URL was checked during discovery." if not self.config.discovery_only else "Discovery-only inventory saved", seed)
            self._report_status = "CANCELLED" if self.stopped() else "AWAITING_CORPUS"
            if not self.config.discovery_only and not self.stopped():
                self._report_status = "LIMITED" if self._limited else "COMPLETED"
            report = self._write_reports()
            if self.config.compare_run:
                report.update(self._write_regression(self.config.compare_run))
            status = self._report_status
            if status != "AWAITING_CORPUS":
                self.db.finish_verification(self.run_id, status)
            else:
                self.db.set_phase(self.run_id, "Inventory ready · waiting for corpus check")
            self._event(status, "Inventory ready; corpus has not been contacted" if status == "AWAITING_CORPUS" else "Audit finished; coverage refers to the observed inventory", seed)
            report["status"] = status
            return report
        except CorpusUnavailable as error:
            self._limit('Corpus API', None, str(error))
            self._report_status = 'CANCELLED' if self.stopped() else 'CORPUS_PAUSED'
            self.db.reconcile_navigation_depth(self.run_id)
            report = self._write_reports()
            self.db.finish_run(self.run_id, self._report_status, str(error))
            self._event(self._report_status, 'Scan paused; saved website snapshots and successful checks will be reused', severity='WARNING')
            report['status'] = self._report_status
            return report
        except Exception as error:
            self.db.add_error(self.run_id, self.phase, None, f"{type(error).__name__}: {error}")
            self._event("FAILED", str(error), severity="ERROR")
            self.db.finish_run(self.run_id, "FAILED", str(error))
            raise
        finally:
            self.db.close()

    async def verify_inventory(self) -> dict:
        """Check a durable inventory only; never re-crawl the website in this phase."""
        try:
            self._report_status = await self._verify_all()
            report = self._write_reports()
            self.db.finish_verification(self.run_id, self._report_status)
            self._event(self._report_status, "Corpus check complete" if self._report_status in {'COMPLETED', 'LIMITED'} else "Corpus check paused; saved results can be resumed")
            report['status'] = self._report_status
            return report
        except Exception as error:
            self.db.add_error(self.run_id, self.phase, None, f"{type(error).__name__}: {error}")
            self.db.finish_run(self.run_id, "CORPUS_PAUSED", str(error))
            raise
        finally:
            self.db.close()

    def _new_api_client(self):
        return CoverageAPIClient(self.config.corpus_url, timeout=self.config.corpus_timeout_seconds,
            retries=self.config.max_retries, min_interval=self.config.corpus_interval_seconds,
            on_event=self._event, stopped=self.stopped)

    def _prepare_api_verification(self):
        if self._api_prepared:
            return
        self.db.start_verification(self.run_id)
        provider = 'coverage_api:' + self.config.corpus_url.rstrip('/')
        self.config.corpus_provider = 'coverage_api'
        self.db.connection.execute('UPDATE audit_runs SET corpus_url=?,config_json=? WHERE id=?',
            (self.config.corpus_url, json.dumps(asdict(self.config) | {'corpus_text_api_version': TEXT_API_VERSION}, default=lambda value: sorted(value) if isinstance(value, set) else str(value)), self.run_id))
        self.db.connection.execute('INSERT INTO corpus_match_history SELECT * FROM corpus_matches WHERE audit_run_id=? AND (provider IS NULL OR provider != ?)', (self.run_id, provider))
        self.db.connection.execute('DELETE FROM corpus_matches WHERE audit_run_id=? AND (provider IS NULL OR provider != ?)', (self.run_id, provider))
        self.db.connection.commit()
        self._api_prepared = True

    async def _verify_all(self, *, api=None, only_url=None, ready_only=False) -> str:
        """Shared serial verifier for live scans and saved inventories. Never bulk-check."""
        provider = 'coverage_api:' + self.config.corpus_url.rstrip('/')
        try:
            async with AsyncExitStack() as stack:
                api = api if api is not None else await stack.enter_async_context(self._new_api_client())
                self._prepare_api_verification()
                candidates = [row for row in map(dict, self.db.report_rows(self.run_id, only_url=only_url)) if is_inventory_url(row)
                    and (not ready_only or row['state'] not in {'PENDING', 'FETCHING', 'RENDERING'})]
                for row in candidates:
                    if self.stopped():
                        return 'CORPUS_PAUSED'
                    needs_presence = row['match_status'] not in {'NOT_FOUND', 'EXACT_MATCH', 'NORMALIZED_MATCH', 'CANONICAL_MATCH', 'REDIRECT_MATCH', 'NOT_TRACKED'} or row['page_type'] != 'HTML' and json.loads(row.get('evidence_json') or '{}').get('api_contract_version') != TEXT_API_VERSION
                    needs_text = row['page_type'] == 'HTML' and is_found(row) and row['text_status'] not in ASSESSED_TEXT_STATUSES
                    if not needs_presence and not needs_text:
                        continue
                    if needs_presence:
                        status = None
                        self._phase('Checking corpus API · one URL at a time')
                        self.db.set_url_state(self.run_id, row['normalized_url'], 'CHECKING_CORPUS')
                        self._event('CORPUS_CHECKING', 'Checking this URL in Corpus', row['url'], row['min_depth'])
                        item = (await api.held([row['url']]))[row['url']]
                        if item['lookup_status'] == 'found':
                            self._save_api_match(row, item, 'EXACT_MATCH' if item['match'] == 'exact' else 'NORMALIZED_MATCH', provider, row['url'])
                        elif item['lookup_status'] in {'not_tracked', 'error'}:
                            self._save_api_match(row, item, 'NOT_TRACKED' if item['lookup_status'] == 'not_tracked' else 'API_ERROR', provider, row['url'])
                        else:
                            status, queried, seen = 'NOT_FOUND', row['url'], {row['url']}
                            for alternate, kind in [(row['normalized_url'], 'NORMALIZED_MATCH'), (row.get('final_url'), 'REDIRECT_MATCH'), (row.get('canonical_url'), 'CANONICAL_MATCH')]:
                                if not alternate or alternate in seen or urlsplit(alternate).scheme not in {'http', 'https'} or not urlsplit(alternate).hostname:
                                    continue
                                if kind == 'CANONICAL_MATCH' and not same_scope(alternate, self.config.seed_hosts, self.config.include_subdomains):
                                    continue
                                seen.add(alternate)
                                alias = (await api.held([alternate]))[alternate]
                                if alias['lookup_status'] == 'found':
                                    item, status, queried = alias, kind, alternate
                                    break
                                if alias['lookup_status'] != 'missing':
                                    status = 'API_ERROR'
                            self._save_api_match(row, item, status, provider, queried)
                        if item['lookup_status'] == 'error' or status == 'API_ERROR':
                            raise CorpusUnavailable('URL presence could not be verified; scan paused before the next URL')
                    await self._compare_api_html(api, provider, only_url=row['normalized_url'])
                    self.db.set_url_state(self.run_id, row['normalized_url'], 'DONE')
        except CorpusUnavailable as error:
            self.db.add_error(self.run_id, 'Corpus API', None, str(error))
            self._limit('Corpus API', None, str(error))
            for row in self.db.report_rows(self.run_id):
                if row['state'] == 'CHECKING_CORPUS':
                    self.db.set_url_state(self.run_id, row['normalized_url'], 'DONE')
            self._event('CORPUS_PAUSED', str(error) + '; remaining URLs stay unverified. Resume the saved inventory later.', severity='WARNING')
            return 'CORPUS_PAUSED'
        return "LIMITED" if self._limited else "COMPLETED"

    def _save_api_match(self, row: dict, item: dict, status: str, provider: str, queried: str) -> None:
        previous = json.loads(row.get('evidence_json') or '{}')
        if row.get('match_status') and row['page_type'] != 'HTML' and previous.get('api_contract_version') != TEXT_API_VERSION:
            # Preserve old file-index results while upgrading to the new contract.
            self.db.connection.execute('INSERT INTO corpus_match_history SELECT * FROM corpus_matches WHERE audit_run_id=? AND normalized_url=?', (self.run_id, row['normalized_url']))
        metadata = {name: item.get(name) for name in ('lookup_status', 'kind', 'match', 'match_method', 'matched_url', 'record_id', 'content_type', 'page_url', 'first_fetched', 'last_fetched', 'published_at', 'title', 'versions')}
        metadata['queried_url'] = queried
        metadata['api_contract_version'] = TEXT_API_VERSION
        html = row['page_type'] == 'HTML'
        self.db.add_corpus_match(self.run_id, row['normalized_url'], status, item.get('matched_url') if item['held'] else None, item.get('record_id'), None, None,
            'TEXT_NOT_AVAILABLE' if html else 'URL_ONLY', provider=provider, evidence=metadata)
        self.db.set_url_state(self.run_id, row['normalized_url'], 'DONE')
        message = ('Not found in Corpus — missed by production crawler' if html else 'Resource URL not found in Corpus; file availability not assessed') if status == 'NOT_FOUND' else 'Found in Corpus through Coverage API'
        if status in {'NOT_TRACKED', 'API_ERROR'}:
            message = 'This resource type is not tracked by Corpus; not a crawler miss' if status == 'NOT_TRACKED' else 'API could not verify this URL; retry on resume'
        self._event('MISSING' if status == 'NOT_FOUND' else 'FOUND' if is_found({'match_status': status}) else status, message, row['url'], row['min_depth'])

    def _save_api_text(self, row, provider, status, *, length=None, score=None, metadata=None):
        evidence = json.loads(row.get('evidence_json') or '{}')
        evidence.update(metadata or {})
        self.db.add_corpus_match(self.run_id, row['normalized_url'], row['match_status'], row['corpus_url'],
            evidence.get('record_id') or row.get('document_id'), length, score, status, provider=provider, evidence=evidence)
        self.db.set_url_state(self.run_id, row['normalized_url'], 'DONE')

    async def _compare_api_html(self, api, provider, *, only_url=None):
        rows = [dict(row) for row in self.db.report_rows(self.run_id, only_url=only_url)
                if row['page_type'] == 'HTML' and is_inventory_url(dict(row)) and is_found(dict(row))
                and row['text_status'] not in ASSESSED_TEXT_STATUSES]
        if not rows:
            return
        self._phase('Comparing HTML text · one page at a time')
        retry_needed = False
        for index, row in enumerate(rows, 1):
            if self.stopped():
                raise CorpusUnavailable('Corpus checking stopped; completed comparisons are saved')
            live = self.db.page_snapshot(self.run_id, row['normalized_url'])
            if not live or not live.strip():
                self._save_api_text(row, provider, 'SOURCE_TEXT_NOT_AVAILABLE')
                continue
            presence = json.loads(row.get('evidence_json') or '{}')
            mime = (presence.get('content_type') or '').split(';')[0].strip().lower()
            if presence.get('kind') == 'file' or mime and mime not in {'text/html', 'application/xhtml+xml'}:
                # A stored file masquerading behind an HTML URL is still URL-only.
                self._save_api_text(row, provider, 'TEXT_TYPE_MISMATCH')
                self._limit('Corpus text', row['url'], 'Corpus presence metadata identifies a non-HTML record; no file text requested')
                continue
            self.db.set_url_state(self.run_id, row['normalized_url'], 'CHECKING_CORPUS')
            self._event('TEXT_CHECKING', f'Comparing HTML text {index} of {len(rows)}', row['url'], row['min_depth'])
            try:
                item = (await api.text([row['corpus_url']]))[row['corpus_url']]
            except CorpusUnavailable as error:
                self._save_api_text(row, provider, 'TEXT_API_ERROR')
                raise error
            text = item.get('html_text') or {}
            metadata = {'text_api': {key: item.get(key) for key in ('record_id', 'matched_url', 'match_method', 'content_type', 'version_id', 'last_fetched_at', 'title')},
                        'text_api_status': text.get('status'), 'text_api_truncated': text.get('truncated')}
            if item['lookup_status'] != 'found' or text.get('status') not in {'available', 'empty'}:
                status, comparison = 'TEXT_NOT_AVAILABLE', None
                retry_needed = True
            elif text.get('truncated'):
                status, comparison = 'TEXT_TRUNCATED', None
                self._limit('Corpus text', row['url'], 'Stored HTML text is truncated; full text coverage cannot be assessed')
            elif (item.get('content_type') or '').split(';')[0].strip().lower() not in {'text/html', 'application/xhtml+xml'}:
                status, comparison = 'TEXT_TYPE_MISMATCH', None
                self._limit('Corpus text', row['url'], 'Stored record MIME type is not HTML; text comparison skipped')
            else:
                comparison = compare_html_text(live, text['text'])
                status = comparison['status']
                comparison['evidence']['title_matches'] = (row.get('title') or '').strip().casefold() == (item.get('title') or '').strip().casefold() if row.get('title') and item.get('title') else None
                metadata['text_comparison'] = comparison['evidence']
            self._save_api_text(row, provider, status, length=len(text['text']) if comparison else None,
                                score=comparison['score'] if comparison else None, metadata=metadata)
            self._event('TEXT_COMPARED' if comparison else 'TEXT_UNAVAILABLE',
                f"HTML word coverage: {comparison['score']*100:.1f}% · {status}" if comparison and comparison['score'] is not None else f'HTML text not assessed: {status}',
                row['url'], row['min_depth'], severity='INFO' if comparison else 'WARNING')
        if retry_needed:
            raise CorpusUnavailable('Some stored HTML text was unavailable; URL presence is retained. Resume to retry text checks')

    def _on_sitemap(self, url: str, parent: str | None, kind: str, count: int) -> None:
        self.db.add_sitemap(self.run_id, normalize_url(url), normalize_url(parent) if parent else None, kind, count)
        self._event("SITEMAP", f"Sitemap discovered: {count} entries", url)

    def _on_sitemap_url(self, url: str, sitemap_url: str, _: str | None) -> None:
        self._add_discovery(url, "SITEMAP", 1, normalize_url(sitemap_url), "")

    def _add_discovery(self, url: str, method: str, depth: int, parent: str | None, anchor: str, resource_hint: str | None = None) -> bool:
        url = url.strip()
        source = {'API_DISCOVERY': 'API', 'JS_DISCOVERY': 'RENDERED'}.get(method, method)
        if source in {'SITEMAP', 'STATIC', 'RENDERED', 'API', 'STRUCTURED_DATA', 'HTTP_LINK', 'FEED', 'FILTER_GET', 'PAGINATION'} and not self.config.discovers(source):
            return False
        try:
            from crawler_coverage_auditor.validation.network_safety import validate_target
            validate_target(url)
            normalized = normalize_url(url)
            page_type = resource_hint or resource_type(normalized)
            if urlsplit(normalized).scheme not in {"http", "https"} or (page_type not in URL_ONLY_TYPES and not same_scope(normalized, self.config.seed_hosts, self.config.include_subdomains)):
                return False
            if page_type in URL_ONLY_TYPES and not self.config.external_resources and not same_scope(normalized, self.config.seed_hosts, self.config.include_subdomains):
                return False
        except ValueError:
            return False
        captured = self.config.captures(page_type, normalized)
        if page_type in URL_ONLY_TYPES and not captured:
            return False
        reason = trap_reason(normalized)
        if reason:
            self._limit("Trap prevention", normalized, reason)
            return False
        existing = self.db.connection.execute('SELECT 1 FROM urls WHERE audit_run_id=? AND normalized_url=?', (self.run_id, normalized)).fetchone()
        if self._url_count >= self.config.max_urls and not existing:
            self._limit("Inventory", self.config.target_url, f"Inventory safeguard ({self.config.max_urls} URLs) reached; additional content may exist")
            return False
        inserted = self.db.add_url(self.run_id, DiscoveredURL(url, normalized, method, depth, parent, anchor, page_type, captured))
        if resource_hint and inserted:
            self.db.set_page_type(self.run_id, normalized, page_type)
        if page_type == "DOCUMENT":
            self.db.add_document(self.run_id, normalized, parent, Path(urlsplit(normalized).path).suffix.lstrip("."))
        if inserted:
            self._url_count += 1
            if captured:
                self._event("DISCOVERED", f"Discovered via {method.lower().replace('_', ' ')}", normalized, depth, parent_url=parent)
        return inserted

    def _record_resource_url(self, row: dict, page_type: str, *, content_type: str | None = None,
                             final_url: str | None = None) -> None:
        """Record a link, not evidence that a file exists or its contents were captured."""
        normalized = row['normalized_url']
        self.db.set_page_type(self.run_id, normalized, page_type)
        captured = self.config.captures(page_type, normalized, content_type or '')
        self.db.set_capture_enabled(self.run_id, normalized, captured)
        self.db.add_http_result(self.run_id, normalized, status_code=None, final_url=final_url,
            content_type=content_type, title=None, h1=None, text_length=None, validity='URL_ONLY', error=None)
        self.db.set_url_state(self.run_id, normalized, 'DONE')
        if page_type == 'DOCUMENT' and captured:
            self.db.add_document(self.run_id, normalized, row.get('parent_url'), Path(urlsplit(normalized).path).suffix.lstrip('.'))
        if captured:
            self._event('RESOURCE_URL', 'Resource URL collected only; file content and text are not fetched', normalized, row['min_depth'])

    async def _crawl_static(self, client: PoliteHttpClient) -> None:
        if self._corpus_api:
            await self._drain_pagination(client)
        while not self.stopped():
            pending = self.db.pending_urls(self.run_id, limit=1)
            if not pending:
                return
            row = dict(pending[0])
            self._phase('Discovering website URLs')
            await self._process_url(client, row)
            if self._browser and not self.stopped():
                await self._crawl_browser(self._browser, client, only_url=row['normalized_url'])
            if self.stopped():
                # A partial render is not a completed page snapshot. Revisit this
                # one interrupted URL on resume before checking its final text.
                self.db.set_url_state(self.run_id, row['normalized_url'], 'PENDING')
                return
            if self._corpus_api and not self.stopped():
                if await self._verify_all(api=self._corpus_api, only_url=row['normalized_url']) == 'CORPUS_PAUSED':
                    raise CorpusUnavailable('Corpus unavailable; current URL is saved and discovery has paused')
                await self._drain_pagination(client)

    async def _drain_pagination(self, client):
        while not self.stopped():
            task = self.db.connection.execute("""SELECT t.* FROM pagination_tasks t WHERE t.audit_run_id=?
                AND NOT EXISTS(SELECT 1 FROM urls u LEFT JOIN http_results h
                    ON h.audit_run_id=u.audit_run_id AND h.normalized_url=u.normalized_url
                    WHERE u.audit_run_id=t.audit_run_id AND (u.normalized_url=t.source_url OR h.final_url=t.source_url)
                    AND u.state!='DONE') LIMIT 1""", (self.run_id,)).fetchone()
            if not task:
                return
            # Persist only discovered links/kinds, not raw DOM or page bodies.
            html = ''.join(f'<a href="{escape(hint["url"], quote=True)}">{"Next" if hint["kind"] == "NEXT" else "Page"}</a>' for hint in json.loads(task['hints_json']))
            self._pagination_probing = True
            try:
                await self._analyze_pagination(client, html, task['source_url'], task['depth'])
                if not self.stopped():
                    self.db.connection.execute('DELETE FROM pagination_tasks WHERE audit_run_id=? AND source_url=?', (self.run_id, task['source_url']))
                    self.db.connection.commit()
            finally:
                self._pagination_probing = False

    async def _process_url(self, client: PoliteHttpClient, row: dict, *, response=None) -> None:
        url, normalized = row["url"], row["normalized_url"]
        if row.get('page_type') in URL_ONLY_TYPES:
            self._record_resource_url(row, row['page_type'])
            return
        depth = row.get("navigation_depth") if row.get("navigation_depth") is not None else int(row["min_depth"])
        if urlsplit(url).netloc in self._paused_hosts:
            self.db.add_http_result(self.run_id, normalized, status_code=0, final_url=url, content_type=None, title=None, h1=None,
                                    text_length=0, validity="SITE_PAUSED", error="Site paused after repeated rate limiting")
            self.db.set_url_state(self.run_id, normalized, "DONE")
            return
        self.db.set_url_state(self.run_id, normalized, "FETCHING")
        self._event("CHECKING", "Fetching website content", normalized, depth)
        response = response if response is not None else await client.get(url, referer=row.get("parent_url"))
        mime = response.headers.get("content-type", "").split(";", 1)[0]
        page_type = resource_type(normalized, mime)
        if response.url_only:
            if page_type not in URL_ONLY_TYPES:
                page_type = 'DOCUMENT'
            self._record_resource_url(row, page_type, content_type=mime, final_url=response.final_url)
            return
        if ('xml' in mime or page_type == 'FEED') and feed_links(response.text, response.final_url):
            page_type = 'FEED'
        self.db.set_page_type(self.run_id, normalized, page_type)
        captured = self.config.captures(page_type, normalized, mime)
        self.db.set_capture_enabled(self.run_id, normalized, captured)
        text = title = h1 = None
        access = detect_access(response.status_code, response.headers, response.text, response.final_url) if page_type not in {"DOCUMENT", "IMAGE"} or response.status_code != 200 else None
        if response.error:
            validity = "ROBOTS_RESTRICTED" if "Robots restriction" in response.error else "FETCH_ERROR"
        elif access:
            validity = access.status
            if validity == "RATE_LIMITED":
                self._paused_hosts.add(urlsplit(url).netloc)
            self._event(validity, access.reason, normalized, depth, severity="WARNING", provider=access.provider)
        elif response.status_code in {401, 403, 429}:
            validity = {401: "LOGIN_REQUIRED", 403: "BLOCKED", 429: "RATE_LIMITED"}[response.status_code]
            if response.status_code == 429:
                self._paused_hosts.add(urlsplit(url).netloc)
        elif page_type in URL_ONLY_TYPES:
            validity = 'HTTP_ERROR'
        elif page_type == "API":
            import json
            try:
                text = json.dumps(json.loads(response.text), ensure_ascii=False)
                validity = "VALID_PAGE" if response.status_code == 200 else "HTTP_ERROR"
            except ValueError:
                validity = "INVALID_RESPONSE"
        else:
            text, title, h1 = extract_text(response.text)
            validity = assess_page_validity(response.status_code, text, item_count=count_listing_items(response.text)).status
            lowered = f"{title or ''} {text or ''}".lower()
            soup = BeautifulSoup(response.text, "lxml")
            if len(lowered) < 400 and any(value in lowered for value in ("enable javascript", "requires javascript")):
                validity = "RENDER_REQUIRED"
            elif validity == "VALID_PAGE" and not (text or "").strip():
                validity = "EMPTY_PAGE"
            if urlsplit(normalized).fragment:
                validity = "RENDER_REQUIRED"
            pairs = parse_qsl(urlsplit(normalized).query)
            page_keys = {"page", "paged", "p", "pg", "pagenumber", "offset", "start", "cursor", "cursormark"}
            if validity == "VALID_PAGE" and any(key.lower() in page_keys for key, _ in pairs):
                parts = urlsplit(normalized)
                family = (parts.netloc, parts.path, tuple((key, value) for key, value in pairs if key.lower() not in page_keys))
                fingerprints = self._content_seen.setdefault(family, {})
                fingerprint = sha256_text(text or "")
                if fingerprint in fingerprints and fingerprints[fingerprint] != normalized:
                    validity = "REPEATED_PAGE"
                    self._limit("Trap prevention", normalized, f"Pagination content repeats {fingerprints[fingerprint]}; continuation not followed")
                else:
                    fingerprints[fingerprint] = normalized
        self.db.add_http_result(self.run_id, normalized, status_code=response.status_code,
            final_url=normalize_url(response.final_url), content_type=mime, title=title, h1=h1,
            text_length=len(text or ""), fingerprint=sha256_text(text or "") if text else None,
            validity=validity, error=response.error)
        if page_type == 'HTML' and captured:
            self.db.add_page_snapshot(self.run_id, normalized, text or "")
        self.db.set_url_state(self.run_id, normalized, "DONE")
        self._event("VALIDATED", f"Website: {validity.replace('_', ' ').lower()} — HTTP {response.status_code}", normalized, depth, http_status=response.status_code)
        if validity != "VALID_PAGE":
            reason = access.reason if access else response.error or title or ''
            self.db.add_error(self.run_id, "Website", normalized, f"{validity}: HTTP {response.status_code}; {reason}")
            will_render = self.config.use_browser and page_type == "HTML" and validity in {"BLOCKED", "BOT_BLOCKED", "CAPTCHA_REQUIRED"}
            if not will_render and (response.error or validity in {"LOGIN_REQUIRED", "CAPTCHA_REQUIRED", "GEO_BLOCKED", "BLOCKED", "RATE_LIMITED", "BOT_BLOCKED"} or response.status_code >= 500):
                self._limit("Website", normalized, f"{validity}: HTTP {response.status_code}; {reason}")
            return
        self._last_successful = normalized
        if page_type == "HTML":
            soup = BeautifulSoup(response.text, "lxml")
            canonical = soup.select_one("link[rel=canonical][href]")
            self.db.set_canonical(self.run_id, normalized, normalize_url(urljoin(response.final_url, canonical["href"])) if canonical else None)
        if page_type in {"DOCUMENT", "IMAGE"}:
            return
        links = json_links(response.text, response.final_url) if page_type == "API" else extract_links(response.text, response.final_url)
        if not self.config.incremental:
            for link in links:
                self._add_discovery(link.url, "API_DISCOVERY" if page_type == "API" else "STATIC", depth + 1, normalized, link.anchor_text, link_resource_hint(link))
            for link in embedded_links(response.text, response.final_url) if page_type == 'HTML' else []:
                self._add_discovery(link.url, 'STRUCTURED_DATA', depth + 1, normalized, link.anchor_text)
            for link in feed_links(response.text, response.final_url) if page_type == 'FEED' else []:
                self._add_discovery(link.url, 'FEED', depth + 1, normalized, link.anchor_text)
            for link in header_links(response.headers.get('link', ''), response.final_url):
                self._add_discovery(link.url, 'HTTP_LINK', depth + 1, normalized, link.anchor_text)
        if page_type == "HTML":
            self._analyze_static_filters(response.text, response.final_url, depth)
            await self._analyze_pagination(client, response.text, response.final_url, depth)
        elif page_type == "API":
            for parameter, value in parse_qsl(urlsplit(normalized).query):
                if parameter.lower() in {"page", "offset", "start", "cursor", "cursormark"}:
                    key = pagination_series_key(normalized, parameter)
                    self.db.upsert_pagination_series(self.run_id, key, normalized, parameter, row.get("parent_url") or normalized,
                        int(value) if value.isdigit() else 1, status="OBSERVED", evidence="API pagination followed via response-supplied links/cursors; actual maximum not proven")
                    self._event("API_PAGINATION", f"Following observed API pagination: {parameter}", normalized, depth)

    def _analyze_static_filters(self, html: str, page_url: str, depth: int) -> None:
        if not self.config.discovers('FILTER_GET'):
            return
        controls = detect_filters(html, page_url)
        for control in controls:
            source = normalize_url(page_url)
            self.db.add_filter_definition(self.run_id, source, control.name, control.control_type, control.form_action, control.form_method, control.selector_hint)
            self._event("FILTER_DETECTED", f"Filter detected: {control.name} ({control.form_method}); {len(control.values)} values", source, depth)
            for value in control.values:
                self.db.add_filter_value(self.run_id, source, control.name, value.display_value, value.request_value)
            if len(control.values) > self.config.max_filter_values:
                self._limit("Filters", source, f"Filter {control.name} exceeds {self.config.max_filter_values} exploration values; remaining values recorded but not applied")
            if control.form_method != "get":
                continue
            for value, filter_url in zip(control.values[:self.config.max_filter_values], get_filter_urls(control, page_url)):
                if self._add_discovery(filter_url, "FILTER_GET", depth + 1, source, f"{control.name}={value.request_value}"):
                    self._event("FILTER_APPLYING", f"Applying {control.name}: {value.display_value}", filter_url, depth + 1)
        if len(controls) > 1:
            self._limit("Filters", normalize_url(page_url), "Multiple filter controls: discovered values and current URL dependencies explored; exhaustive combinations are not proven")

    async def _crawl_browser(self, browser: BrowserExplorer, client: PoliteHttpClient, *, only_url=None) -> bool:
        candidates = [dict(row) for row in self.db.report_rows(self.run_id, include_traversal=True, only_url=only_url) if row["page_type"] == "HTML"
                      and row["validity"] in {"VALID_PAGE", "EMPTY_PAGE", "RENDER_REQUIRED", "BLOCKED", "BOT_BLOCKED", "CAPTCHA_REQUIRED"} and row["normalized_url"] not in self._browser_seen
                      and (only_url is None or row['normalized_url'] == only_url)]
        if not candidates:
            return False
        for row in candidates:
            if self.stopped():
                return False
            if len(self._browser_seen) >= self.config.max_browser_pages:
                self._limit("Browser", self.config.target_url, f"Rendered page safeguard ({self.config.max_browser_pages}) reached; {len(candidates)} candidates may remain")
                return False
            self._browser_seen.add(row["normalized_url"])
            if not await self._allowed(row["url"]) or urlsplit(row["url"]).netloc in self._paused_hosts:
                continue
            cookies = client.browser_cookies(row['url'])
            if cookies:
                await browser.context.add_cookies(cookies)
            self._browser_depth = row['navigation_depth'] if row.get('navigation_depth') is not None else row['min_depth']
            self.db.set_url_state(self.run_id, row['normalized_url'], 'RENDERING')
            result = await browser.explore(row["url"])
            self.db.set_url_state(self.run_id, row['normalized_url'], 'DONE')
            for diagnostic in result.script_errors:
                self._event('JAVASCRIPT_DIAGNOSTIC', f"Website script warning: {diagnostic['message']}. Recorded as a diagnostic, not proof that this page is unavailable.", diagnostic['url'], self._browser_depth,
                            script_stack=diagnostic['stack'], page_content_available=bool(result.html and not result.error and extract_text(result.html)[0].strip()))
            if not result.error and result.http_status == 200:
                cookies = await browser.context.cookies([row['url']])
                identity_page = await browser.context.new_page()
                try:
                    identity = await identity_page.evaluate("navigator.userAgent")
                finally:
                    await identity_page.close()
                client.use_browser_session(row['url'], cookies, identity)
                self._event("SESSION_REUSED", "Browser cookies shared with website requests; protected links/resources can use the same public session", row['url'])
            if result.http_status == 429:
                self._paused_hosts.add(urlsplit(row['url']).netloc)
            if result.error:
                self.db.add_error(self.run_id, "Browser", row["url"], result.error)
                self._limit("Browser", row["url"], result.error)
            for reason in result.limitations:
                self._limit("Browser", row["url"], reason)
            for request in result.network:
                self.db.add_network_request(self.run_id, row["normalized_url"], request.url, request.method, request.resource_type, request.status_code)
                if request.status_code == 429:
                    self._paused_hosts.add(urlsplit(request.url).netloc)
                if not result.error and request.method == "GET" and request.resource_type in {"xhr", "fetch"}:
                    self._add_discovery(request.url, "API_DISCOVERY", row["min_depth"] + 1, row["normalized_url"], "Observed browser API request")
            if result.error:
                continue
            for link in result.links:
                self._add_discovery(link.url, "API_DISCOVERY" if link.tag == "api" else "JS_DISCOVERY", row["min_depth"] + 1, row["normalized_url"], link.anchor_text, link_resource_hint(link))
            for state in result.states:
                if state.get('kind') == 'content_tab':
                    await self._analyze_pagination(client, state['html'], state['state_url'], row['min_depth'] + 1)
                    continue
                self.db.add_filter_definition(self.run_id, state["source_url"], state["control"], "rendered", state["state_url"], state["method"], None)
                self.db.add_filter_value(self.run_id, state["source_url"], state["control"], state["label"], state["value"])
                await self._analyze_pagination(client, state["html"], state["state_url"], row["min_depth"] + 1)
            if result.html and not result.error:
                text, title, h1 = extract_text(result.html)
                self._analyze_static_filters(result.html, result.final_url or row["url"], row["min_depth"])
                await self._analyze_pagination(client, result.html, result.final_url or row["url"], row["min_depth"])
                if text.strip():
                    if self.config.captures('HTML', row['normalized_url']):
                        self.db.add_page_snapshot(self.run_id, row["normalized_url"], text)
                    if result.http_status == 200:
                        validity = assess_page_validity(result.http_status, text).status
                        self.db.add_http_result(self.run_id, row["normalized_url"], status_code=result.http_status, final_url=result.final_url,
                            content_type="text/html", title=title, h1=h1, text_length=len(text), fingerprint=sha256_text(text), validity=validity, error=None)
                        if validity == 'VALID_PAGE':
                            self._last_successful = row['normalized_url']
                            self._event("BROWSER_RECOVERED", "Public website content validated after browser/session recovery", row['url'])
        return True

    def _browser_event(self, kind: str, message: str, url: str | None = None, **details) -> None:
        self._event(kind, message, url, getattr(self, '_browser_depth', None), **details)

    async def _analyze_pagination(self, client: PoliteHttpClient, html: str, page_url: str, depth: int) -> None:
        if not self.config.discovers('PAGINATION'):
            return
        if self._corpus_api and (not self._pagination_probing or self._processing_probe):
            hints = [{'url': hint.url, 'kind': hint.kind} for hint in detect_pagination(html, page_url) if hint.url]
            if hints:
                old = self.db.connection.execute('SELECT hints_json FROM pagination_tasks WHERE audit_run_id=? AND source_url=?', (self.run_id, page_url)).fetchone()
                merged = {(hint['url'], hint['kind']): hint for hint in (json.loads(old[0]) if old else []) + hints}
                self.db.connection.execute('INSERT OR REPLACE INTO pagination_tasks VALUES(?,?,?,?)', (self.run_id, page_url, depth, json.dumps(list(merged.values()))))
                self.db.connection.commit()
            return
        hints = detect_pagination(html, page_url)
        for hint in hints:
            if not hint.url or self.stopped():
                continue
            candidate = normalize_url(hint.url, page_url)
            if not same_scope(candidate, self.config.seed_hosts, self.config.include_subdomains):
                continue
            parameter_and_value = numeric_page_parameter(candidate)
            if not parameter_and_value:
                for name, value in parse_qsl(urlsplit(candidate).query):
                    if name.lower() in {"offset", "start", "cursor", "cursormark"}:
                        self.db.upsert_pagination_series(self.run_id, pagination_series_key(candidate, name), candidate, name,
                            page_url, int(value) if value.isdigit() else 1, status="OBSERVED", evidence="Observed next/offset/cursor URLs followed; no verified numeric boundary")
                if hint.kind == "NEXT":
                    self._add_discovery(candidate, "PAGINATION", depth + 1, normalize_url(page_url), "Observed next/offset/cursor link")
                continue
            parameter, observed_page = parameter_and_value
            series_key = pagination_series_key(candidate, parameter)
            self.db.upsert_pagination_series(self.run_id, series_key, candidate, parameter, page_url, observed_page)
            if series_key in self._pagination_series_seen:
                continue
            self._pagination_series_seen.add(series_key)
            self._event("PAGINATION_DETECTED", f"Pagination detected: {parameter}; finding maximum valid page", page_url, depth)
            fingerprints: dict[str, int] = {}
            observed_valid = observed_page
            probe_responses = {}
            async def fetch_probe(probe_url):
                if probe_url in probe_responses:
                    return probe_responses[probe_url]
                response = await client.get(probe_url)
                probe_responses[probe_url] = response
                if self._corpus_api:
                    normalized = normalize_url(probe_url)
                    self._add_discovery(probe_url, 'PAGINATION', depth + 1, normalize_url(page_url), 'Numeric boundary probe')
                    rows = self.db.connection.execute('SELECT * FROM urls WHERE audit_run_id=? AND normalized_url=?', (self.run_id, normalized)).fetchall()
                    if not rows:
                        raise PaginationUnverified('Inventory safeguard prevents completing this pagination probe')
                    if rows[0]['state'] == 'DONE':
                        return response
                    self._processing_probe = True
                    try:
                        await self._process_url(client, dict(rows[0]), response=response)
                        if self._browser and not self.stopped():
                            await self._crawl_browser(self._browser, client, only_url=normalized)
                        if self.stopped():
                            self.db.set_url_state(self.run_id, normalized, 'PENDING')
                            raise CorpusUnavailable('Stopped during pagination exploration')
                        if await self._verify_all(api=self._corpus_api, only_url=normalized) == 'CORPUS_PAUSED':
                            raise CorpusUnavailable('Corpus unavailable while verifying a pagination probe')
                    finally:
                        self._processing_probe = False
                return response
            async def is_valid(page: int) -> bool:
                nonlocal observed_valid
                if self.stopped():
                    raise PaginationUnverified("Audit stopped while finding pagination boundary")
                probe_url = replace_page_parameter(candidate, parameter, page)
                self._event("PAGINATION_CHECKING", f"Checking pagination page {page}", probe_url, depth + 1)
                response = await fetch_probe(probe_url)
                if response.error or response.status_code in {401, 403, 429} or response.status_code >= 500:
                    self.db.add_pagination_page(self.run_id, series_key, page, normalize_url(probe_url), 'PROBE_BLOCKED', None)
                    raise PaginationUnverified(f"Boundary blocked at page {page}: HTTP {response.status_code}; {response.error or ''}")
                text, _, _ = extract_text(response.text)
                if response.status_code == 200 and not text.strip():
                    self.db.add_pagination_page(self.run_id, series_key, page, normalize_url(probe_url), 'RENDER_REQUIRED', None)
                    raise PaginationUnverified(f'No readable content at pagination page {page}; maximum cannot be established')
                fingerprint = sha256_text(text)
                repeated = fingerprint in fingerprints and fingerprints[fingerprint] != page
                if not repeated and page > 1:
                    previous = await fetch_probe(replace_page_parameter(candidate, parameter, page - 1))
                    if previous.error or previous.status_code in {401, 403, 429} or previous.status_code >= 500:
                        raise PaginationUnverified(f"Adjacent boundary probe failed at page {page - 1}: HTTP {previous.status_code}")
                    previous_text = extract_text(previous.text)[0]
                    current_valid = assess_page_validity(response.status_code, text, item_count=count_listing_items(response.text)).status == 'VALID_PAGE'
                    previous_valid = assess_page_validity(previous.status_code, previous_text, item_count=count_listing_items(previous.text)).status == 'VALID_PAGE'
                    if current_valid and not previous_valid:
                        self.db.add_pagination_page(self.run_id, series_key, page - 1, normalize_url(previous.final_url), 'NON_MONOTONIC', sha256_text(previous_text))
                        raise PaginationUnverified(f'Non-monotonic sequence: page {page} is valid but adjacent page {page - 1} is not; maximum remains unknown')
                    if previous.status_code == 200:
                        repeated = sha256_text(previous_text) == fingerprint
                if detect_access(response.status_code, response.headers, response.text, response.final_url):
                    self.db.add_pagination_page(self.run_id, series_key, page, normalize_url(probe_url), 'ACCESS_RESTRICTED', fingerprint)
                    raise PaginationUnverified(f"Bot/access restriction during page {page} probe")
                result = assess_page_validity(response.status_code, text, item_count=count_listing_items(response.text), repeats_known_page=repeated)
                self.db.add_pagination_page(self.run_id, series_key, page, normalize_url(response.final_url), result.status, fingerprint)
                if repeated and assess_page_validity(response.status_code, text, item_count=count_listing_items(response.text)).status == 'VALID_PAGE':
                    raise PaginationUnverified(f'Repeated content at page {page}; maximum is ambiguous, not verified')
                if result.status == "VALID_PAGE" and text.strip():
                    observed_valid = max(observed_valid, page)
                    fingerprints[fingerprint] = page
                    self._last_successful = probe_url
                    return True
                return False
            try:
                maximum = await find_maximum_page(is_valid, start=max(1, observed_page), maximum_probe=self.config.maximum_pagination_probe)
            except PaginationUnverified as error:
                self.db.upsert_pagination_series(self.run_id, series_key, candidate, parameter, page_url, observed_valid, status="LIMITED", evidence=str(error))
                self._limit("Pagination", candidate, str(error))
                continue
            if maximum is None:
                self.db.upsert_pagination_series(self.run_id, series_key, candidate, parameter, page_url, observed_page, status="UNVERIFIED", evidence=f"Observed page {observed_page} did not validate; boundary unknown")
                self._limit("Pagination", candidate, f"Observed page {observed_page} did not validate; maximum remains unknown")
                continue
            evidence = f"Exponential probes and binary search found page {maximum}; assumes a contiguous monotonic page sequence"
            self.db.upsert_pagination_series(self.run_id, series_key, candidate, parameter, page_url, observed_valid, max_page=maximum, status="VERIFIED", evidence=evidence)
            self._event("PAGINATION_MAX", f"Maximum valid page: {maximum}", candidate, depth, maximum=maximum)
            for page in range(1, min(maximum, self.config.max_urls) + 1):
                if self.stopped():
                    break
                self._add_discovery(replace_page_parameter(candidate, parameter, page), "PAGINATION", depth + 1, normalize_url(page_url), f"{parameter}={page}")
            if maximum > self.config.max_urls:
                self._limit("Pagination", candidate, "Verified pagination exceeds inventory safeguard; remaining pages not all visited")


    def _write_reports(self) -> dict:
        rows = [dict(row) for row in self.db.report_rows(self.run_id)]
        summary = coverage_summary(rows)
        start_time = self.db.connection.execute("SELECT started_at FROM audit_runs WHERE id=?", (self.run_id,)).fetchone()[0]
        summary.update({"audit_run_id": self.run_id, "target": self.config.target_url, "started_at": start_time,
                        "audit_status": self._report_status,
                        'corpus_provider': 'coverage_api', 'text_comparison_supported': True,
                        'text_comparison_note': 'HTML text is compared via /v1/text using estimated word retention and beginning/middle/ending checks. Snapshot dates can differ; scores are not exact semantic completeness. All files remain URL-only.',
                        "discovery_limited": bool(self._limited), "limitations": len(self._limited)})
        limits = [dict(row) for row in self.db.connection.execute("SELECT stage,url,reason,last_successful_url,created_at FROM audit_limits WHERE audit_run_id=?", (self.run_id,))]
        errors = [dict(row) for row in self.db.connection.execute("SELECT stage,url,message,created_at FROM errors WHERE audit_run_id=?", (self.run_id,))]
        events = [dict(row) for row in self.db.connection.execute("SELECT created_at,stage,kind,severity,message,url,depth FROM audit_events WHERE audit_run_id=? ORDER BY id DESC LIMIT 200", (self.run_id,))]
        depth, issues = coverage_by_depth(rows), root_causes(rows)
        pagination_rows = [dict(row) for row in self.db.pagination_rows(self.run_id)]
        filter_rows = [dict(row) for row in self.db.filter_rows(self.run_id)]
        document_rows = [dict(row) for row in self.db.document_rows(self.run_id)]
        destination = Path(self.config.output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        write_excel(destination / "coverage_report.xlsx", summary, rows, depth, issues, pagination_rows, filter_rows, document_rows, archive_coverage(rows), sitemap_coverage(rows), js_only_rows(rows), limits=limits, events=events, errors=errors)
        write_html(destination / "coverage_report.html", summary, rows, issues, depth_rows=depth, pagination_rows=pagination_rows, filter_rows=filter_rows, document_rows=document_rows, archive_rows=archive_coverage(rows), sitemap_rows=sitemap_coverage(rows), js_rows=js_only_rows(rows), limits=limits, events=events, errors=errors)
        missing = missing_rows(rows)
        with (destination / "missing_from_corpus.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(missing[0]) if missing else ["url", "status"])
            writer.writeheader()
            writer.writerows({key: "'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')) else value for key, value in row.items()} for row in missing)
        return summary

    def _write_regression(self, baseline_run: int) -> dict[str, int]:
        baseline = [dict(row) for row in self.db.report_rows(baseline_run)]
        summary, changes = compare_runs(baseline, [dict(row) for row in self.db.report_rows(self.run_id)])
        with (Path(self.config.output_dir) / "regression_changes.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["url", "change"])
            writer.writeheader()
            writer.writerows(changes)
        return summary

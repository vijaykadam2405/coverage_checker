from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlsplit

from crawler_coverage_auditor.corpus.credentials import API_URL

CAPTURE_TYPES = frozenset({'HTML', 'PDF', 'DOCUMENT', 'IMAGE', 'API', 'FEED', 'MEDIA', 'OTHER'})
DISCOVERY_SOURCES = frozenset({'SITEMAP', 'STATIC', 'RENDERED', 'API', 'STRUCTURED_DATA',
                              'HTTP_LINK', 'FEED', 'FILTER_GET', 'FILTER_BROWSER', 'PAGINATION', 'TABS', 'LOAD_MORE',
                              'JS_SOURCE', 'METADATA', 'PUBLIC_API', 'RESOURCESYNC', 'EXTERNAL_INDEX'})
DEFAULT_DISCOVERY_SOURCES = DISCOVERY_SOURCES - {'EXTERNAL_INDEX'}
AUTOMATIC_DISCOVERY_SOURCES = DISCOVERY_SOURCES
DISCOVERY_ENGINE_VERSION = 3
LEGACY_DISCOVERY_SOURCES = DEFAULT_DISCOVERY_SOURCES - {'JS_SOURCE', 'METADATA', 'PUBLIC_API', 'RESOURCESYNC'}


def upgraded_discovery_sources(values):
    sources = set(values.get('discovery_sources', LEGACY_DISCOVERY_SOURCES))
    if values.get('discovery_version', 1) < 2 and sources == LEGACY_DISCOVERY_SOURCES:
        sources = set(DEFAULT_DISCOVERY_SOURCES)
    return sources


def automatic_scan_settings(values: dict) -> dict:
    """Normal scans run all supported discovery, including live-validated Wayback.

    Capture choices and conservative pacing remain user-controlled/preserved.
    Diagnostic callers can still construct an explicit AuditConfig source subset.
    """
    return dict(values) | {
        'discovery_sources': sorted(AUTOMATIC_DISCOVERY_SOURCES),
        'discovery_version': DISCOVERY_ENGINE_VERSION,
        'use_browser': True, 'include_subdomains': True,
        'external_resources': False, 'reuse_public_session': True,
    }
LEGACY_DISCOVERY_LIMITS = ('max_urls', 'max_browser_pages', 'max_browser_actions',
                         'max_filter_values', 'maximum_pagination_probe')
WEBSITE_DELAY_SECONDS = 1.0
CORPUS_INTERVAL_SECONDS = 2.0
PACING_VERSION = 2


def paced_scan_settings(values: dict, *, upgrade_legacy: bool = False) -> dict:
    """Moderate serial pacing; migrate old defaults, not intentional slower settings."""
    result = dict(values)
    legacy = upgrade_legacy and result.get('pacing_version', 1) < PACING_VERSION
    for name, minimum, old_default in (
        ('website_delay_seconds', WEBSITE_DELAY_SECONDS, 1.5),
        ('corpus_interval_seconds', CORPUS_INTERVAL_SECONDS, 3.0),
    ):
        value = result.get(name, minimum)
        if legacy and value == old_default:
            value = minimum
        result[name] = max(minimum, value)
    result.update(pacing_version=PACING_VERSION, max_concurrency=1, corpus_batch_size=1)
    return result


def capture_category(page_type: str, url: str, content_type: str = '') -> str:
    """A single classification shared by capture controls and dashboard counts."""
    if page_type in {'HTML', 'IMAGE', 'API', 'FEED'}:
        return page_type
    if page_type == 'RESOURCE':
        return 'MEDIA'
    if page_type == 'DOCUMENT':
        path = urlsplit(url).path.lower()
        if 'application/pdf' in content_type.lower() or not content_type and path.endswith('.pdf'):
            return 'PDF'
        document_extensions = ('.doc', '.docx', '.docm', '.xls', '.xlsx', '.xlsm', '.xlsb',
                               '.ppt', '.pptx', '.pptm', '.csv', '.txt', '.rtf', '.odt', '.ods', '.odp', '.epub')
        document_mimes = ('officedocument', 'msword', 'opendocument', 'ms-powerpoint', 'ms-excel',
                          'spreadsheet', 'epub', 'text/plain', 'text/csv', 'rtf')
        return 'DOCUMENT' if path.endswith(document_extensions) or any(mime in content_type.lower() for mime in document_mimes) else 'OTHER'
    return 'OTHER'


def resource_subtype(page_type: str, url: str, content_type: str = '') -> str:
    """Describe evidence, not file availability; MIME wins over suffix."""
    from pathlib import PurePosixPath
    mime = content_type.split(';')[0].strip().lower()
    extension = PurePosixPath(urlsplit(url).path).suffix.lower()
    if mime:
        if mime in {'text/html', 'application/xhtml+xml'}:
            return 'HTML'
        for marker, label in [('pdf', 'PDF'), ('word', 'Word'), ('spreadsheet', 'Spreadsheet'),
                              ('excel', 'Spreadsheet'), ('presentation', 'Presentation'),
                              ('powerpoint', 'Presentation'), ('opendocument', 'OpenDocument'),
                              ('epub', 'eBook'), ('zip', 'Archive'), ('compressed', 'Archive'),
                              ('json', 'JSON API'), ('rss', 'RSS'), ('atom', 'Atom')]:
            if marker in mime:
                return label
        if mime in {'text/plain', 'text/csv', 'text/css', 'application/javascript', 'text/javascript'}:
            return {'text/plain': 'Text', 'text/csv': 'CSV', 'text/css': 'Stylesheet',
                    'application/javascript': 'Script', 'text/javascript': 'Script'}[mime]
        for prefix, label in [('video/', 'Video'), ('audio/', 'Audio'), ('font/', 'Font'), ('image/', 'Image')]:
            if mime.startswith(prefix):
                return label
        if mime == 'application/octet-stream':
            return 'Unknown binary'
    for extensions, label in [(('.pdf',), 'PDF'), (('.doc', '.docx', '.docm'), 'Word'),
        (('.xls', '.xlsx', '.xlsm', '.xlsb'), 'Spreadsheet'), (('.ppt', '.pptx', '.pptm'), 'Presentation'),
        (('.csv',), 'CSV'), (('.txt', '.rtf'), 'Text'), (('.odt', '.ods', '.odp'), 'OpenDocument'),
        (('.zip', '.7z', '.rar', '.gz', '.tar'), 'Archive'), (('.epub', '.mobi'), 'eBook'),
        (('.mp4', '.webm', '.mov', '.avi'), 'Video'), (('.mp3', '.wav', '.ogg'), 'Audio'),
        (('.woff', '.woff2', '.ttf', '.otf'), 'Font'), (('.css',), 'Stylesheet'), (('.js',), 'Script'),
        (('.rss',), 'RSS'), (('.atom',), 'Atom'), (('.json',), 'JSON API')]:
        if extension in extensions:
            return label
    return {'HTML': 'HTML', 'IMAGE': 'Image', 'API': 'API', 'FEED': 'Feed',
            'DOCUMENT': 'Other document', 'RESOURCE': 'Other asset'}.get(page_type, 'Unknown')


@dataclass(slots=True)
class AuditConfig:
    """Configuration for one auditable target website."""

    target_url: str
    corpus_url: str = API_URL
    corpus_provider: str = 'coverage_api'
    corpus_batch_size: int = 1
    include_subdomains: bool = False
    use_browser: bool = False
    max_concurrency: int = 1
    max_filter_values: int | None = None
    output_dir: Path = Path("reports")
    database_path: Path = Path("data/coverage.sqlite")
    resume: bool = False
    full_audit: bool = True
    incremental: bool = False
    compare_run: int | None = None
    request_timeout_seconds: float = 25.0
    max_retries: int = 3
    user_agent: str = "CrawlerCoverageAuditor/0.1 (+coverage audit)"
    # Retained only to read old saved configurations. Discovery has no count ceilings.
    max_urls: int | None = None
    max_browser_pages: int | None = None
    max_browser_actions: int | None = None
    respect_robots: bool = True
    discovery_only: bool = False
    website_delay_seconds: float = 0.0
    corpus_interval_seconds: float = CORPUS_INTERVAL_SECONDS
    pacing_version: int = PACING_VERSION
    corpus_timeout_seconds: float = 60.0
    corpus_cache_ttl_seconds: float = 86400.0
    browser_challenge_wait_seconds: float = 12.0
    seed_hosts: set[str] = field(default_factory=set)
    capture_types: set[str] = field(default_factory=lambda: set(CAPTURE_TYPES))
    discovery_sources: set[str] = field(default_factory=lambda: set(DEFAULT_DISCOVERY_SOURCES))
    discovery_version: int = DISCOVERY_ENGINE_VERSION
    # Kept for reading older configurations; target inventories exclude external hosts.
    external_resources: bool = False
    reuse_public_session: bool = True
    maximum_pagination_probe: int | None = None

    def __post_init__(self) -> None:
        self.external_resources = False
        # Resuming an old scan must not restore its previous discovery cutoffs.
        for name in LEGACY_DISCOVERY_LIMITS:
            setattr(self, name, None)
        self.capture_types = set(self.capture_types)
        self.discovery_sources = set(self.discovery_sources)
        if not self.discovery_sources <= DISCOVERY_SOURCES:
            raise ValueError('Unknown discovery source')
        if not self.capture_types or not self.capture_types <= CAPTURE_TYPES:
            raise ValueError('Select at least one valid capture type')
        parsed = urlparse(self.target_url)
        from crawler_coverage_auditor.validation.network_safety import validate_target
        validate_target(self.target_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("--url must be an absolute HTTP(S) URL")
        self.target_url = self.target_url if parsed.path else self.target_url.rstrip("/") + "/"
        self.corpus_url = self.corpus_url.rstrip("/") + "/"
        self.seed_hosts.add((parsed.hostname or "").lower())
        if (parsed.hostname or "").lower().startswith("www."):
            self.seed_hosts.add((parsed.hostname or "").lower().removeprefix("www."))
        self.max_concurrency = 1
        self.website_delay_seconds = max(0.0, self.website_delay_seconds)
        self.corpus_interval_seconds = max(0.0, self.corpus_interval_seconds)
        self.corpus_batch_size = 1
        if self.corpus_cache_ttl_seconds <= 0:
            raise ValueError('Corpus cache refresh interval must be positive')
        self.browser_challenge_wait_seconds = min(30.0, max(0.0, self.browser_challenge_wait_seconds))

    def captures(self, page_type: str, url: str, content_type: str = '') -> bool:
        return capture_category(page_type, url, content_type) in self.capture_types

    def discovers(self, source: str) -> bool:
        return source in self.discovery_sources

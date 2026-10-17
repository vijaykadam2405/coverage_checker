"""Wait for content work, not unrelated beacons, without shortening content grace."""
from time import monotonic
from urllib.parse import urlsplit

BACKGROUND_ENDPOINTS = {
    'google-analytics.com': frozenset({'/collect', '/g/collect', '/j/collect', '/mp/collect'}),
    'clarity.ms': frozenset({'/collect'}),
}
CONTENT_REQUEST_TYPES = frozenset({'document', 'script', 'stylesheet', 'xhr', 'fetch'})


class ContentReadiness:
    """Keep the 500ms content-network quiet window; ignore known third-party telemetry.

    Scripts/styles and all same-site or unknown API requests still count. Requests
    are never blocked here; BrowserExplorer observes their responses as before.
    """

    def __init__(self, target_url: str, *, clock=monotonic):
        self.target_host = (urlsplit(target_url).hostname or '').lower().removeprefix('www.')
        self.pending = set()
        self.clock = clock
        self.last_activity = clock()

    def _relevant(self, request) -> bool:
        if request.resource_type not in CONTENT_REQUEST_TYPES:
            return False
        parts = urlsplit(request.url)
        host = (parts.hostname or '').lower().removeprefix('www.')
        same_site = host == self.target_host or host.endswith('.' + self.target_host) or self.target_host.endswith('.' + host)
        telemetry = False
        for domain, paths in BACKGROUND_ENDPOINTS.items():
            if (host == domain or host.endswith('.' + domain)) and parts.path.rstrip('/') in paths:
                telemetry = True
                # If the provider is the target website, its own APIs are content.
                same_site = same_site or self.target_host == domain or self.target_host.endswith('.' + domain)
                break
        return request.resource_type not in {'xhr', 'fetch'} or same_site or not telemetry

    def started(self, request):
        if self._relevant(request):
            self.pending.add(request)
            self.last_activity = self.clock()

    def finished(self, request):
        if request in self.pending:
            self.pending.discard(request)
            self.last_activity = self.clock()

    async def wait(self, page, *, stopped=lambda: False, timeout=3.5) -> bool:
        started = self.clock()
        while not stopped():
            now = self.clock()
            # Preserve the 200ms onchange/scroll scheduling grace and the previous
            # networkidle 500ms quiet period for actual content requests.
            if now - started >= .2 and not self.pending and now - self.last_activity >= .5:
                return True
            if now - started >= timeout:
                return False
            await page.wait_for_timeout(50)
        return False

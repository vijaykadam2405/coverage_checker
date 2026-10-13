"""Harvest fetch — httpx fast-path, fixtures fallback, optional JS render (§4).

Responsibilities (Stage 1 HARVEST):
  * fetch a URL within the job's budget, honoring conditional GET (304) so an
    unchanged page on a daily sweep is skipped before download (§7A);
  * capture validators (ETag / Last-Modified) for next run;
  * detect content type (html / pdf / image / other);
  * optionally render JS via Playwright when ``render_js`` is set AND Playwright
    is installed — otherwise degrade gracefully to the httpx body.

Anti-bot ladders, proxy pools and captcha solving (present in the production
reference) are intentionally out of scope for the test build.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import config, errors, fixtures, interaction, stealth
from .canonicalize import canonicalize_url
from .models import InteractionConfig

# Shared pooled httpx clients (keyed by SSL-verify). Reusing TCP+TLS connections instead of a fresh
# handshake per request is the biggest per-fetch latency win on a network-bound crawl. httpx.Client
# is safe to share across the asyncio.to_thread workers that call _http_fetch.
import threading as _threading
_HTTP_CLIENTS: dict = {}
_HTTP_CLIENTS_LOCK = _threading.Lock()
log = logging.getLogger("fetcher")


def _shared_http_client(verify_ssl: bool):
    import httpx
    c = _HTTP_CLIENTS.get(verify_ssl)
    if c is None:
        with _HTTP_CLIENTS_LOCK:
            c = _HTTP_CLIENTS.get(verify_ssl)
            if c is None:
                limits = httpx.Limits(
                    max_connections=int(os.environ.get("CRAWLER_HTTP_MAX_CONNECTIONS", "200")),
                    max_keepalive_connections=int(os.environ.get("CRAWLER_HTTP_KEEPALIVE", "100")))
                c = httpx.Client(follow_redirects=True, verify=verify_ssl, limits=limits)
                _HTTP_CLIENTS[verify_ssl] = c
    return c


@dataclass
class FetchResult:
    url: str                                  # canonical
    final_url: str
    status: int | None
    content_type: str | None = None
    kind: str = "other"                       # html | pdf | image | other
    text_html: str | None = None             # decoded body when textual
    inner_text: str | None = None            # visible innerText after interactions ran
    body_bytes: bytes | None = None          # raw bytes (pdf/image)
    screenshot_png: bytes | None = None       # full-page PNG grabbed during the render (no 2nd browser)
    title: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False                # 304 — unchanged since last crawl
    published_hint: str | None = None         # from fixture meta, if any
    tier: int = 0                             # 0=httpx/fixture, 1=rendered
    fetched_at: str = ""
    error: str | None = None
    reason: str | None = None                 # errors.classify_failure output (typed category)
    retry_after_s: float | None = None        # parsed Retry-After (429/503 only)
    # Not None ONLY on a page fetched with TLS verification DISABLED by the per-host opt-in
    # (Job.tls_insecure_hosts). Holds the ORIGINAL certificate error from the verified attempt that
    # failed, so the untrusted fetch is never separable from the reason it was untrusted.
    # Deliberately its own field and NOT `error`: `error` is the failure channel, so
    # classify_failure would read "certificate" back out of it and re-label this SUCCESSFUL fetch
    # as an SSL failure, and several callers branch on `error` being set at all.
    insecure_tls: str | None = None
    link_header: str | None = None            # raw RFC 8288 `Link:`. The ONLY next-page pointer on
    # api.github.com and every wp-json endpoint: they answer with a JSON array that contains no
    # next link in the body at all, so without this the crawler paged such an API exactly once.
    from_fixture: bool = False
    js_incomplete: bool = False               # DOM was STILL growing when the settle cap hit, so
    # this capture is a snapshot of a page mid-build. Recorded rather than swallowed: a partial
    # render that looks identical to a finished one is how a 174KB page gets stored as 44KB.
    cookies: list | None = None                # cookies harvested from the render context —
    # replayed on protected asset downloads (Akamai wants the page session's cookies).
    extra_links: list | None = None            # links found on pagination pages 2+ (enqueued too)
    extra_pdf_links: list | None = None        # PDF/attachment links from pagination pages 2+
    #                                            (so every page's Download links download, not just last)

    # Captcha / solver provenance (populated by the CamoFox C3 path).
    captcha_type: str | None = None
    captcha_solved: bool = False
    captcha_solver: str | None = None
    captcha_solver_cost_usd: float | None = None
    proxy_country: str | None = None

    def is_html(self) -> bool:
        return self.kind == "html"


def _body_budget_s(timeout_s: float) -> float:
    """Total wall-clock allowed for reading ONE response body.

    Separate from the httpx timeout, which is per-I/O-operation: its read timeout measures the
    silence BETWEEN chunks, so a server trickling a byte every few seconds never trips it. Default
    is 4x the per-request timeout, floored at 60s, so an ordinary large PDF on a slow link still
    completes while a tarpit is cut loose."""
    override = os.environ.get("CRAWLER_BODY_BUDGET_S")
    if override:
        try:
            return float(override)
        except ValueError:
            pass
    return max(60.0, float(timeout_s) * 4)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _force_playwright() -> bool:
    return os.environ.get("CRAWLER_FORCE_PLAYWRIGHT", "0") == "1"


# Realistic browser UA used only to retry a 403 once — some basic WAF rules
# key off the UA string alone. This does not bypass Cloudflare JS challenges,
# IP-based blocks, or CAPTCHAs (out of scope; see module docstring).
_BROWSER_UA_FALLBACK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _with_www(url: str) -> str:
    """The `www.` variant of a canonical (www-stripped) URL — some sites' certs
    or DNS only validate one of the two hostnames."""
    p = urlsplit(url)
    if p.hostname and p.hostname.startswith("www."):
        return url
    from urllib.parse import urlunsplit
    netloc = "www." + p.netloc
    return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))


def _is_tls_or_dns_error(exc: Exception) -> bool:
    # Cover BOTH httpx (getaddrinfo / "name or service not known") AND Chromium
    # (net::ERR_NAME_NOT_RESOLVED / ERR_CERT_* / ERR_SSL_*) so the www-retry fires on
    # the render path too — many apex hosts (army.mil) have NO A record, only www. does.
    msg = str(exc).lower()
    return any(s in msg for s in ("certificate", "ssl", "getaddrinfo", "name or service not known",
                                  "nodename nor servname", "err_name_not_resolved",
                                  "err_cert", "err_ssl"))


def _classify_kind(content_type: str | None, url: str) -> str:
    ct = (content_type or "").lower()
    low = url.lower().split("?", 1)[0]
    if "pdf" in ct or low.endswith(".pdf"):
        return "pdf"
    if ct.startswith("image/") or low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
        return "image"
    if "html" in ct or "xml" in ct or low.endswith((".html", ".htm", "/")):
        return "html"
    if not ct and not low.rsplit("/", 1)[-1].count("."):
        return "html"           # extensionless path, no content-type -> assume page
    return "other"


def _curl_cffi_get(url: str, **kw):
    """curl_cffi GET with a real Chrome TLS/JA3 fingerprint, plus optional DNS-over-HTTPS.

    DoH is best-effort: when CRAWLER_DOH_TEMPLATE is set we route through a Session carrying
    CurlOpt.DOH_URL; if this curl_cffi build doesn't accept curl_options, we fall back to a plain
    impersonated GET (the TLS block is still bypassed — only the DNS lookup isn't over HTTPS)."""
    from curl_cffi import requests as cc
    doh = stealth.doh_url()
    if doh:
        try:
            from curl_cffi import CurlOpt
            return cc.Session(curl_options={CurlOpt.DOH_URL: doh}).get(url, **kw)
        except Exception:
            pass   # unsupported option/build → plain impersonated GET below (DNS not over HTTPS)
    return cc.get(url, **kw)


# -- dev-mode fetch cache (offline replay of the httpx path) ------------------
# ponytail: caches the httpx chokepoint ONLY — rendered (browser) pages aren't cached; the ceiling
# is HAR-level replay, out of scope. Lets extraction/gate changes re-run without re-fetching.
def _dev_cache_enabled() -> bool:
    return os.environ.get("CRAWLER_DEV_CACHE", "0") == "1"


def _dev_cache_base(canon: str):
    import hashlib
    import pathlib
    d = pathlib.Path(os.environ.get("CRAWLER_DEV_CACHE_DIR", str(config.DATA_DIR / "dev_cache")))
    d.mkdir(parents=True, exist_ok=True)
    return d / hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _dev_cache_get(canon: str) -> "FetchResult | None":
    import json
    base = _dev_cache_base(canon)
    meta_f = base.with_suffix(".json")
    body_f = base.with_suffix(".body")
    if not (meta_f.exists() and body_f.exists()):
        return None
    try:
        m = json.loads(meta_f.read_text(encoding="utf-8"))
        body = body_f.read_bytes()
        kind = m.get("kind", "html")
        enc = m.get("enc", "utf-8")
        return FetchResult(
            url=canon, final_url=m.get("final_url", canon), status=m.get("status"),
            content_type=m.get("content_type"), kind=kind,
            text_html=body.decode(enc, "ignore") if kind in ("html", "other") else None,
            body_bytes=body if kind in ("pdf", "image") else None,
            etag=m.get("etag"), last_modified=m.get("last_modified"), fetched_at=_now_iso())
    except Exception:
        return None


def _dev_cache_put(canon: str, r: "FetchResult") -> None:
    import json
    try:
        base = _dev_cache_base(canon)
        body = r.body_bytes if r.body_bytes is not None else (r.text_html or "").encode("utf-8")
        base.with_suffix(".body").write_bytes(body)
        base.with_suffix(".json").write_text(json.dumps({
            "final_url": r.final_url, "status": r.status, "content_type": r.content_type,
            "kind": r.kind, "etag": r.etag, "last_modified": r.last_modified, "enc": "utf-8",
        }), encoding="utf-8")
    except Exception:
        pass


class Fetcher:
    """One fetcher per crawl job. Carries the job's UA / timeout / delay."""

    def __init__(self, user_agent: str, timeout_s: int = 30, delay_s: float = 2.0,
                 max_retries: int = 2, render_js: bool = False,
                 respect_robots: bool = True,
                 prefer_fixtures: bool | None = None, allow_network: bool | None = None,
                 interaction_cfg: InteractionConfig | None = None,
                 screenshot_wanted: bool = False,
                 tls_insecure_hosts: list[str] | None = None):
        self.user_agent = user_agent
        # "Advanced -> Proceed anyway", per HOST. Empty (the default) means no fetch from this
        # Fetcher can ever run with verify=False. www-stripped so the operator lists the host they
        # typed even when the www-retry rewrites the request.
        self.tls_insecure_hosts = {
            h.strip().lower().strip(".")[4:] if h.strip().lower().strip(".").startswith("www.")
            else h.strip().lower().strip(".")
            for h in (tls_insecure_hosts or []) if h.strip()}
        self.timeout_s = timeout_s
        # Allow env var override for benchmarks: CRAWLER_CRAWL_DELAY=0 disables politeness
        self.delay_s = float(os.environ.get("CRAWLER_CRAWL_DELAY", str(delay_s)))
        # Same-page asset sub-fetches (images/PDFs on an already-fetched page)
        # get a lighter delay than page fetches — they're not new page loads on
        # the host's routes, just static files, so full politeness is overkill.
        self.asset_delay_s = float(os.environ.get("CRAWLER_ASSET_DELAY", "0.5"))
        # Grab the screenshot during the render pass instead of relaunching a
        # second browser per kept page (see _render_fetch / enrich_assets).
        self.screenshot_wanted = screenshot_wanted
        self.max_retries = max_retries
        self.render_js = render_js
        self.respect_robots = respect_robots
        self.prefer_fixtures = config.prefer_fixtures() if prefer_fixtures is None else prefer_fixtures
        self.allow_network = config.allow_network() if allow_network is None else allow_network
        self._last_fetch_ts = 0.0
        self._robots = None
        self._interaction_cfg = interaction_cfg
        self._shared_ctx = None   # (playwright_cm, browser, context, page) once opened
        if respect_robots:
            from .robots import RobotsCache
            self._robots = RobotsCache(user_agent, timeout_s=min(timeout_s, 10))

    # -- SPA click-through shared page ------------------------------------
    def open_shared_page(self) -> bool:
        """Open one Playwright browser/context/page kept alive across calls, for
        SPA click-through mode (§4). Returns False (no-op) if Playwright isn't
        installed or a shared page is already open. Caller is responsible for
        calling close_shared_page() when done."""
        if self._shared_ctx is not None:
            return True
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            return False
        try:
            pw_cm = sync_playwright().start()
            headless = os.environ.get("CRAWLER_HEADLESS", "1") != "0"
            slow_mo = int(os.environ.get("CRAWLER_SLOW_MO", "0"))
            browser = pw_cm.chromium.launch(
                headless=headless, slow_mo=slow_mo,
                args=["--no-sandbox", *stealth.stealth_launch_args(), *stealth.doh_launch_args()],
                ignore_default_args=stealth.stealth_ignore_default_args())
            ctx = browser.new_context(user_agent=self.user_agent,
                                      viewport={"width": 1920, "height": 1080})
            if stealth.stealth_enabled():
                ctx.add_init_script(stealth.STEALTH_INIT_JS)
            page = ctx.new_page()
            self._shared_ctx = (pw_cm, browser, ctx, page)
            return True
        except Exception:
            self._shared_ctx = None
            return False

    def close_shared_page(self) -> None:
        """Idempotent teardown of the shared browser opened by open_shared_page()."""
        if self._shared_ctx is None:
            return
        pw_cm, browser, ctx, page = self._shared_ctx
        for closer in (ctx.close, browser.close, pw_cm.stop):
            try:
                closer()
            except Exception:
                pass
        self._shared_ctx = None

    def _click_or_goto(self, canon: str, interaction_cfg: InteractionConfig | None = None) -> FetchResult | None:
        """Try to reach *canon* by clicking a matching <a href> on the shared live
        page instead of a cold navigation. Returns None (caller falls back to the
        ordinary render/httpx ladder) whenever the click path isn't viable or its
        result can't be verified — never fabricates a status."""
        if self._shared_ctx is None:
            return None
        _, _, _, page = self._shared_ctx
        rel_path = _extract_path(canon)
        try:
            link = (page.query_selector(f'a[href="{rel_path}"]')
                    or page.query_selector(f'a[href$="{rel_path}"]')
                    or page.query_selector(f'a[href="{canon}"]'))
            if not link:
                return None
            try:
                link.click(timeout=10000)
            except Exception:
                return None   # a failed click falls through to a real cold fetch,
                              # never a fabricated navigation (no pushState fallback here)
            page.wait_for_timeout(int(os.environ.get("CRAWLER_SLOW_MO", "0")) or 300)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            url_matches = urlsplit(page.url).path.rstrip("/") == rel_path.rstrip("/")
            if not url_matches:
                return None   # click didn't actually navigate to the target path

            if interaction_cfg and self._has_any_interaction(interaction_cfg):
                html, inner_text = interaction.run_interactions(page, interaction_cfg)
            else:
                html, inner_text = page.content(), None

            return FetchResult(url=canon, final_url=page.url, status=200,
                               content_type="text/html", kind="html", text_html=html,
                               inner_text=inner_text, tier=1, fetched_at=_now_iso())
        except Exception:
            return None

    # -- public ----------------------------------------------------------
    def fetch(self, url: str, conditional: dict | None = None, click_mode: bool = False) -> FetchResult:
        canon = canonicalize_url(url)

        if self.prefer_fixtures and fixtures.has(canon):
            return self._from_fixture(canon, conditional)

        if not self.allow_network:
            # Offline and no fixture -> a clean error (never a fabricated page).
            return FetchResult(url=canon, final_url=canon, status=None,
                               fetched_at=_now_iso(), error="offline_no_fixture",
                               reason=errors.OTHER)

        # robots.txt politeness — only for live (non-fixture) fetches.
        if self._robots is not None and not self._robots.allowed(canon):
            return FetchResult(url=canon, final_url=canon, status=None,
                               fetched_at=_now_iso(), error="blocked_by_robots",
                               reason=errors.ROBOTS)

        self._throttle()

        # When forced, skip httpx entirely — go straight to Playwright.
        # SPA sites return a 200 shell with no links to httpx, killing BFS.
        if _force_playwright():
            rendered = self._render_fetch(canon, self._interaction_cfg)
            if rendered is not None:
                return rendered

        res = self._http_fetch(canon, conditional)

        # A WAF/Cloudflare block page often comes back as 403 *with* an HTML
        # body — retry once with a realistic browser UA before giving up.
        # (Covers UA-based blocks only; see _BROWSER_UA_FALLBACK docstring.)
        if res.status == 403:
            retried = self._http_fetch(canon, conditional, user_agent=_BROWSER_UA_FALLBACK)
            if retried.status != 403:
                res = retried

        # 429/503 rate-limit: wait out Retry-After (capped) then one refetch. The sync driver
        # has no event loop to defer to, so this is a bounded blocking sleep. ponytail: capped
        # at 60s — a longer Retry-After surfaces as an error, retried next run via record_failure.
        if res.status in (429, 503):
            wait = min(res.retry_after_s or 30.0, 60.0)
            time.sleep(wait)
            retried = self._http_fetch(canon, conditional)
            if retried.status not in (429, 503):
                res = retried

        # If interaction is configured, always use Playwright for richer page content
        # (even if httpx returned valid HTML — interaction steps need a real browser).
        if self._interaction_cfg and self._has_any_interaction(self._interaction_cfg):
            rendered = self._render_fetch(canon, self._interaction_cfg)
            if rendered is not None:
                return rendered

        # A successfully-downloaded non-HTML file (PDF, image, other binary) is
        # already the content we want — never fall through to the JS renderer,
        # which would try to page.goto() it and fail on "Download is starting".
        got_binary_file = (res.status and res.status < 400 and res.body_bytes
                           and not res.is_html())
        # Any 4xx (not just 403) may be a bot-block that a real browser session
        # clears — WAFs return 401/403/429 and sometimes a masking 404/406. Give
        # them all one full Playwright pass with a browser UA before giving up.
        blocked_4xx = bool(res.status and 400 <= res.status < 500)
        if (res.error or not res.text_html or blocked_4xx) and self.render_js \
                and not got_binary_file:
            if click_mode and self._shared_ctx is not None:
                clicked = self._click_or_goto(canon, self._interaction_cfg)
                if clicked is not None:
                    return clicked
                # fall through to the ordinary cold-render ladder below
            fallback_ua = _BROWSER_UA_FALLBACK if blocked_4xx else None
            rendered = self._render_fetch(canon, user_agent_override=fallback_ua)
            still_blocked = (rendered is not None and rendered.status
                             and 400 <= rendered.status < 500)
            if rendered is not None and not (blocked_4xx and still_blocked):
                return rendered
            # The human-like render still couldn't clear the block. Keep the
            # original 4xx but retag it: this needs a different network path
            # (residential proxy / real headless session / source API), not
            # another UA retry. NEEDS_NETWORK_PATH makes these queryable.
            if blocked_4xx:
                res.reason = errors.NEEDS_NETWORK_PATH
        return res

    def fetch_asset(self, url: str, referer: str | None = None,
                    cookies: list | None = None) -> FetchResult:
        """Download a same-page asset (image / PDF) via httpx only. Uses the
        lighter asset delay and NEVER renders — assets are files, not pages, so
        the JS renderer would only try to page.goto() them and fail on
        "Download is starting". Same robots + fixture handling as fetch()."""
        self._throttle(self.asset_delay_s)
        return self._fetch_asset_core(url, referer, cookies)

    def _fetch_asset_core(self, url: str, referer: str | None = None,
                          cookies: list | None = None) -> FetchResult:
        """The un-throttled body of fetch_asset — fixture/robots handling + a
        plain httpx download, no page-load throttle. Used directly by
        fetch_assets(), where the bounded worker pool is the rate limit.

        On a 403/401 (WAF fingerprinting the plain-httpx TLS handshake — Akamai
        "Access Denied"), escalate: (1) curl_cffi with a Chrome TLS/JA3 fingerprint +
        Referer + the render's cookies, then (2) CamoFox in-context byte fetch. Most
        assets never touch the ladder; it's lazy on the 403 branch only."""
        canon = canonicalize_url(url)
        if self.prefer_fixtures and fixtures.has(canon):
            return self._from_fixture(canon, None)
        if not self.allow_network:
            return FetchResult(url=canon, final_url=canon, status=None,
                               fetched_at=_now_iso(), error="offline_no_fixture",
                               reason=errors.OTHER)
        if self._robots is not None and not self._robots.allowed(canon):
            return FetchResult(url=canon, final_url=canon, status=None,
                               fetched_at=_now_iso(), error="blocked_by_robots",
                               reason=errors.ROBOTS)
        res = self._http_fetch(canon, None)
        if res.status in (401, 403):
            escalated = self._asset_bypass(canon, referer, cookies)
            if escalated is not None:
                return escalated
        return res

    def _asset_bypass(self, url: str, referer: str | None,
                      cookies: list | None) -> FetchResult | None:
        """Two-tier bypass for a WAF-blocked asset. Returns a 200 FetchResult with
        body_bytes on success, or None (caller keeps the original 403)."""
        # Tier 1 — curl_cffi with a real Chrome fingerprint + page Referer/cookies.
        try:
            headers = {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
            if referer:
                headers["Referer"] = referer
            jar = {c["name"]: c["value"] for c in (cookies or []) if c.get("name")}
            r = _curl_cffi_get(url, impersonate="chrome", headers=headers, cookies=jar or None,
                               timeout=self.timeout_s, allow_redirects=True)
            if r.status_code < 400 and r.content:
                ct = r.headers.get("content-type")
                return FetchResult(url=url, final_url=str(r.url), status=r.status_code,
                                   content_type=ct, kind=_classify_kind(ct, url),
                                   body_bytes=r.content, tier=1, fetched_at=_now_iso())
        except Exception:
            pass
        # Tier 2 — CamoFox: fetch the bytes inside the stealth browser session (no-op if
        # CAMOFOX_ENABLED != 1). The last resort for assets even curl_cffi can't clear.
        try:
            from . import camofox_client
            data = camofox_client.fetch_bytes(url, timeout_s=float(self.timeout_s) + 30)
            if data:
                return FetchResult(url=url, final_url=url, status=200,
                                   content_type=None, kind=_classify_kind(None, url),
                                   body_bytes=data, tier=1, fetched_at=_now_iso())
        except Exception:
            pass
        return None

    def _impersonate_fetch(self, url: str, referer: str | None = None,
                           cookies: list | None = None) -> "FetchResult | None":
        """A WAF-blocked HTML page via curl_cffi's real Chrome TLS/JA3 fingerprint — clears the
        plain-httpx-TLS blocks (Akamai/Cloudflare "Access Denied" 403/401) a browser would
        otherwise be spun up for. The cheap middle tier between httpx and a render. Returns a 200-ish
        HTML FetchResult on success, or None (caller keeps escalating to the browser/C3).

        Off via CRAWLER_IMPERSONATE_FETCH=0. Same curl_cffi + DoH path as _asset_bypass Tier 1."""
        if os.environ.get("CRAWLER_IMPERSONATE_FETCH", "1") == "0":
            return None
        canon = canonicalize_url(url)
        try:
            headers = {
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
            if referer:
                headers["Referer"] = referer
            jar = {c["name"]: c["value"] for c in (cookies or []) if c.get("name")}
            r = _curl_cffi_get(canon, impersonate="chrome", headers=headers, cookies=jar or None,
                               timeout=self.timeout_s, allow_redirects=True)
            ct = r.headers.get("content-type")
            kind = _classify_kind(ct, canon)
            if r.status_code < 400 and r.text and kind in ("html", "other"):
                return FetchResult(url=canon, final_url=str(r.url), status=r.status_code,
                                   content_type=ct, kind="html", text_html=r.text,
                                   tier=1, fetched_at=_now_iso())
        except Exception:
            pass
        return None

    def fetch_assets(self, urls: list[str], referer: str | None = None,
                     cookies: list | None = None) -> list[FetchResult]:
        """Download several same-page assets (images/PDFs) concurrently, results
        in the SAME order as ``urls``. A bounded pool (CRAWLER_ASSET_WORKERS,
        default 6) IS the rate limit — at most N concurrent requests to the host
        — so no per-asset throttle is applied. Each worker uses its own httpx
        client (no shared state); robots + fixtures still honored per URL. Same
        bytes as sequential fetch_asset, just faster.

        ``referer``/``cookies`` (from the page render) are replayed on the 403 bypass
        ladder so a protected asset host sees the same session that loaded the page."""
        if not urls:
            return []
        import functools
        one = functools.partial(self._fetch_asset_core, referer=referer, cookies=cookies)
        workers = int(os.environ.get("CRAWLER_ASSET_WORKERS", "6"))
        if workers <= 1 or len(urls) == 1:
            return [one(u) for u in urls]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as pool:
            return list(pool.map(one, urls))

    # -- fixtures --------------------------------------------------------
    def _from_fixture(self, canon: str, conditional: dict | None) -> FetchResult:
        data, meta = fixtures.get(canon)
        etag = meta.get("etag")
        # Honor conditional GET against the fixture's validator (proves §7A 304).
        if conditional and etag and conditional.get("If-None-Match") == etag:
            return FetchResult(url=canon, final_url=canon, status=304,
                               not_modified=True, etag=etag,
                               last_modified=meta.get("last_modified"),
                               fetched_at=_now_iso(), from_fixture=True)
        ct = meta.get("content_type", "text/html")
        kind = _classify_kind(ct, canon)
        text_html = data.decode("utf-8", errors="ignore") if kind in ("html", "other") else None
        return FetchResult(
            url=canon, final_url=canon, status=200, content_type=ct, kind=kind,
            text_html=text_html, body_bytes=data if kind in ("pdf", "image") else None,
            etag=etag, last_modified=meta.get("last_modified"),
            published_hint=meta.get("published"), fetched_at=_now_iso(),
            from_fixture=True,
        )

    # -- network ---------------------------------------------------------
    def _http_fetch(self, canon: str, conditional: dict | None,
                     user_agent: str | None = None, _fetch_url: str | None = None,
                     _insecure: bool = False) -> FetchResult:
        import httpx
        fetch_url = _fetch_url or canon
        # Dev replay: serve the stored body so extraction/gate changes re-run without re-fetching.
        # Only on the primary call (not the www-retry recursion); ignores conditional GET on purpose.
        if _fetch_url is None and _dev_cache_enabled():
            hit = _dev_cache_get(canon)
            if hit is not None:
                return hit
        headers = {"User-Agent": user_agent or self.user_agent, "Accept": "*/*"}
        # Replay a stored login session (crawler.siteauth) on the httpx tier — without this the
        # cheap path would fetch the logged-OUT teaser of every page the browser tier sees in
        # full, and the two tiers would disagree about what the site contains. The store is
        # host-scoped, so only this host's own cookies can ever be attached, and it is a no-op
        # (empty string) for the overwhelming majority of hosts, which need no account.
        try:
            from . import siteauth
            ck = siteauth.cookie_header(fetch_url)
            if ck:
                headers["Cookie"] = ck
        except Exception:
            pass
        if conditional:
            headers.update({k: v for k, v in conditional.items() if v})
        html_cap = int(os.environ.get("CRAWLER_MAX_HTML_BYTES", str(20 * 1024 * 1024)))
        asset_cap = int(os.environ.get("CRAWLER_MAX_ASSET_BYTES", str(50 * 1024 * 1024)))
        # Honor CRAWLER_IGNORE_HTTPS_ERRORS for httpx too (Chromium already does). Many gov/defence
        # hosts ship a broken cert chain (missing intermediate CA) — with strict verify their PDFs/
        # images fail to download (SSL: CERTIFICATE_VERIFY_FAILED) even though the page rendered fine.
        # `verify` is a property of the httpx CLIENT, not the request, so the insecure retry below
        # cannot be a per-request kwarg — it takes the OTHER pooled client. _shared_http_client is
        # already keyed on this bool, so that client is built lazily on first use and only ever
        # exists in a process that actually took the retry.
        verify_ssl = os.environ.get("CRAWLER_IGNORE_HTTPS_ERRORS", "0") != "1" and not _insecure
        last_err = None
        tried_insecure = False          # at most ONE click-through per URL, whatever max_retries is
        for attempt in range(self.max_retries + 1):
            try:
                # Stream so a Content-Length header (or a streamed overrun) aborts a giant
                # file before it's read whole into RAM — a 500MB "page" no longer OOMs a worker.
                client = _shared_http_client(verify_ssl)
                # Stream on the SHARED pooled client (keepalive reuses the TCP+TLS conn); per-request
                # headers/timeout keep the conditional-GET + per-fetcher timeout behavior intact.
                with client.stream("GET", fetch_url, headers=headers, timeout=self.timeout_s) as resp:
                    ct = resp.headers.get("content-type")
                    etag = resp.headers.get("etag")
                    lm = resp.headers.get("last-modified")
                    sc = resp.status_code
                    final_url = str(resp.url)
                    if sc == 304:
                        return FetchResult(url=canon, final_url=final_url, status=304,
                                           not_modified=True, etag=etag, last_modified=lm,
                                           content_type=ct, fetched_at=_now_iso())
                    kind = _classify_kind(ct, canon)
                    cap = asset_cap if kind in ("pdf", "image") else html_cap
                    clen = resp.headers.get("content-length")
                    if clen and clen.isdigit() and int(clen) > cap:
                        return FetchResult(url=canon, final_url=final_url, status=sc,
                                           content_type=ct, kind=kind, fetched_at=_now_iso(),
                                           error="too_large", reason=errors.TOO_LARGE)
                    buf = bytearray()
                    overrun = stalled = False
                    # Wall-clock budget for the WHOLE body, not just the gap between chunks.
                    # httpx's read timeout only measures inter-chunk silence, so a server that
                    # trickles one byte every few seconds never trips it and holds this worker
                    # forever — measured locally: 26s elapsed, 14 bytes received, no timeout, with
                    # a 10s read timeout configured. That is a tarpit, and with a bounded worker
                    # pool a handful of them starve the whole crawl. Nutch calls the same knob
                    # http.time.limit and documents the same reason.
                    body_deadline = time.monotonic() + _body_budget_s(self.timeout_s)
                    for chunk in resp.iter_bytes():
                        buf += chunk
                        if len(buf) > cap:
                            overrun = True
                            break
                        if time.monotonic() > body_deadline:
                            stalled = True
                            break
                    if overrun:
                        return FetchResult(url=canon, final_url=final_url, status=sc,
                                           content_type=ct, kind=kind, fetched_at=_now_iso(),
                                           error="too_large", reason=errors.TOO_LARGE)
                    if stalled:
                        # TIMEOUT, not TOO_LARGE: the body was slow, not big. The distinction
                        # matters because TIMEOUT is transient (requeued) and TOO_LARGE is terminal.
                        return FetchResult(url=canon, final_url=final_url, status=sc,
                                           content_type=ct, kind=kind, fetched_at=_now_iso(),
                                           error=f"body read timed out after "
                                                 f"{_body_budget_s(self.timeout_s):.0f}s "
                                                 f"({len(buf)} bytes)",
                                           reason=errors.TIMEOUT)
                    enc = resp.encoding or "utf-8"
                text_html = bytes(buf).decode(enc, errors="ignore") if kind in ("html", "other") else None
                body = bytes(buf) if kind in ("pdf", "image") else None
                reason = errors.status_reason(sc) if sc >= 400 else None
                # No status gate. parse_retry_after(None) is None, so this is a no-op on every
                # response without the header — but a 403 CARRYING Retry-After (which
                # edgecase.diagnose already reads as a rate limit) used to arrive here with
                # retry_after_s=None, and the host cooldown then used its 30s default instead of
                # the number the server actually sent. The render path never had this gate either.
                retry_after = errors.parse_retry_after(resp.headers.get("retry-after"))
                result = FetchResult(
                    url=canon, final_url=final_url, status=sc,
                    content_type=ct, kind=kind, text_html=text_html, body_bytes=body,
                    etag=etag, last_modified=lm, fetched_at=_now_iso(),
                    reason=reason, retry_after_s=retry_after,
                    link_header=resp.headers.get("link"),
                )
                if _fetch_url is None and _dev_cache_enabled() and sc and sc < 400:
                    _dev_cache_put(canon, result)
                return result
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                # The redirect cap is not transient and it is not cheap. httpx raises this only
                # after following 20 hops, so each retry spends 20 more requests on one host to
                # relearn a fact that was certain the first time — up to 63 requests for a URL that
                # can never resolve. Break out immediately; the reason below already names it.
                if isinstance(exc, httpx.TooManyRedirects):
                    break
                # Proceed-anyway runs BEFORE the www-retry, on the URL we actually wanted. The
                # www-retry is itself a workaround for this same error class and it `return`s
                # directly, so leaving it first meant clicking through on a host the site does not
                # serve: measured, www.expired.badssl.com answers 421 Misdirected Request with
                # verification off, while expired.badssl.com answers 200.
                if not tried_insecure:
                    tried_insecure = True
                    ok = self._proceed_anyway(canon, conditional, user_agent, fetch_url,
                                              last_err, _insecure)
                    if ok is not None:
                        return ok
                # Some sites' certs/DNS only validate the www. host even though
                # our dedup key strips it — retry once against www. before giving up.
                if _fetch_url is None and _is_tls_or_dns_error(exc):
                    www_url = _with_www(canon)
                    if www_url != canon:
                        return self._http_fetch(canon, conditional, user_agent, _fetch_url=www_url)
                time.sleep(0.3 * (attempt + 1))
        return FetchResult(url=canon, final_url=canon, status=None,
                           fetched_at=_now_iso(), error=last_err or "fetch_failed",
                           reason=errors.classify_failure(None, last_err) or errors.OTHER)

    def _proceed_anyway(self, canon: str, conditional: dict | None, user_agent: str | None,
                        fetch_url: str, last_err: str, _insecure: bool) -> FetchResult | None:
        """"Advanced -> Proceed anyway". Returns a FetchResult only on an untrusted SUCCESS.

        A browser lets a human click through a cert error. This is that, with the three things a
        browser also does: it is per-ORIGIN (the host must be named in the job), it happens only
        AFTER a fully-verified attempt has already failed on a CERTIFICATE, and the result is
        marked untrusted rather than filed with every other page.

        STRUCTURAL, not a check: this is the only caller that can reach the verify=False client,
        and it returns early unless the failure classified as SSL. There is no path by which a host
        that never produced a certificate error is fetched with verification off.

        SSRF: private_target is re-run HERE, on the exact URL about to be re-fetched, and it is the
        LAST word — a host may be listed and still be refused. Discovered links are screened at
        enqueue time, but SEED urls are not, so this is the only guard covering a seed pointed at
        169.254.169.254 / metadata.google.internal.
        """
        if _insecure or not self.tls_insecure_hosts:
            return None
        if (errors.classify_failure(None, last_err) or errors.OTHER) != errors.SSL:
            return None
        from . import edgecase
        h = (urlsplit(fetch_url).hostname or "").lower().strip(".")
        h = h[4:] if h.startswith("www.") else h
        priv = edgecase.private_target(fetch_url)
        if priv:
            log.warning("REFUSED tls-insecure retry for %s — internal address (%s)",
                        fetch_url, priv)
            return None
        if h not in self.tls_insecure_hosts:
            return None
        log.warning("TLS VERIFICATION DISABLED for one fetch of %s (host listed in "
                    "job.tls_insecure_hosts). Original certificate error: %s", fetch_url, last_err)
        retried = self._http_fetch(canon, conditional, user_agent,
                                   _fetch_url=fetch_url, _insecure=True)
        if retried.error is None and retried.status and retried.status < 400:
            retried.insecure_tls = (last_err or "")[:300]
            return retried
        # Nothing usable. A handshake/protocol failure is not a TRUST failure and verify=False
        # cannot fix one — so return None and let the caller report the ORIGINAL certificate
        # error, which is what the operator needs to see.
        return None

# Resource types a TEXT crawler never reads. Aborting them at the route layer is the single
# cheapest speed win available to a render tier: the page still lays out and its JS still runs,
# but the browser stops fetching hero images, webfonts, video and analytics beacons.
#
# LEARNED FROM SCRAPLING (scrapling/engines/constants.py::EXTRA_RESOURCES, applied through a
# route handler in toolbelt/navigation.py::create_intercept_handler). It blocks exactly this set
# by default and documents ~25% faster fetches; benchmarked head-to-head it averaged 67s per
# source against our 178s. We were blocking nothing at all.
#
# Stylesheets are in the list deliberately: layout still computes without them, and no text
# extraction we do reads CSS. Scripts are NOT — they are what renders the tender tables.
_USELESS_RESOURCES = frozenset({
    "font", "image", "media", "beacon", "object", "imageset", "texttrack", "websocket",
    "csp_report", "stylesheet",
})


def _block_useless_resources(page) -> None:
    """Abort image/font/media/stylesheet requests on this page. Best-effort and never fatal:
    a route handler that fails must not cost us the page it was meant to speed up."""
    if os.environ.get("CRAWLER_BLOCK_RESOURCES", "1") != "1":
        return
    try:
        page.route("**/*", lambda route: (
            route.abort() if route.request.resource_type in _USELESS_RESOURCES
            else route.continue_()))
    except Exception as exc:                                    # noqa: BLE001
        log.debug("resource blocking not installed: %s", exc)



    def _render_fetch(self, canon: str, interaction_cfg: InteractionConfig | None = None,
                       user_agent_override: str | None = None) -> FetchResult | None:
        """Render with Playwright if available; else None (graceful degrade).

        Handles pure SPAs where every server-side route returns 404 — we bootstrap
        through the root page then client-side navigate to the target path.

        Playwright's sync API allows only one driver connection per thread, so
        when a shared click-through page is open (self._shared_ctx), this reuses
        its browser (a new tab) instead of starting a second sync_playwright()
        — starting a second one would raise, breaking the cold-fetch fallback
        that click-mode depends on whenever a link can't be found/clicked."""
        owns_browser = self._shared_ctx is None
        if owns_browser:
            try:
                from playwright.sync_api import sync_playwright
            except Exception:
                return None
        pw_cm = None
        page = None
        try:
            if owns_browser:
                pw_cm = sync_playwright().start()
                headless = os.environ.get("CRAWLER_HEADLESS", "1") != "0"
                slow_mo = int(os.environ.get("CRAWLER_SLOW_MO", "0"))
                browser = pw_cm.chromium.launch(
                    headless=headless, slow_mo=slow_mo,
                    args=["--no-sandbox", *stealth.stealth_launch_args(), *stealth.doh_launch_args()],
                    ignore_default_args=stealth.stealth_ignore_default_args())
                # Look like a real Chrome session: WAFs fingerprint missing
                # Accept-Language / locale / timezone as bot traffic.
                ctx = browser.new_context(
                    user_agent=user_agent_override or _BROWSER_UA_FALLBACK,
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York",
                    extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                )
                if stealth.stealth_enabled():
                    ctx.add_init_script(stealth.STEALTH_INIT_JS)
                page = ctx.new_page()
                _block_useless_resources(page)
            else:
                # Navigate the shared page itself (not a new tab) so its
                # current location keeps advancing — that's what lets
                # _click_or_goto find links on wherever we just landed.
                _, browser, ctx, page = self._shared_ctx

            html = None
            inner_text = None
            status = None

            # Attempt 1: direct navigation. Some sites' certs/DNS only validate
            # the www. host even though our dedup key strips it — retry once
            # against www. before giving up.
            nav_url = canon
            try:
                resp = page.goto(nav_url, timeout=self.timeout_s * 1000,
                                 wait_until="domcontentloaded")
            except Exception as exc:
                www_url = _with_www(canon)
                if not _is_tls_or_dns_error(exc) or www_url == canon:
                    raise
                # Let the failed navigation's internal error page settle before
                # retrying, or Chromium reports the retry as "interrupted by
                # another navigation."
                page.wait_for_timeout(500)
                nav_url = www_url
                resp = page.goto(nav_url, timeout=self.timeout_s * 1000,
                                 wait_until="domcontentloaded")
            try:
                # domcontentloaded already fired; this waits for XHR/JS to settle
                # but EXITS EARLY the instant the network idles. The cap only
                # bounds sites that never idle (analytics beacons / websockets) —
                # they'd otherwise burn the full 15s every page. 4s default.
                page.wait_for_load_state(
                    "networkidle",
                    timeout=int(os.environ.get("CRAWLER_NETWORKIDLE_MS", "4000")))
            except Exception:
                pass
            # Fixed post-load settle. networkidle already waited for the network;
            # this is just for late JS paint. 500ms is plenty for most sites.
            page.wait_for_timeout(int(os.environ.get("CRAWLER_RENDER_SETTLE_MS", "500")))
            html = page.content()
            status = resp.status if resp else None

            # SPA bootstrap: if we got a tiny 404 body, the JS router never ran.
            # Navigate to root, let the SPA boot, then client-side navigate to target.
            if status and status >= 400 and len(html) < 5000 and canon.rstrip("/") != _root_url(canon).rstrip("/"):
                pre_bootstrap_len = len(html)
                root = _root_url(canon)
                rel_path = _extract_path(canon)
                page.goto(root, timeout=self.timeout_s * 1000,
                          wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state(
                        "networkidle",
                        timeout=int(os.environ.get("CRAWLER_NETWORKIDLE_MS", "4000")))
                except Exception:
                    pass
                page.wait_for_timeout(3000)
                # Click the nav link matching this path to trigger SPA router
                link = page.query_selector(f'a[href="{rel_path}"]')
                if not link:
                    link = page.query_selector(f'a[href$="{rel_path}"]')
                navigated = False
                if link:
                    try:
                        link.click(timeout=10000)
                        navigated = True
                    except Exception:
                        try:
                            page.evaluate(
                                """(p) => { window.location.hash = ''; window.history.pushState({}, '', p); }""",
                                rel_path)
                            navigated = True
                        except Exception:
                            navigated = False
                    page.wait_for_timeout(3000)
                candidate_html = page.content()
                # Only trust the bootstrap result if the DOM demonstrably
                # changed (bigger body) and/or the URL now matches the
                # target path — otherwise we're still looking at the same
                # 404 shell / homepage and must not mask that as success.
                url_matches = urlsplit(page.url).path.rstrip("/") == rel_path.rstrip("/")
                content_changed = len(candidate_html) > pre_bootstrap_len * 1.5
                if navigated and (url_matches or content_changed):
                    html = candidate_html
                    status = 200  # verified: rendered content replaced the server 404
                # else: keep the original >=400 status — a broken/blank
                # SPA route must surface as an error, not a fabricated 200.

            if interaction_cfg and self._has_any_interaction(interaction_cfg):
                html, inner_text = interaction.run_interactions(page, interaction_cfg)

            # Grab the full-page screenshot NOW, on the already-rendered page,
            # instead of relaunching a second browser + re-navigating per kept
            # page (the old screenshot.capture path). enrich_assets uses this.
            shot_png = None
            if self.screenshot_wanted:
                try:
                    shot_png = page.screenshot(full_page=True)
                except Exception:
                    shot_png = None

            final_url = page.url
            if owns_browser:
                browser.close()   # tears down our own browser+context+driver
            # else: leave the shared page open — close_shared_page() (called once
            # by harvest() at the end of the job) owns its teardown.
            return FetchResult(url=canon, final_url=final_url, status=status,
                               content_type="text/html", kind="html", text_html=html,
                               inner_text=inner_text, screenshot_png=shot_png,
                               tier=1, fetched_at=_now_iso())
        except Exception as exc:  # noqa: BLE001
            if owns_browser and pw_cm is not None:
                try:
                    pw_cm.stop()
                except Exception:
                    pass
            return FetchResult(url=canon, final_url=canon, status=None,
                               fetched_at=_now_iso(), error=f"render_failed: {exc}",
                               reason=errors.classify_failure(None, f"render_failed: {exc}"))

    @staticmethod
    def _has_any_interaction(cfg: InteractionConfig) -> bool:
        return bool(
            (cfg.scroll and cfg.scroll.enabled)
            or (cfg.paginate and cfg.paginate.enabled)
            or (cfg.click and cfg.click.enabled)
            or (cfg.hover and cfg.hover.enabled)
            or (cfg.search and cfg.search.enabled)
        )

    def _throttle(self, delay_s: float | None = None) -> None:
        """Respect crawl_delay_seconds between live fetches. Pass a smaller
        delay for asset sub-fetches (see fetch_asset)."""
        d = self.delay_s if delay_s is None else delay_s
        if d <= 0:
            return
        elapsed = time.monotonic() - self._last_fetch_ts
        if elapsed < d:
            time.sleep(d - elapsed)
        self._last_fetch_ts = time.monotonic()


def _root_url(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/", "", ""))


def _extract_path(url: str) -> str:
    from urllib.parse import urlsplit
    p = urlsplit(url)
    return p.path or "/"

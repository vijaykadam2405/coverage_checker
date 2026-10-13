"""C1 anti-detection + privacy helpers, shared by the async browser pool (async_engine) and the
sync render path (fetcher). Everything here is pure/deterministic so it unit-tests without a
browser.

ponytail: the launch args + init script neutralize the CHEAP, common headless tells that most WAFs
check (navigator.webdriver, the automation switch, missing plugins/languages). They do NOT fix the
deep CDP `Runtime.enable` / main-world-execution-context leak — that needs a patched Playwright
(patchright / rebrowser-patches: swap the `chromium.launch` import). Add that only if a
CDP-sniffing WAF (advanced DataDome) still blocks C1 after this.
"""
from __future__ import annotations

import os

# Injected before any page script runs (context.add_init_script) — masks the tells a WAF reads off
# `navigator` in plain headless Chromium. Wrapped in try/catch so a hardened page can't throw here.
STEALTH_INIT_JS = """
(() => {
  try { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}); } catch (e) {}
  try { Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]}); } catch (e) {}
  try { Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']}); } catch (e) {}
  try { window.chrome = window.chrome || {runtime: {}}; } catch (e) {}
  try {
    const _q = navigator.permissions && navigator.permissions.query;
    if (_q) navigator.permissions.query = (p) =>
      (p && p.name === 'notifications')
        ? Promise.resolve({state: Notification.permission})
        : _q(p);
  } catch (e) {}
})();
"""


def stealth_enabled() -> bool:
    """C1 stealth hardening on by default; CRAWLER_STEALTH=0 reverts to plain Playwright."""
    return os.environ.get("CRAWLER_STEALTH", "1") != "0"


def stealth_launch_args() -> list[str]:
    """Chromium launch args that remove the automation fingerprint. `AutomationControlled` off is
    what actually drops `navigator.webdriver` at the source (the init script is belt-and-braces)."""
    if not stealth_enabled():
        return []
    return ["--disable-blink-features=AutomationControlled"]


def stealth_ignore_default_args() -> list[str]:
    """Playwright launches Chromium WITH `--enable-automation` by default (sets the webdriver flag +
    the 'controlled by automated software' infobar). Ignoring it removes that tell. Empty list when
    stealth is off = ignore nothing (Playwright default)."""
    return ["--enable-automation"] if stealth_enabled() else []


def doh_url() -> str | None:
    """The configured DNS-over-HTTPS template, or None. e.g. https://cloudflare-dns.com/dns-query"""
    return os.environ.get("CRAWLER_DOH_TEMPLATE", "").strip() or None


def doh_launch_args() -> list[str]:
    """Chromium DNS-over-HTTPS in SECURE mode (no plaintext fallback ⇒ no DNS leak behind a proxy),
    only when a template is configured. OFF by default: secure mode fails hard if the DoH host is
    unreachable, so it's an opt-in privacy tradeoff for careful/gov crawling."""
    tmpl = doh_url()
    if not tmpl:
        return []
    return ["--dns-over-https-mode=secure", f"--dns-over-https-templates={tmpl}"]


# Third-party ad/analytics/tracker hosts to abort at the wire — faster renders + a smaller
# fingerprint surface. These are NEVER a crawl target (always third-party beacons), so a suffix
# match can't drop real content. Curated to the heavy hitters (>90% of tracker requests).
# ponytail: swap for an EasyList / hosts-file load if coverage ever needs to be exhaustive.
TRACKER_HOSTS = frozenset({
    "google-analytics.com", "googletagmanager.com", "googletagservices.com",
    "googlesyndication.com", "doubleclick.net", "adservice.google.com",
    "connect.facebook.net", "facebook.net", "ads-twitter.com", "static.ads-twitter.com",
    "hotjar.com", "mixpanel.com", "segment.com", "segment.io", "amplitude.com",
    "quantserve.com", "scorecardresearch.com", "criteo.com", "criteo.net",
    "taboola.com", "outbrain.com", "bat.bing.com", "clarity.ms",
    "cloudflareinsights.com", "newrelic.com", "nr-data.net", "optimizely.com",
    "fullstory.com", "sentry.io", "hs-analytics.net", "pardot.com",
})


def is_tracker(host: str) -> bool:
    """True if host is (or is a subdomain of) a known tracker in TRACKER_HOSTS."""
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in TRACKER_HOSTS)


def block_trackers_enabled() -> bool:
    """Tracker blocking on by default; CRAWLER_BLOCK_TRACKERS=0 disables."""
    return os.environ.get("CRAWLER_BLOCK_TRACKERS", "1") != "0"


def demo() -> None:
    """Runnable self-check for the pure logic (python -m crawler.stealth)."""
    assert is_tracker("www.google-analytics.com")
    assert is_tracker("googletagmanager.com")
    assert not is_tracker("mbda-systems.com")
    assert not is_tracker("mod.gov.my")
    assert not is_tracker("")
    os.environ.pop("CRAWLER_DOH_TEMPLATE", None)
    assert doh_launch_args() == []
    os.environ["CRAWLER_DOH_TEMPLATE"] = "https://cloudflare-dns.com/dns-query"
    assert any("dns-over-https-mode=secure" in a for a in doh_launch_args())
    os.environ.pop("CRAWLER_DOH_TEMPLATE")
    os.environ.pop("CRAWLER_STEALTH", None)
    assert "--disable-blink-features=AutomationControlled" in stealth_launch_args()
    os.environ["CRAWLER_STEALTH"] = "0"
    assert stealth_launch_args() == [] and stealth_ignore_default_args() == []
    os.environ.pop("CRAWLER_STEALTH")
    print("stealth demo OK")


if __name__ == "__main__":
    demo()

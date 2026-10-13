"""C4 tier — hand a Cloudflare-INTERACTIVE page to the host-side solver.

The last rung of the fetch ladder, reached only when a page is walled by a challenge that provably
cannot clear from inside the container:

    C1 httpx / curl_cffi JA3  ->  C2 Wayback  ->  C3 CamoFox stealth  ->  C4 host solver

WHY A SEPARATE PROCESS ON THE HOST
----------------------------------
Cloudflare's interactive tier never self-clears and injects no Turnstile widget, so there is nothing
in the page for the crawler to click. What DOES clear it is a real GPU: measured, the container gets
`ANGLE (Mesa, llvmpipe)` and is blocked, while the Windows host gets `ANGLE (Intel(R) Graphics,
Direct3D11)` and passes. llvmpipe cannot be spoofed away in JS (the capability surface is
cross-checked) and WSL2 does not pass OpenGL through to the Docker VM. So the crawler calls out to
`scripts/host_solver.py`, the same way it already calls out to the host's Ollama for vision OCR.

WHY IT IS WORTH THE HOP
-----------------------
The solver keeps its browser warm, and cf_clearance keeps working for the session that earned it.
Measured on zoopla.co.uk:

    cold (solves the challenge) : 469,757 chars in 10.5s
    warm (same session, no challenge at all) : 630,653 / 467,244 / 487,423 chars in 6.2 / 6.0 / 5.1s

So the FIRST page of a challenged host pays the solve and every page after it is an ordinary
navigation — which is what makes crawling such a site practical rather than a one-URL rescue.

Disabled unless CRAWLER_HOST_SOLVER_URL is set, so a deployment without the host service behaves
exactly as before (the wall is recorded in the issues log and the crawl moves on).
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def enabled() -> bool:
    return bool(os.environ.get("CRAWLER_HOST_SOLVER_URL", "").strip())


def base_url() -> str:
    return os.environ.get("CRAWLER_HOST_SOLVER_URL", "").strip().rstrip("/")


def timeout_s() -> float:
    # Generous: a cold solve drives a real browser through a challenge. The crawler's own item
    # watchdog is the outer bound.
    return float(os.environ.get("CRAWLER_HOST_SOLVER_TIMEOUT_S", "180"))


def solve(url: str) -> dict | None:
    """Fetch `url` through the host solver. Returns the service's dict, or None if it is not
    configured / unreachable / returned nothing usable.

    Never raises: this is the last rung, and a failure here means "record the wall and move on",
    never "end the crawl"."""
    if not enabled():
        return None
    import httpx
    try:
        with httpx.Client(timeout=timeout_s()) as c:
            r = c.post(f"{base_url()}/solve", json={"url": url})
            if r.status_code >= 400:
                log.warning("host solver HTTP %s for %s", r.status_code, url)
                return None
            data = r.json()
    except Exception as exc:                          # noqa: BLE001
        log.warning("host solver unreachable (%s: %s) — leaving %s walled",
                    type(exc).__name__, str(exc)[:90], url)
        return None
    # `ok` is judged on CONTENT by the service (a 200 that is still the interstitial is the failure
    # mode), so trust it rather than re-deriving it here.
    if not data.get("ok") or not (data.get("html") or ""):
        log.info("host solver could not clear %s (solved=%s, %s chars, %s)",
                 url, data.get("solved"), data.get("chars"), data.get("error") or "no error")
        return None
    log.info("host solver cleared %s via=%s solved=%s chars=%s in %ss",
             url, data.get("via"), data.get("solved"), data.get("chars"), data.get("elapsed"))
    return data


def health() -> dict | None:
    if not enabled():
        return None
    import httpx
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(f"{base_url()}/health")
            return r.json() if r.status_code < 400 else None
    except Exception:                                 # noqa: BLE001
        return None

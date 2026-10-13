"""HTTP transport for the SIPRI harvester. C3 (CamoFox) first, direct httpx as the fallback.

A CORRECTED FINDING, worth keeping because it nearly became a permanent design decision:

C3 was failing 100% against sipri.org, and the obvious reading — "SIPRI blocks the CamoFox egress"
— was wrong. A control test against example.com and Wikipedia failed identically, which proved the
fault was ours, not theirs. Root cause: the Gluetun VPN container was unhealthy (its VPNGate server
at 104.59.204.175:1810 refusing connections), so CamoFox's configured proxy accepted TCP but routed
nothing, camoufox could not resolve a public IP, and the browser never launched — for every site.
With the dead proxy removed, C3 renders sipri.org fine. Never attribute a transport failure to the
target without testing the transport against a control.

How "through C3" works here: the API is POST-only, so a page render cannot reach it. Instead we open
a tab on the SPA's own origin and run the same fetch() the site's UI runs, via CamoFox /evaluate —
so the call carries a real browser's TLS fingerprint, headers and Origin. See post_json_via_c3.

Direct httpx stays wired as the fallback: C3 was down for hours today, and a harvester that returns
no data when the browser tier breaks is worse than one that quietly still works.

Politeness is not optional here: their server is slow, so every call goes through one shared
rate limiter with a floor of REQUEST_DELAY_S between requests, process-wide.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from . import config

log = logging.getLogger("sipri.fetch")

_LOCK = threading.Lock()
_next_ok = 0.0


def _throttle() -> None:
    """Global spacing between SIPRI requests. Module-level, not per-caller, so the transfers pull
    and the dataset downloads cannot accidentally double the request rate by running together."""
    global _next_ok
    with _LOCK:
        wait = _next_ok - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _next_ok = time.monotonic() + config.REQUEST_DELAY_S


def _c3_enabled() -> bool:
    return os.environ.get("SIPRI_USE_C3", "1") == "1"


# One tab reused for the whole harvest. A tab per page would mean 15 browser sessions for the arms
# transfers pull; CamoFox charges a real launch cost per tab and the SPA origin only needs opening
# once. Reset to None on 410 Gone (the tab expired) and the next call reopens.
_C3_TAB: str | None = None


def _c3_api() -> tuple[str, str]:
    import httpx
    return config.API_BASE, os.environ.get("CAMOFOX_URL", "http://camofox:9377").rstrip("/")


def _c3_open_tab() -> str | None:
    """Open a tab on the SPA's own origin so in-page fetches carry the right Origin/Referer."""
    global _C3_TAB
    import httpx

    _base, cam = _c3_api()
    try:
        with httpx.Client(timeout=config.TIMEOUT_S) as c:
            r = c.post(f"{cam}/tabs", json={"userId": "sipri-harvester", "sessionKey": "s",
                                            "url": config.SPA_ORIGIN})
        if r.status_code >= 400:
            log.warning("C3 tab create -> %s: %.140s", r.status_code, r.text)
            return None
        _C3_TAB = r.json().get("tabId")
        return _C3_TAB
    except Exception as exc:                            # noqa: BLE001
        log.warning("C3 tab create failed: %s", exc)
        return None


def post_json_via_c3(path: str, body: dict) -> object | None:
    """POST to the SIPRI API from INSIDE the CamoFox browser.

    The API is POST-only, so a page render cannot reach it — but the SPA itself calls it with
    fetch(), and CamoFox's /evaluate runs arbitrary JS in the page. Doing the same thing means the
    request carries the real browser's TLS fingerprint, headers and Origin, which is what "through
    C3" actually buys: it is indistinguishable from the site's own UI making the call.
    """
    global _C3_TAB
    import json as _json

    import httpx

    _base, cam = _c3_api()
    key = os.environ.get("CAMOFOX_API_KEY", "").strip()
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"      # /evaluate is the auth-gated route

    for attempt in range(config.MAX_RETRIES):
        tab = _C3_TAB or _c3_open_tab()
        if not tab:
            return None
        _throttle()
        expr = (
            "(async()=>{const r=await fetch(%s,{method:'POST',"
            "headers:{'Content-Type':'application/json','Authorization':''},"
            "body:JSON.stringify(%s)});return JSON.stringify(await r.json());})()"
            % (_json.dumps(f"{config.API_BASE}{path}"), _json.dumps(body))
        )
        try:
            with httpx.Client(timeout=config.TIMEOUT_S) as c:
                r = c.post(f"{cam}/tabs/{tab}/evaluate", headers=headers,
                           json={"userId": "sipri-harvester", "expression": expr})
            if r.status_code == 410:
                _C3_TAB = None                          # tab expired — reopen and retry
                continue
            if r.status_code >= 400:
                log.warning("C3 evaluate -> %s: %.140s", r.status_code, r.text)
                _C3_TAB = None
                continue
            payload = r.json()
            result = payload.get("result") if isinstance(payload, dict) else None
            if not result:
                log.warning("C3 evaluate returned no result: %.140s", str(payload))
                continue
            return _json.loads(result) if isinstance(result, str) else result
        except Exception as exc:                        # noqa: BLE001
            log.warning("C3 evaluate failed: %s", exc)
            _C3_TAB = None
    return None


def _c3_get(url: str) -> bytes | None:
    """Fallback through the C3 CamoFox lane. Returns rendered HTML bytes, or None if refused."""
    try:
        from crawler import camofox_client
    except Exception:                                  # noqa: BLE001 — harvester must not hard-depend on C3
        return None
    if not camofox_client.enabled():
        return None
    snap = camofox_client.render(url, timeout_s=config.TIMEOUT_S)
    if not snap:
        return None
    body = snap.get("html") or snap.get("text") or ""
    return body.encode("utf-8", "replace") if body else None


def get(url: str, *, headers: dict | None = None) -> tuple[bytes, str] | None:
    """GET with retries. Returns (body, content_type) or None. Never raises for a bad response."""
    import httpx

    hdrs = {"User-Agent": config.BROWSER_UA, **(headers or {})}
    for attempt in range(config.MAX_RETRIES):
        _throttle()
        try:
            with httpx.Client(timeout=config.TIMEOUT_S, follow_redirects=True) as c:
                r = c.get(url, headers=hdrs)
            if r.status_code == 200:
                return r.content, r.headers.get("content-type", "")
            log.info("GET %s -> %s (attempt %d)", url, r.status_code, attempt + 1)
        except Exception as exc:                        # noqa: BLE001
            log.info("GET %s failed: %s (attempt %d)", url, exc, attempt + 1)

    if _c3_enabled():
        log.info("direct GET exhausted for %s — trying C3", url)
        body = _c3_get(url)
        if body:
            return body, "text/html"
    return None


def post_json(path: str, body: dict) -> object | None:
    """POST JSON to the SIPRI API and return the parsed response, or None.

    `path` is a path under config.API_BASE, not a full URL — every caller must go through the same
    base so a host change is one edit.
    """
    import json as _json

    import httpx

    # C3 FIRST when enabled: the requirement is that SIPRI data arrives through the stealth
    # browser. Direct httpx remains as the fallback so a CamoFox outage degrades to working data
    # rather than no data — C3 was down for hours today because of a dead VPN proxy.
    if _c3_enabled():
        out = post_json_via_c3(path, body)
        if out is not None:
            return out
        log.warning("C3 transport failed for %s — falling back to direct", path)

    url = f"{config.API_BASE}{path}"
    for attempt in range(config.MAX_RETRIES):
        _throttle()
        try:
            with httpx.Client(timeout=config.TIMEOUT_S, follow_redirects=True) as c:
                r = c.post(url, headers=config.API_HEADERS, json=body)
            if r.status_code == 200:
                try:
                    return r.json()
                except _json.JSONDecodeError:
                    return r.text                        # /search/count answers as plain text
            # Spring echoes a precise reason (e.g. a wrong `sorts` type) — surface it, don't bury it.
            log.warning("POST %s -> %s: %s", path, r.status_code, r.text[:200])
        except Exception as exc:                         # noqa: BLE001
            log.info("POST %s failed: %s (attempt %d)", path, exc, attempt + 1)
    return None

"""C3 — CamoFox stealth-render client (§ Tri-Crawler).

jo-inc/camofox-browser is a standalone anti-detection browser SERVER (a Firefox
fork with C++-engine-level fingerprint spoofing) exposing a REST API on :9377 —
NOT a Playwright target. We call it on demand as the WAF-block fallback (C3):
open a tab, read the accessibility snapshot (lightweight, LLM-ready text), close
the tab. The snapshot — not bloated HTML — IS the content, per the plan.

Disabled unless CAMOFOX_ENABLED=1, so the fallback ladder cleanly skips C3 when
no CamoFox server is deployed. Server URL via CAMOFOX_URL (default the compose
service http://camofox:9377). Every call is best-effort — returns None on any
error so the caller falls through to the archival ladder.

ponytail: thin httpx REST wrapper (~1 open + 1 snapshot + 1 close); no CLI
subprocess, no Playwright, no camofox SDK.
"""
from __future__ import annotations

import base64
import itertools
import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import captcha as captcha_mod
from . import captcha_solver

log = logging.getLogger("camofox")

# camofox-browser creates a fresh browser CONTEXT per userId. A unique userId PER
# URL is the strongest anti-bot posture, BUT the server's session caps REJECT (503)
# rather than evict, so thousands of per-URL contexts pile up and OOM the 128MB→2GB
# Node heap. Instead ROTATE a small bounded pool of userIds (round-robin): still
# isolated, fresh-ish contexts, but ≤N live sessions instead of hundreds.
# ponytail: 4 rotating contexts is enough isolation for a reactive fallback; per-URL
# uniqueness was over-isolation that caused the OOM crash.
#
# THE PREFIX MUST BE PER-INSTANCE. CamoFox keys a browser context on (userId, sessionKey), and both
# of these used to be hardcoded — so every container in the fleet asked for "crawler-0".."crawler-3"
# with sessionKey "s" and they all landed in the SAME four contexts. That was invisible while C3 had
# a single owner; opening the tier to all 12 instances turned it into three real faults at once:
# twelve instances contending for four contexts however high MAX_SESSIONS is set, cookies and
# localStorage bleeding between instances that are supposed to be independent, and one instance's
# logged-in session being handed to another mid-crawl.
#
# The container hostname is unique per container, needs no new configuration, and is stable for the
# container's life — which is exactly the lifetime a browser context should share.
_INSTANCE = re.sub(r"[^A-Za-z0-9]", "", socket.gethostname())[:12] or "x"
_USER_PREFIX = f"crawler-{_INSTANCE}-"
_SESSION = f"s{_INSTANCE}"


def _pool_size() -> int:
    try:
        return max(1, int(os.environ.get("CRAWLER_CAMOFOX_POOL", "4")))
    except ValueError:
        return 4


# Round-robin userId picker, thread-safe (render/fetch_bytes run under asyncio.to_thread
# on the pool). Rebuilt if the env pool size changes between calls (cheap).
_pool_lock = threading.Lock()
_pool_cycle = itertools.cycle(f"{_USER_PREFIX}{i}" for i in range(_pool_size()))
_pool_n = _pool_size()


def _next_user() -> str:
    global _pool_cycle, _pool_n
    with _pool_lock:
        n = _pool_size()
        if n != _pool_n:                        # pool size changed → rebuild the cycle
            _pool_cycle = itertools.cycle(f"{_USER_PREFIX}{i}" for i in range(n))
            _pool_n = n
        return next(_pool_cycle)


def enabled() -> bool:
    return os.environ.get("CAMOFOX_ENABLED", "0") == "1"


def _bases() -> list[str]:
    """Every CamoFox server this instance may use. CAMOFOX_URL is comma-separated; one entry is
    the ordinary single-server case and behaves exactly as it always did."""
    raw = os.environ.get("CAMOFOX_URL", "http://camofox:9377")
    out = [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]
    return out or ["http://camofox:9377"]


def _base() -> str:
    """A server, for callers that do not have a userId (health, and fetch_bytes' own tab)."""
    return _bases()[0]


def _base_for(user: str | None) -> str:
    """The server this userId is pinned to.

    Deterministic on purpose. CamoFox keys a browser context on (userId, sessionKey), so a userId
    that moves between servers gets a SEPARATE context on each one: cookies, localStorage and any
    logged-in session split, and the anti-bot posture the rotating pool exists to provide is lost.
    Hashing the userId keeps one userId on one server for as long as the pool composition is
    unchanged, while still spreading the instance's several userIds across the whole pool.

    md5 rather than hash(): PYTHONHASHSEED randomises str hashing per process, so hash() would
    re-pin every userId on every container restart.
    """
    pool = _bases()
    if len(pool) == 1 or not user:
        return pool[0]
    import hashlib
    h = int(hashlib.md5(user.encode("utf-8", "replace")).hexdigest()[:8], 16)
    return pool[h % len(pool)]


def base_url() -> str:
    return _base()


def pool_urls() -> list[str]:
    """Exposed so the API can report what the render pool actually is - an operator reading
    'C3 healthy' off one server while five others are dead is the failure this prevents."""
    return _bases()


def _headers() -> dict:
    """Bearer header for the auth-gated /evaluate endpoint (raw-HTML grab).
    Empty when no key is set — /evaluate then just fails and we fall back to the
    aria snapshot for text."""
    key = os.environ.get("CAMOFOX_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


# ── Captcha detection and solving ─────────────────────────────────────────────

@dataclass
class CaptchaInfo:
    captcha_type: str | None = None
    sitekey: str | None = None
    solved: bool = False
    solver: str | None = None
    cost_usd: float | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # How this wall CAN be beaten: token_injectable / wait_only / vendor_task / not_injectable /
    # self_solving. Drives whether a paid solve is even attempted — see crawler/captcha.py.
    solvability: str = "none"
    blocking: bool = False      # the page body is withheld until this clears


def _evaluate(c, base: str, user: str, tab_id: str, expr: str):
    """Run one JS expression in the tab; None if the call failed. Never raises."""
    try:
        e = c.post(f"{base}/tabs/{tab_id}/evaluate",
                   json={"userId": user, "expression": expr}, headers=_headers())
        if e.status_code >= 400:
            log.debug("evaluate HTTP %s", e.status_code)
            return None
        return e.json().get("result")
    except Exception as exc:
        log.debug("evaluate failed: %s", exc)
        return None


def detect_captcha(c, base: str, user: str, tab_id: str) -> CaptchaInfo:
    """Detect a captcha wall in the current tab.

    One round trip gathers raw DOM evidence; crawler.captcha.classify() decides. The split keeps
    the decision testable offline — the previous inline version could only be checked by hitting a
    live site, and it shipped for months returning `recaptcha_v2` on hCaptcha pages and nothing at
    all on a Cloudflare managed challenge."""
    ev = _evaluate(c, base, user, tab_id, captcha_mod.EVIDENCE_JS)
    d = captcha_mod.classify(ev if isinstance(ev, dict) else None)
    if not d.found:
        return CaptchaInfo()
    return CaptchaInfo(captcha_type=d.kind, sitekey=d.sitekey, solvability=d.solvability,
                       blocking=d.blocking, metadata={"all_kinds": d.all_kinds})


def _solve_with_buster(c, base: str, user: str, tab_id: str, timeout_ms: int) -> CaptchaInfo:
    """Use the CamoFox Buster extension bridge to solve reCAPTCHA."""
    try:
        r = c.post(f"{base}/tabs/{tab_id}/solve",
                   json={"userId": user, "captchaType": "recaptcha", "maxWaitMs": timeout_ms},
                   headers=_headers())
        if r.status_code >= 400:
            return CaptchaInfo(error=f"buster endpoint error {r.status_code}")
        data = r.json()
        if data.get("solved"):
            return CaptchaInfo(captcha_type="recaptcha_v2", solved=True,
                               solver="buster", metadata=data)
        return CaptchaInfo(error=data.get("error") or "buster did not solve")
    except Exception as e:
        return CaptchaInfo(error=f"buster exception: {e}")


def _solve_with_commercial(info: CaptchaInfo, page_url: str) -> CaptchaInfo:
    solver = captcha_solver.CaptchaSolver.from_env()
    if info.captcha_type == "recaptcha_v2" and info.sitekey:
        res = solver.solve_recaptcha_v2(info.sitekey, page_url)
    elif info.captcha_type == "hcaptcha" and info.sitekey:
        res = solver.solve_hcaptcha(info.sitekey, page_url)
    elif info.captcha_type == "turnstile" and info.sitekey:
        res = solver.solve_turnstile(info.sitekey, page_url)
    elif info.captcha_type == "datadome":
        res = solver.solve_datadome(captcha_url=page_url, page_url=page_url)
    else:
        return CaptchaInfo(captcha_type=info.captcha_type, sitekey=info.sitekey,
                           error="no commercial solver task for this captcha type")
    if res.solved:
        return CaptchaInfo(captcha_type=info.captcha_type, sitekey=info.sitekey,
                           solved=True, solver=res.method, cost_usd=res.cost_usd,
                           metadata={"token": res.token})
    return CaptchaInfo(captcha_type=info.captcha_type, sitekey=info.sitekey,
                       error=res.error or "commercial solver failed")


def _inject_token(c, base: str, user: str, tab_id: str, info: CaptchaInfo) -> bool:
    """Write the solver token into the page's response field and fire the site's own callback.

    Pure DOM. The previous version called grecaptcha.setResponse(), hcaptcha.setResponse() and
    turnstile.setResponse() — none of which exist; all three measured `undefined` on a live page,
    along with the vendor globals themselves. So every commercial solve that was paid for was then
    thrown away here.

    Writing the field is only half of it: the site is usually waiting on the callback passed to
    render(), which lives in ___grecaptcha_cfg.clients and is not reachable from the DOM."""
    token = (info.metadata or {}).get("token")
    if not token or not info.captcha_type:
        return False
    expr = captcha_mod.injection_js(info.captcha_type, token)
    if not expr:
        return False
    res = _evaluate(c, base, user, tab_id, expr)
    if not isinstance(res, dict):
        return False
    if res.get("error"):
        log.warning("token injection error type=%s: %s", info.captcha_type, res["error"])
    fields = res.get("fields") or 0
    if not fields:
        log.warning("token injection wrote no response field type=%s", info.captcha_type)
        return False
    log.info("token injected type=%s fields=%d callbacks=%s",
             info.captcha_type, fields, res.get("callbacks"))
    # Submitting is best-effort: many widgets auto-submit from the callback, and clicking a second
    # time would double-post. Only submit when no callback fired.
    if not res.get("callbacks"):
        _evaluate(c, base, user, tab_id, captcha_mod.SUBMIT_JS)
    return True


def verify_solved(c, base: str, user: str, tab_id: str) -> tuple[bool, dict]:
    """Did the solve actually work? Judged only on state the SITE controls.

    A filled response field is not evidence: we filled it. Reporting a solve off our own write is
    the difference between a crawler that knows it is blocked and one that stores challenge pages
    as documents."""
    v = _evaluate(c, base, user, tab_id, captcha_mod.VERIFY_JS)
    return captcha_mod.verified(v), (v if isinstance(v, dict) else {})


# ── Image-text captcha (distorted-text gate) — local VLM OCR ──────────────────
# ASP.NET CaptchaImage.axd and similar: a picture of characters + a text input gate the whole site.
# Not token-injectable — an OCR task. The local vision model (qwen2.5vl on the host) reads it for
# free; we type the answer and submit, in the C3 stealth browser the site actually lets through
# (headless Chromium gets soft-blocked to an error page even with the right answer). Grab via canvas,
# NOT a re-fetch of img.src: a BotDetect axd rotates the code on every request, which would desync the
# answer from the form. Sentinels: 'NONE' = no captcha element (past the gate), 'ERR...' = unreadable.
_IMG_TEXT_GRAB_JS = (
    "(() => { const img = document.querySelector("
    "'img[src*=\"captcha\" i], img[id*=\"captcha\" i], img[alt*=\"captcha\" i]'); if (!img) return 'NONE';"
    " try { const c=document.createElement('canvas'); c.width=img.naturalWidth||img.width;"
    " c.height=img.naturalHeight||img.height; if(!c.width||!c.height) return 'ERR:zero';"
    " c.getContext('2d').drawImage(img,0,0); return c.toDataURL('image/png'); }"
    " catch(e){ return 'ERR:'+((e&&e.message)||e); } })()"
)
_IMG_TEXT_SUBMIT_JS = (
    "(() => { const b=[...document.querySelectorAll('input[type=submit],button,input[type=button]')]"
    ".find(x=>/enter|submit|verify|continue|proceed|go\\b/i.test(x.value||x.innerText||''))"
    "|| document.querySelector('input[type=submit],button'); if(b){ b.click(); return true; } return false; })()"
)


def _ocr_image_text(data_url: str) -> str:
    """Read a distorted-text captcha with the local vision model (Ollama). Alphanumerics only."""
    import re

    import httpx
    ocr_base = os.environ.get("CRAWLER_OCR_URL", "http://host.docker.internal:11434").rstrip("/")
    model = os.environ.get("CRAWLER_OCR_MODEL", "qwen2.5vl:7b")
    b64 = data_url.split(",", 1)[1]
    prompt = ("This is a CAPTCHA image with distorted characters. Read the exact characters shown "
              "(letters and digits, case-sensitive). Reply with ONLY the characters, nothing else.")
    r = httpx.post(f"{ocr_base}/api/generate", timeout=_env_int("CRAWLER_OCR_TIMEOUT_S", 60),
                   json={"model": model, "prompt": prompt, "images": [b64],
                         "stream": False, "options": {"temperature": 0}})
    return re.sub(r"[^A-Za-z0-9]", "", (r.json().get("response") or ""))


def _solve_image_text(c, base: str, user: str, tab_id: str) -> bool:
    """OCR-solve a distorted-text captcha gate in the C3 tab, retrying a few times (OCR is not 100%).
    True once the captcha image is gone (we are past the gate). Best-effort; never raises."""
    for _ in range(_env_int("CRAWLER_OCR_MAX_TRIES", 3)):
        grab = _evaluate(c, base, user, tab_id, _IMG_TEXT_GRAB_JS)
        if grab == "NONE":
            return True                            # no captcha element => already inside
        if not isinstance(grab, str) or not grab.startswith("data:"):
            continue                               # image mid-load / unreadable — retry the grab
        try:
            answer = _ocr_image_text(grab)
        except Exception as exc:
            log.info("c3 image_text OCR call failed: %s", exc)
            return False
        if not answer:
            continue
        # Fill the captcha input then submit. The answer is inlined into the expression because the
        # C3 /evaluate endpoint takes an expression string, not bound arguments.
        fill = ("(() => { const i=document.querySelector("
                "'input[id*=\"captcha\" i], input[name*=\"captcha\" i]') "
                "|| document.querySelector('input[type=\"text\"], input:not([type])'); if(!i) return false;"
                " i.focus(); i.value=" + json.dumps(answer) + ";"
                " i.dispatchEvent(new Event('input',{bubbles:true}));"
                " i.dispatchEvent(new Event('change',{bubbles:true})); return true; })()")
        if not _evaluate(c, base, user, tab_id, fill):
            return False
        _evaluate(c, base, user, tab_id, _IMG_TEXT_SUBMIT_JS)
        _settle(c, base, user, tab_id)
        if _evaluate(c, base, user, tab_id, _IMG_TEXT_GRAB_JS) == "NONE":   # gate gone => solved
            log.info("c3 image-text captcha solved answer=%s", answer)
            return True
    return False


def solve_captcha(c, base: str, user: str, tab_id: str, page_url: str,
                  use_buster: bool = True, commercial_fallback: bool = True) -> CaptchaInfo:
    """Detect a captcha and beat it by whatever means actually applies to that kind.

    Routing by solvability matters for cost as much as correctness. A Cloudflare managed challenge
    looks superficially like Turnstile — same response input, same script — but its token is minted
    in-session and validated against the fingerprint and cf_clearance cookie, so a per-sitekey token
    bought from a solver cannot satisfy it, and the sitekey is not extractable from the DOM anyway.
    Sending one to a paid solver spends money on a token that provably cannot work. So:

      wait_only     -> the render already waits it out; report it, never pay for it
      self_solving  -> proof-of-work, finishes on its own; just wait
      not_injectable-> no DOM sink the site reads (reCAPTCHA v3); report honestly
      otherwise     -> Buster (free, reCAPTCHA only), then the commercial solver

    A solve is confirmed against the page afterwards — see verify_solved."""
    info = detect_captcha(c, base, user, tab_id)
    if not info.captcha_type:
        return info

    log.info("captcha detected type=%s sitekey=%s solvability=%s blocking=%s url=%s",
             info.captcha_type, info.sitekey, info.solvability, info.blocking, page_url)

    if info.solvability in ("wait_only", "self_solving"):
        # _settle already polls these out; if we are here it did not clear inside its budget.
        # A wait-only wall that never clears is usually the browser's fingerprint being rejected, so
        # check it before giving up — one extra evaluate on a path that has already failed, and it
        # turns "captcha not solved" into a specific, fixable cause.
        info.error = f"{info.captcha_type}: {info.solvability} — no token applies, waited instead"
        flaws = captcha_mod.fingerprint_flaws(
            _evaluate(c, base, user, tab_id, captcha_mod.FINGERPRINT_JS))
        if flaws:
            info.metadata["fingerprint_flaws"] = flaws
            info.error += f" | fingerprint rejected: {flaws[0]}"
            log.warning("c3 fingerprint is incoherent, which is why %s never cleared: %s",
                        info.captcha_type, "; ".join(flaws))
        return info
    if info.solvability == "not_injectable":
        info.error = f"{info.captcha_type}: no injectable response field on this page"
        return info

    # Distorted-text gate: read it with the local vision model and type it in-tab. Free (no provider),
    # and it runs in the stealth browser that the site actually lets through. Gate: CRAWLER_CAPTCHA_OCR.
    if info.captcha_type == "image_text":
        if os.environ.get("CRAWLER_CAPTCHA_OCR", "1") == "1" and _solve_image_text(c, base, user, tab_id):
            info.solved = True
            info.solver = "local_vlm_ocr"
            info.cost_usd = 0.0
            return info
        info.error = "image_text: local OCR did not clear the gate"
        return info

    timeout_ms = _env_int("CAMOFOX_SOLVE_MAX_WAIT_MS", 60000)

    if use_buster and info.captcha_type in ("recaptcha_v2", "recaptcha_v2_invisible"):
        buster_info = _solve_with_buster(c, base, user, tab_id, timeout_ms)
        if buster_info.solved:
            ok, _ = verify_solved(c, base, user, tab_id)
            if ok:
                return buster_info
            info.error = "buster reported solved but the page is still walled"
        else:
            info.error = buster_info.error or "buster failed"

    if commercial_fallback:
        if not info.sitekey and info.captcha_type != "datadome":
            # Every solver task is keyed on the sitekey. Submitting without one burns an API call
            # and always fails, so stop here and say why.
            info.error = f"{info.captcha_type}: no sitekey found; cannot build a solver task"
            return info
        commercial_info = _solve_with_commercial(info, page_url)
        if commercial_info.solved:
            if not _inject_token(c, base, user, tab_id, commercial_info):
                commercial_info.solved = False
                commercial_info.error = "token bought but could not be injected"
                return commercial_info
            _settle(c, base, user, tab_id)
            ok, detail = verify_solved(c, base, user, tab_id)
            if not ok:
                # Do NOT report success off our own token write. The cost is still recorded — it
                # was really spent — but the page is not treated as retrieved.
                commercial_info.solved = False
                commercial_info.error = f"token injected but unverified: {detail.get('reasons')}"
            return commercial_info
        info.error = commercial_info.error or info.error

    return info


def _env_int(k: str, d: int) -> int:
    try:
        return int(os.environ.get(k, str(d)))
    except ValueError:
        return d


# Non-interactive challenge/block markers — if still present after the first settle, wait once more.
_CHALLENGE_MARKERS = ("just a moment", "challenge-platform", "cf-chl", "checking your browser",
                      "attention required", "_incapsula_resource", "pardon our interruption",
                      "please enable js", "captcha-delivery", "verifying you are human", "please verify")


def _swallow_post(c, url: str, body: dict, timeout: float = 8.0) -> None:
    """Fire-and-forget POST. The timeout is NOT optional politeness: without it these inherit the
    caller's render timeout, so an endpoint that hangs server-side burns the whole budget while
    the exception handler hides that it happened."""
    try:
        c.post(url, json=body, timeout=timeout)
    except Exception:
        pass


# Scroll to the bottom and report the document height, so the caller can tell whether the page
# grew (lazy-load / infinite feed) between passes.
_BEHAVIOR_JS = """(() => { try {
  window.scrollTo(0, document.body.scrollHeight);
  return document.body.scrollHeight;
} catch (e) { return -1; } })()"""
_SCROLL_TOP_JS = "(() => { try { window.scrollTo(0,0); return 1; } catch(e){ return -1; } })()"


def _behave(c, base: str, user: str, tab_id: str) -> None:
    """Scroll the page so lazy-loaded content actually renders.

    Uses /evaluate, NOT the mouse-driven /scroll and /click endpoints. Those are Playwright
    mouse.wheel / mouse.click, and MEASURED against the live container they return HTTP 500 after
    a 30s server-side timeout under Camoufox's humanize:true. Three of the four calls in the old
    sequence failed that way — 110s burned per C3 render on example.com — and _swallow_post
    discarded every error, so it was invisible AND the page never scrolled.

    The old sequence existed to emit trusted input events for behavioural anti-bot gates. A scroll
    that 500s emits nothing, so that value was already zero; this trades a pretence of trusted
    events for a scroll that actually happens, in about a second. If trusted-event behaviour is
    wanted again it needs the vendor mouse endpoints fixed first, not another swallowed call.

    Bounded by CRAWLER_CAMOFOX_SCROLL_PASSES: an infinite feed never stops growing, so the cap is
    what keeps this a page-load step rather than a crawl of its own. Stops early once the document
    height stops changing, which is the common case and costs one extra evaluate."""
    passes = _env_int("CRAWLER_CAMOFOX_SCROLL_PASSES", 3)
    last = -1
    try:
        for _ in range(max(1, passes)):
            e = c.post(f"{base}/tabs/{tab_id}/evaluate",
                       json={"userId": user, "expression": _BEHAVIOR_JS},
                       headers=_headers(), timeout=15.0)
            height = (e.json() or {}).get("result") if e.status_code < 400 else None
            if not isinstance(height, (int, float)) or height == last:
                break                       # nothing new appeared; stop paying for passes
            last = height
            time.sleep(float(os.environ.get("CRAWLER_CAMOFOX_SCROLL_WAIT_S", "0.8")))
        c.post(f"{base}/tabs/{tab_id}/evaluate",
               json={"userId": user, "expression": _SCROLL_TOP_JS},
               headers=_headers(), timeout=10.0)
    except Exception:
        pass


def _looks_challenged(c, base: str, user: str, tab_id: str) -> bool:
    """Best-effort: does the settled DOM still look like a challenge/block interstitial?"""
    try:
        e = c.post(f"{base}/tabs/{tab_id}/evaluate",
                   json={"userId": user,
                         "expression": "(document.title+' '+(document.body?document.body.innerText:'')).slice(0,600)"},
                   headers=_headers())
        if e.status_code < 400:
            t = (e.json().get("result") or "").lower()
            return any(m in t for m in _CHALLENGE_MARKERS)
    except Exception:
        pass
    return False


# The Turnstile widget slot: a ~200-500px wide, ~55-90px tall container sitting near the top of the
# challenge page. Its interior (the checkbox) is a cross-origin challenges.cloudflare.com iframe no
# selector can reach, but the slot's own box gives us the pixel to aim at.
# No upper width bound: the challenge container is often full-column-width (~896px measured), not the
# ~300px of the visible widget — the checkbox still renders at the container's LEFT edge, so we aim at
# x+30 regardless of width. The distinctive filter is the height band (a Turnstile slot is ~55-95px)
# near the top of the page. Capping width at ~560 (an earlier mistake) filtered the real slot out and
# the click never fired.
_CF_WIDGET_JS = (
    "(() => { const c=[...document.querySelectorAll('div')].map(e=>{const r=e.getBoundingClientRect();"
    "return {x:r.x,y:r.y,w:r.width,h:r.height};})"
    ".filter(b=>b.h>=55&&b.h<=95&&b.w>=180&&b.y>120&&b.y<460); c.sort((a,b)=>a.y-b.y);"
    "const s=c[0]; return s?{x:Math.round(s.x),y:Math.round(s.y),h:Math.round(s.h)}:null; })()"
)


def _click_turnstile_checkbox(c, base: str, user: str, tab_id: str) -> bool:
    """Click the Cloudflare "Verify you are human" checkbox by pixel coordinate.

    An interactive Turnstile challenge never self-clears — it waits for a click — and the checkbox
    lives in a cross-origin iframe that page.locator() cannot address, so a selector click is
    impossible. A TRUSTED humanized coordinate click on it does clear the challenge (verified live on
    scrapingcourse.com/antibot-challenge: "You bypassed the Antibot challenge!"). Requires the /click
    coordinate patch in the CamoFox server; on an unpatched server the /click returns 400 and this is
    a harmless no-op. Gate: CRAWLER_CAMOFOX_CF_CLICK=0 disables it."""
    try:
        e = c.post(f"{base}/tabs/{tab_id}/evaluate",
                   json={"userId": user, "expression": _CF_WIDGET_JS}, headers=_headers())
        if e.status_code >= 400:
            return False
        slot = e.json().get("result")
        if not isinstance(slot, dict):
            return False
        cx = int(slot["x"]) + 30            # the checkbox sits ~30px in from the widget's left edge
        cy = int(slot["y"] + slot["h"] / 2)
        r = c.post(f"{base}/tabs/{tab_id}/click",
                   json={"userId": user, "coordinates": {"x": cx, "y": cy}})
        ok = r.status_code < 400 and isinstance(r.json(), dict) and r.json().get("clicked") is True
        if ok:
            log.info("clicked turnstile checkbox at (%d,%d)", cx, cy)
        return ok
    except Exception:
        return False


def _settle(c, base: str, user: str, tab_id: str) -> None:
    """Post-navigation settle so C3 captures the REAL page, not a pre-challenge / half-rendered DOM:
      1. /wait  — networkidle + hydration poll (lets a Cloudflare/Imperva JS challenge auto-solve),
      2. humanized scroll + a body click — TRUSTED mouse/scroll events for behavioral gates
         (CamoFox's humanized cursor traces a curved path -> many isTrusted mousemove events),
      3. if a challenge marker is still present, KEEP waiting until it clears or the budget runs out.
    All best-effort; never raises. Tunable: CRAWLER_CAMOFOX_SETTLE_MS, CRAWLER_CAMOFOX_BEHAVIOR,
    CRAWLER_CAMOFOX_CHALLENGE_MS."""
    settle_ms = _env_int("CRAWLER_CAMOFOX_SETTLE_MS", 8000)
    try:
        c.post(f"{base}/tabs/{tab_id}/wait",
               json={"userId": user, "timeout": settle_ms, "waitForNetwork": True})
    except Exception:
        pass
    if os.environ.get("CRAWLER_CAMOFOX_BEHAVIOR", "1") == "1":
        _behave(c, base, user, tab_id)
    # Cloudflare's managed challenge ("Just a moment...") resolves itself, but not always inside one
    # extra wait — settle_ms*2 was the whole budget, so a render returned the INTERSTITIAL and the
    # page was recorded as blocked. Measured on scrapingcourse.com/antibot-challenge: every attempt
    # came back in ~16.5s with the challenge still up, identical at a 90s or 150s caller timeout,
    # because the caller's timeout never governed this loop. Poll until it clears or the budget ends.
    budget_ms = _env_int("CRAWLER_CAMOFOX_CHALLENGE_MS", 45000)
    cf_click = os.environ.get("CRAWLER_CAMOFOX_CF_CLICK", "1") == "1"
    clicks = 0
    # Budget against the WALL CLOCK, not against an assumed cost per iteration. /wait returns as soon
    # as the network idles — on a challenge page that is ~230ms, not settle_ms — so the old
    # `waited += settle_ms` booked 8s of credit for 0.2s of real waiting and burned the whole 45s
    # budget in ~1.3s. Measured on dzone.com: five iterations inside 1.2s, then the render gave up
    # and reported the interstitial, even though the challenge clears on its own at ~10s.
    started_at = time.monotonic()
    deadline = started_at + budget_ms / 1000.0
    # Don't click straight away. Measured on zoopla.co.uk: a click dispatched in the challenge's first
    # seconds BLOCKS in the browser's input path and only fails at the 30s handler timeout — burning
    # most of the settle budget for nothing. The same click at t+25s returns in 1.2s. So let the page
    # settle first; a passive challenge often clears on its own inside that window anyway, and an
    # interactive one still gets its clicks with budget to spare.
    click_after = _env_int("CRAWLER_CAMOFOX_CLICK_AFTER_S", 15)
    while time.monotonic() < deadline and _looks_challenged(c, base, user, tab_id):
        # A NON-interactive challenge clears on its own; an INTERACTIVE Turnstile ("Verify you are
        # human") does not — it needs the checkbox clicked. Try that up to twice; a no-op if the
        # widget isn't found or the server lacks the coordinate-click patch.
        if (cf_click and clicks < 2 and time.monotonic() - started_at >= click_after
                and _click_turnstile_checkbox(c, base, user, tab_id)):
            clicks += 1
        started = time.monotonic()
        try:
            c.post(f"{base}/tabs/{tab_id}/wait",
                   json={"userId": user, "timeout": settle_ms, "waitForNetwork": True})
        except Exception:
            break                       # tab/server gone — nothing to wait for
        # An early return means the page went idle while still challenged; sleep the remainder so the
        # challenge's own JS actually gets time to run instead of spinning the budget away.
        left = (settle_ms / 1000.0) - (time.monotonic() - started)
        if left > 0:
            time.sleep(min(left, max(0.0, deadline - time.monotonic())))


def health() -> bool:
    """True if the CamoFox server answers /health (only meaningful when enabled)."""
    if not enabled():
        return False
    import httpx
    try:
        return httpx.get(f"{_base()}/health", timeout=3.0).status_code == 200
    except Exception:
        return False


def _flatten(node, out: list[str]) -> None:
    """Collect human-readable strings from a CamoFox accessibility snapshot — a
    JSON tree of {role, name, children} (shape may drift, so we pull the common
    text fields and recurse into anything nested)."""
    if isinstance(node, str):
        s = node.strip()
        if s:
            out.append(s)
    elif isinstance(node, dict):
        for k in ("name", "text", "value", "label"):
            v = node.get(k)
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        for k, v in node.items():
            if k in ("name", "text", "value", "label"):
                continue
            if isinstance(v, (dict, list)):
                _flatten(v, out)
    elif isinstance(node, list):
        for it in node:
            _flatten(it, out)


def _snapshot_text(snapshot) -> str:
    """A11y snapshot -> plain text (one line per node), collapsing adjacent
    duplicate lines (nav items repeat across the tree)."""
    if isinstance(snapshot, str):
        return snapshot.strip()
    out: list[str] = []
    _flatten(snapshot, out)
    lines: list[str] = []
    for s in out:
        if not lines or lines[-1] != s:
            lines.append(s)
    return "\n".join(lines)


def render(url: str, timeout_s: float = 45.0, solve_captchas: bool = False,
           user: str | None = None, base_url: str | None = None) -> dict | None:
    """Open URL in CamoFox and capture EVERYTHING the page yields, so a C3 doc is
    as rich as a C1 one. Returns {text, html, screenshot, snapshot, final_url} or
    None. Steps (one fresh tab/context):
      - snapshot   -> aria text (LLM-ready fallback body)
      - evaluate   -> document.documentElement.outerHTML = the REAL rendered HTML
                      (needs CAMOFOX_API_KEY Bearer; enables downstream image/pdf/
                      media/video extraction the same as a live fetch)
      - screenshot -> full-page PNG bytes
    The tab is always torn down. Synchronous (httpx) — call via asyncio.to_thread."""
    if not enabled():
        return None
    import httpx

    # base_url routes this render at a DIFFERENT CamoFox instance — used to escalate to the paid
    # (proxied) container after the free one is refused. Defaults to the free instance.
    # A caller-supplied user forces a FRESH CamoFox context (⇒ a fresh proxy session/IP under a
    # rotating upstream) — used by the C3 fresh-IP retry after an IP-block. Default: rotating pool.
    user = user or _next_user()     # rotating pool userId (bounded live sessions; see _next_user)
    # Resolved AFTER the userId exists, because the pool is pinned by userId (see _base_for).
    # An explicit base_url still wins - that is the paid-instance escalation.
    base = (base_url or _base_for(user)).rstrip("/")
    tab_id = None
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as c:
            # POST /tabs requires userId + sessionKey; it navigates and returns {tabId, url}.
            r = c.post(f"{base}/tabs",
                       json={"userId": user, "sessionKey": _SESSION, "url": url})
            if r.status_code >= 400 or not r.content:
                return None
            data = r.json()
            tab_id = data.get("tabId") or data.get("id") or data.get("targetId")
            if not tab_id:
                return None
            # Settle: let JS challenges auto-solve + drive trusted input for behavioral gates BEFORE
            # capture (a bare domcontentloaded snapshot grabs the challenge/half-rendered DOM).
            _settle(c, base, user, tab_id)

            # Optional captcha solving for interactive widgets (reCAPTCHA via Buster,
            # other widgets via commercial solver). Captured after the first settle so
            # the widget has loaded; if a solve succeeds, settle again to let the page load.
            captcha_info: CaptchaInfo = CaptchaInfo()
            if solve_captchas and os.environ.get("CAMOFOX_SOLVE_RECAPTCHA", "1") == "1":
                captcha_info = solve_captcha(c, base, user, tab_id, url)
                if captcha_info.solved:
                    _settle(c, base, user, tab_id)

            # GET snapshot requires ?userId=; the aria snapshot text is in .snapshot.
            s = c.get(f"{base}/tabs/{tab_id}/snapshot", params={"userId": user})
            payload = {}
            if s.status_code < 400 and s.content:
                ctype = s.headers.get("content-type", "")
                payload = s.json() if ctype.startswith("application/json") else {"snapshot": s.text}

            # Raw rendered HTML (best-effort; needs the Bearer key). This is what lets
            # enrich_assets pull images / PDFs / media / video links downstream.
            html = ""
            try:
                e = c.post(f"{base}/tabs/{tab_id}/evaluate",
                           json={"userId": user, "expression": "document.documentElement.outerHTML"},
                           headers=_headers())
                if e.status_code < 400:
                    res = e.json().get("result")
                    if isinstance(res, str):
                        html = res
            except Exception:
                pass

            # The REAL HTTP status. POST /tabs reports only {tabId, url}, so without this every
            # render looked like a success: a 404 error page was stored as a document, and because
            # no failure was ever recorded, is_gone() could not fire and the dead URL came back on
            # every run. Firefox carries the status on the navigation timing entry.
            status = None
            try:
                st = c.post(f"{base}/tabs/{tab_id}/evaluate",
                            json={"userId": user, "expression":
                                  "performance.getEntriesByType('navigation')[0].responseStatus"},
                            headers=_headers())
                if st.status_code < 400:
                    v = st.json().get("result")
                    # 0 means "the browser could not tell us" (cross-origin, cache, no entry) —
                    # that is unknown, not success, so it stays None rather than becoming a 200.
                    if isinstance(v, (int, float)) and 100 <= int(v) <= 599:
                        status = int(v)
            except Exception:
                pass

            # Full-page screenshot (PNG bytes; no auth).
            shot = None
            try:
                sc = c.get(f"{base}/tabs/{tab_id}/screenshot",
                           params={"userId": user, "fullPage": "true"})
                if sc.status_code < 400 and sc.content:
                    shot = sc.content
            except Exception:
                pass

        raw = payload.get("snapshot") if isinstance(payload, dict) else payload
        text = _snapshot_text(raw)
        if not text.strip() and not html.strip():
            return None                 # nothing usable came back
        page_url = payload.get("url") if isinstance(payload, dict) else None
        result = {"text": text, "html": html, "screenshot": shot,
                "snapshot": raw, "final_url": page_url or url, "status": status}
        if solve_captchas:
            result["captcha_info"] = captcha_info
        return result
    except Exception:
        log.info("camofox render failed url=%s", url, exc_info=True)
        return None
    finally:
        if tab_id:
            try:                        # tear down the tab (frees the page immediately)
                with httpx.Client(timeout=5.0) as c:
                    c.delete(f"{base}/tabs/{tab_id}", params={"userId": user})
            except Exception:
                pass


def render_with_solver(url: str, timeout_s: float = 90.0,
                       user: str | None = None,
                       base_url: str | None = None) -> tuple[dict | None, CaptchaInfo]:
    """Render a URL through CamoFox with captcha solving enabled. Returns
    (snapshot_dict, captcha_info). snapshot_dict is None on failure. ``user`` forces a fresh
    session (fresh proxy IP) — see render()."""
    snap = render(url, timeout_s=timeout_s, solve_captchas=True, user=user, base_url=base_url)
    if snap is None:
        return None, CaptchaInfo()
    info = snap.pop("captcha_info", None) or CaptchaInfo()
    return snap, info


def fetch_bytes(url: str, timeout_s: float = 45.0) -> bytes | None:
    """Fetch a WAF-blocked asset (image/PDF) through the stealth browser and return its
    RAW BYTES — the last-resort path when even curl_cffi's Chrome fingerprint is 403'd
    (Akamai). Opens the asset URL as a tab, then evaluates an in-page fetch() that reads
    the response as base64 (an arrayBuffer collapses to {} through Playwright's JSON
    serialization; a base64 string survives). Needs CAMOFOX_API_KEY (the evaluate route is
    Bearer-gated). Returns None on any error / when disabled. Synchronous — call via
    asyncio.to_thread. ponytail: base64 over the wire is ~33% bloat but it's the only
    serialization the evaluate endpoint supports; fine for the rare blocked asset."""
    if not enabled():
        return None
    import base64
    import httpx

    user = _next_user()
    base = _base_for(user)
    tab_id = None
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=True) as c:
            r = c.post(f"{base}/tabs",
                       json={"userId": user, "sessionKey": _SESSION, "url": url})
            if r.status_code >= 400 or not r.content:
                return None
            tab_id = (r.json() or {}).get("tabId") or (r.json() or {}).get("id")
            if not tab_id:
                return None
            # In-page fetch reads the asset from the SAME browser session that just loaded it
            # (same cookies/fingerprint that passed the WAF), then hands back base64.
            expr = (
                "(async()=>{const r=await fetch(" + _js_str(url) + ",{credentials:'include'});"
                "if(!r.ok)return null;const b=await r.arrayBuffer();"
                "let s='',a=new Uint8Array(b);for(let i=0;i<a.length;i++)s+=String.fromCharCode(a[i]);"
                "return btoa(s);})()"
            )
            e = c.post(f"{base}/tabs/{tab_id}/evaluate",
                       json={"userId": user, "expression": expr}, headers=_headers())
            if e.status_code >= 400:
                return None
            res = (e.json() or {}).get("result")
            if not isinstance(res, str) or not res:
                return None
            return base64.b64decode(res)
    except Exception:
        log.info("camofox fetch_bytes failed url=%s", url, exc_info=True)
        return None
    finally:
        if tab_id:
            try:
                with httpx.Client(timeout=5.0) as c:
                    c.delete(f"{base}/tabs/{tab_id}", params={"userId": user})
            except Exception:
                pass


def _js_str(s: str) -> str:
    """JSON-encode a Python string into a safe JS string literal (handles quotes/backslashes
    in the URL — a query string with an apostrophe would otherwise break the expression)."""
    import json
    return json.dumps(s)


if __name__ == "__main__":   # python -m crawler.camofox_client  (offline self-check)
    # Snapshot flattening is the only non-trivial logic; verify it without a server.
    tree = {"role": "document", "name": "K9 Vajra page",
            "children": [{"role": "heading", "name": "K9 Vajra-T delivered"},
                         {"role": "text", "text": "L&T handed over the howitzer."},
                         {"role": "nav", "children": [{"name": "Home"}, {"name": "Home"}]}]}
    txt = _snapshot_text(tree)
    assert "K9 Vajra-T delivered" in txt and "L&T handed over the howitzer." in txt
    assert txt.count("Home") == 1, f"adjacent dupes not collapsed: {txt!r}"
    assert _snapshot_text("raw yaml-ish text") == "raw yaml-ish text"

    # userId pool rotates round-robin within a bounded set (bounds live sessions → no OOM).
    os.environ["CRAWLER_CAMOFOX_POOL"] = "4"
    users = [_next_user() for _ in range(9)]
    assert set(users) == {"crawler-0", "crawler-1", "crawler-2", "crawler-3"}, users
    assert users[0] != users[1], "should rotate, not repeat"

    # _js_str produces a valid JS/JSON string literal even with quotes in the URL.
    assert _js_str("http://x/a'b\"c") == '"http://x/a\'b\\"c"', _js_str("http://x/a'b\"c")
    print("OK — camofox snapshot flattener + userId pool self-check passed")

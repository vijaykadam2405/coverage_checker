"""Edge-case harness — drive every corpus URL through the crawler's own detector and score it.

    python edgecases/run.py                 # all categories
    python edgecases/run.py --only ssl_error,soft_404
    python edgecases/run.py --limit 3       # smoke test, 3 URLs per category

Reads  edgecases/corpus/*.json   (a flat list of {category, url, expect, evidence, ...})
Writes edgecases/results.jsonl   (one row per URL: what we observed + what diagnose() said)

WHAT THIS PROVES, AND WHAT IT DOES NOT
--------------------------------------
Pass 1 fetches each URL over plain HTTP with the crawler's user agent — the C1 tier, where the
crawler makes its first decision — and asks ``crawler.edgecase.diagnose()`` to name the obstacle.
A row PASSES when the detected case matches the category the URL was collected for. That is a real
test of the detector: the corpus label is a hypothesis, the measured response is the fact, and a
mismatch is reported as a mismatch rather than quietly relabelled.

Three categories cannot be judged from a single HTTP response, because the whole point of them is
that the first response is not the final content: ``js_render``, ``lazy_loading`` and
``infinite_scroll``. Those are marked ``needs_render`` in pass 1 and settled by ``--render``, which
drives the real CamoFox service on :9377 and compares text length before and after. Pagination is
likewise a frontier property, not a page property; ``--crawl`` handles it via the crawler API.

SSL VERIFICATION STAYS ON. The ssl_error rows only mean something if the handshake is actually
allowed to fail, so this harness never passes verify=False — same rule the crawler follows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from crawler import edgecase as ec  # noqa: E402
from crawler.async_engine import (  # noqa: E402  (the shipped code paths, not copies)
    _INPAGE_CLICK_JS, _LAZY_RESOLVE_JS, _LOADMORE_JS, _SCROLL_DEEPEST_JS,
)

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"
RESULTS = HERE / "results.jsonl"

# ── redaction: results.jsonl is a PUBLIC artefact ────────────────────────────
# The evidence strings quote real response bodies and headers verbatim, which is what makes them
# trustworthy — and also means a run captures third-party anti-bot SESSION COOKIES (DataDome,
# Incapsula, PerimeterX, Akamai, cf_clearance) and captcha sitekeys straight into a committed file.
# This was previously scrubbed BY HAND, so every regeneration silently undid it and one hCaptcha
# key reached GitHub and blocked a push. Doing it at the single write site means it cannot regress.
#
# Cookie VALUES are live credentials; the NAME is the evidence, so the name is kept. Sitekeys are
# public by design but GitHub's push protection does not agree, and the corpus proves detection
# without them.
import re as _re

_REDACTIONS = (
    (_re.compile(r"(?i)\b(datadome|incap_ses_\w+|visid_incap_\w+|_px\w*|ak_bmsc|bm_sv|bm_sz|"
                 r"cf_clearance|__cf_bm|reese84|kpsdk\w*)=[^;\s\"']{8,}"), r"\1=<redacted>"),
    # Bounded on BOTH sides: a reCAPTCHA site key is exactly 40 chars and stands alone in an
    # attribute. Without the lookarounds this fired inside base64 — "6Ly9" is just how "://"
    # encodes — and shredded OpenAlex's pagination cursors, which are the evidence for those rows.
    # Over-redaction is not free: it destroys the measurement the corpus exists to record.
    (_re.compile(r"(?<![0-9A-Za-z_-])6L[0-9A-Za-z_-]{38}(?![0-9A-Za-z_-])"),
     "<recaptcha-sitekey-redacted>"),
    (_re.compile(r"(?<![0-9A-Za-z_-])0x4[0-9A-Za-z]{20,}(?![0-9A-Za-z_-])"),
     "<turnstile-sitekey-redacted>"),
    (_re.compile(r"(?i)(sitekey[\"'=: ]{1,6})[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                 r"[0-9a-f]{4}-[0-9a-f]{12}"), r"\1<hcaptcha-sitekey-redacted>"),
)


def redact(value):
    """Strip session credentials and sitekeys from anything on its way into results.jsonl."""
    if isinstance(value, str):
        for pat, sub in _REDACTIONS:
            value = pat.sub(sub, value)
        return value
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
CAMOFOX = os.environ.get("CRAWLER_CAMOFOX_URL", "http://localhost:9377")

# corpus category -> the edgecase.CASE_* the detector should return.
EXPECTED = {
    "dns_failure": ec.CASE_DNS, "timeout": ec.CASE_TIMEOUT, "ssl_error": ec.CASE_SSL,
    "conn_error": ec.CASE_CONN,
    "rate_limit": ec.CASE_RATE_LIMIT, "geo_block": ec.CASE_GEO_BLOCK,
    "antibot": ec.CASE_ANTIBOT, "captcha_image_text": ec.CASE_CAPTCHA,
    "recaptcha": ec.CASE_CAPTCHA, "hcaptcha": ec.CASE_CAPTCHA,
    "auth_wall": ec.CASE_AUTH_WALL, "paywall": ec.CASE_PAYWALL,
    "consent_wall": ec.CASE_CONSENT_WALL, "soft_404": ec.CASE_SOFT_404,
    "redirect_loop": ec.CASE_REDIRECT_LOOP, "calendar_trap": ec.CASE_CALENDAR_TRAP,
    "url_space_loop": ec.CASE_URL_SPACE_LOOP,
    "js_render": ec.CASE_JS_RENDER,
}
# Settled by --render / --crawl, not by a single response.
RENDER_ONLY = {"js_render", "lazy_loading", "infinite_scroll"}
CRAWL_ONLY = {"pagination"}
# An anti-bot wall in front of a captcha page is a CORRECT call, not a miss — the crawler is
# blocked either way and escalates the same. Accept these as equivalent rather than scoring a
# detector failure for being right about a different layer of the same block.
ALSO_OK = {ec.CASE_CAPTCHA: {ec.CASE_ANTIBOT}, ec.CASE_ANTIBOT: {ec.CASE_CAPTCHA},
           ec.CASE_GEO_BLOCK: {ec.CASE_ANTIBOT}, ec.CASE_AUTH_WALL: {ec.CASE_ANTIBOT},
           # Same rule, applied consistently: a Cloudflare challenge or a login page sitting in
           # FRONT of a consent gate is a correct call about a different layer of the same block,
           # not a miss. Measured: absolut/researchgate/quora/udemy answer a CF challenge before
           # any CMP runs, and zoom.us/signin genuinely is a login page.
           ec.CASE_CONSENT_WALL: {ec.CASE_ANTIBOT, ec.CASE_AUTH_WALL}}
# Cases where the obstacle is decided per-request (IP reputation, time of day, rate) rather than
# being a fixed property of the URL, so a clean fetch means the wall did not reproduce today.
WALL_CASES = {ec.CASE_ANTIBOT, ec.CASE_CAPTCHA, ec.CASE_GEO_BLOCK, ec.CASE_RATE_LIMIT}
# Categories whose entire subject is a transport failure — there the exception IS the measurement,
# so they keep being scored on naming it correctly.
_TRANSPORT_CASES = {ec.CASE_DNS, ec.CASE_SSL, ec.CASE_CONN, ec.CASE_TIMEOUT}
# Cases decided by reading the page BODY, which a JS shell simply does not contain yet.
_BODY_DRIVEN = {ec.CASE_SOFT_404, ec.CASE_PAYWALL, ec.CASE_AUTH_WALL, ec.CASE_CONSENT_WALL}


def load_corpus(only: set[str] | None, limit: int | None) -> list[dict]:
    rows, seen = [], set()
    for f in sorted(CORPUS.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ! skipping {f.name}: {exc}", file=sys.stderr)
            continue
        for r in data if isinstance(data, list) else data.get("urls", []):
            cat, url = r.get("category"), r.get("url")
            if not cat or not url or (only and cat not in only) or (cat, url) in seen:
                continue
            seen.add((cat, url))
            r["_file"] = f.name
            rows.append(r)
    if limit:
        per: dict[str, int] = {}
        keep = []
        for r in rows:
            c = r["category"]
            if per.get(c, 0) < limit:
                per[c] = per.get(c, 0) + 1
                keep.append(r)
        rows = keep
    return rows


async def probe(client: httpx.AsyncClient, row: dict, timeout_s: float) -> dict:
    """One fetch + one diagnosis. Never raises: a transport failure IS the measurement."""
    # For a calendar trap the corpus `url` is the LEGITIMATE calendar page — which the crawler
    # should fetch happily — and `far_future_url` is the trap the pager walks into. Testing the
    # base page would assert the opposite of what we want, so the trap URL is the subject here.
    row = dict(row)
    if row.get("far_future_url"):
        row["url"] = row["far_future_url"]
    url = row["url"]
    # A corpus row may pin its own budget. Needed for the deliberate-delay endpoints: a /delay/10
    # service is only a timeout against a budget under 10s, and testing it at the default 20s
    # measures nothing except that the service works. The row states the budget it is a case for.
    timeout_s = float(row.get("timeout_s") or timeout_s)
    t0 = time.monotonic()
    status = headers = body = final = err = None
    redirects = 0
    chain: list[str] = []
    try:
        r = await client.get(url, timeout=timeout_s, follow_redirects=True,
                             headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        status, headers, final = r.status_code, dict(r.headers), str(r.url)
        redirects = len(r.history)
        # The CHAIN, not just its length. A count cannot express "hop 9 pointed back at hop 4",
        # which is what a cookie-less ping-pong looks like, so no detector rule about a repeated
        # URL can ever be written while this is thrown away. Recorded on the row rather than passed
        # to diagnose(), because diagnose's signature is shared with the production fetcher, which
        # has no chain to give it.
        chain = [f"{h.status_code} {h.headers.get('location', '')[:120]}" for h in r.history]
        ctype = r.headers.get("content-type", "")
        # JSON counts as a body. Several auth walls answer a plain GET with a JSON error rather
        # than a login page — slack.com/api/auth.test returns 200 {"ok":false,"error":"not_authed"}
        # — and a text/*-only filter recorded that as zero bytes, i.e. as nothing to diagnose.
        body = r.text if (not ctype or "text" in ctype or "json" in ctype
                          or "xml" in ctype) else ""
    except httpx.TooManyRedirects as exc:
        err, redirects = str(exc), 99          # the cap itself is the finding
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    elapsed = round(time.monotonic() - t0, 2)

    v = ec.diagnose(url, status=status, headers=headers, body=body, final_url=final,
                    redirects=redirects, elapsed_s=elapsed, error=err)
    cat = row["category"]
    exp = EXPECTED.get(cat)
    # A body-driven verdict cannot be reached through a JS shell. When diagnose returns js_render
    # for one of these, that is not a wrong answer — it is "the HTML does not contain the page
    # yet", and the crawler's own response is to escalate to the render tier and look again. So
    # the harness does the same, instead of scoring the shell. Measured on pinterest.com's 404:
    # 1,092,367 bytes of HTML yielding 193 chars of text.
    if (exp in _BODY_DRIVEN and v.case == ec.CASE_JS_RENDER and cat not in RENDER_ONLY):
        html = await _render_html(client, url)
        if html:
            rv = ec.diagnose(url, status=status, headers=headers, body=html, final_url=final,
                             redirects=redirects, elapsed_s=elapsed, error=err)
            if rv.case != ec.CASE_JS_RENDER:
                v, body = rv, html
    text_chars = len(ec.visible_text(body))
    if cat in RENDER_ONLY and v.case != exp:
        verdict = "needs_render"
    elif cat in CRAWL_ONLY:
        verdict = "needs_crawl"
    elif exp is None:
        verdict = "unmapped"
    elif v.case == exp or v.case in ALSO_OK.get(exp, ()):
        verdict = "pass"
    elif err and exp not in _TRANSPORT_CASES:
        # The request never returned a page, so this row measured nothing about the obstacle it was
        # collected for — and diagnose() was RIGHT about what it did see. Every other probe in this
        # file already says so (status_probe, broken_html_probe and domain_change_probe all bucket a
        # transport failure as `error`); the generic probe was the only one scoring it as a wrong
        # answer. Measured 2026-07-27: 13 of the 50 consent_wall rows returned
        # CERTIFICATE_VERIFY_FAILED, and `openssl s_client` shows the certificate issued by
        # "CN=Sophos SSL CA…, O=Sophos" — a filtering middlebox on the MEASURING network, confirmed
        # independently by a CamoFox render answering SEC_ERROR_UNKNOWN_ISSUER for the same hosts.
        # That is a fact about this network, not about the crawler.
        #
        # The transport categories are excluded: for ssl_error/dns_failure/conn_error/timeout the
        # exception IS the measurement, and naming it correctly is exactly what those rows test.
        verdict = "error"
    elif (exp in WALL_CASES and status == 200 and text_chars > 2000
          and v.case in (ec.CASE_OK, ec.CASE_JS_RENDER)):
        # The wall did not reproduce: the site served us a real page this time. Anti-bot and geo
        # decisions are per-request — they turn on IP reputation, time of day and rate — so a
        # corpus row collected during a block can come back clean an hour later. Scoring that as a
        # detector failure would be wrong, but so would silently dropping it, hence its own bucket.
        # Deliberately strict: it needs a 200 AND real extracted text, so a content-poor wall we
        # simply failed to recognise (delta.com: 8KB of sensor JS, 57 chars) still counts as a miss.
        verdict = "not_reproduced"
    elif exp == ec.CASE_CONSENT_WALL and v.case == ec.CASE_OK and text_chars > 3000:
        # Not a detector miss — the premise is wrong. A CMP interstitial is an OVERLAY: the server
        # still sends the whole article, and JS paints a dialog over it. A browser sees a wall; an
        # HTML crawler does not, and treating it as one would discard pages we already have in
        # full. Measured: theguardian.com 20,234 chars and 20minutes.fr 29,855 chars of real
        # article text behind their consent dialogs. Only a server-side gate (a redirect to a
        # consent.* domain) actually withholds content, and diagnose() still catches that.
        verdict = "not_blocking"
    else:
        verdict = "fail"
    return {"category": cat, "url": url, "verdict": verdict, "expected": exp,
            "detected": v.case, "action": v.action, "evidence": v.evidence,
            "detail": v.detail, "status": status, "redirects": redirects,
            "elapsed_s": elapsed, "bytes": len(body or ""),
            "text_chars": len(ec.visible_text(body)), "final_url": final,
            "error": (err or "")[:200], "corpus_evidence": row.get("evidence", ""),
            "redirect_chain": chain,
            "variant": row.get("variant") or row.get("vendor") or ""}


CAMO_KEY = os.environ.get("CAMOFOX_API_KEY", "mallory-camofox-key")
CAMO_HDR = {"Authorization": f"Bearer {CAMO_KEY}"}
_OUTER_HTML = "document.documentElement.outerHTML"
_REL_NEXT = __import__("re").compile(
    r'''(?is)<(?:a|link)\b[^>]*\brel\s*=\s*["']?next["']?[^>]*\bhref\s*=\s*["']([^"']+)["']'''
    r'''|<(?:a|link)\b[^>]*\bhref\s*=\s*["']([^"']+)["'][^>]*\brel\s*=\s*["']?next["']?''')
_META_CHARSET_RE = __import__("re").compile(
    r'''<meta[^>]+charset\s*=\s*["']?([\w-]+)|<meta[^>]+content\s*=\s*["'][^"']*charset='''
    r'''\s*([\w-]+)''', __import__("re").I)
# What "new content appeared" actually looks like — measured in the page, not inferred from a
# character count. Different feeds move different signals: an article feed moves characters, a
# photo grid moves images, DOM nodes and page height and nothing else. Measured on this corpus,
# giphy.com grew 1,056 -> 9,045px and gained 120 decoded images while its visible text SHRANK
# 2,216 -> 612 chars, because the grid replaces the marketing chrome — so a text-only metric did
# not merely miss the gain, it inverted the sign and scored a working infinite feed as a failure.
# infinite-scroll.com/demo/masonry is the pure case: +76 images, +152 nodes, +8,118px of document,
# and +16 characters.
_CONTENT_SIGNALS = """
(() => { try {
  // The most-repeated tag+class among siblings IS the feed item — card, tile, row, pin.
  let items = 0;
  for (const p of document.querySelectorAll('*')) {
    if (p.children.length < 3) continue;
    const g = Object.create(null);
    for (const c of p.children) {
      const k = c.tagName + '|' + (typeof c.className === 'string' ? c.className : '');
      if ((g[k] = (g[k] || 0) + 1) > items) items = g[k];
    }
  }
  return {
    imgs: [...document.images].filter(i => i.complete && i.naturalWidth > 0).length,
    nodes: document.querySelectorAll('*').length,
    items: items,
    height: Math.max(document.body ? document.body.scrollHeight : 0,
                     document.documentElement.scrollHeight),
  };
} catch (e) { return {imgs: 0, nodes: 0, items: 0, height: 0}; } })()
"""
_ZERO_SIGNALS = {"imgs": 0, "nodes": 0, "items": 0, "height": 0}


async def render_probe(client: httpx.AsyncClient, row: dict, idx: int = 0) -> dict:
    """Settle a render-only category by taking THREE measurements of the same URL:

        plain    what a plain HTTP GET returns
        loaded   what the browser has after navigating and letting JS settle
        scrolled what the browser has after scrolling to the bottom repeatedly

    The pairs answer different questions, which is why they are kept apart. js_render is proved by
    `loaded > plain` — executing JS is what produced the content. lazy_loading and infinite_scroll
    are proved by `scrolled > loaded` — the content existed only after the viewport moved, so a
    render alone would not have been enough. Collapsing these into one number would let a page that
    merely needs JS masquerade as one that needs scrolling.
    """
    url, cat = row["url"], row["category"]
    plain = ""
    sig_loaded, sig_scrolled = dict(_ZERO_SIGNALS), dict(_ZERO_SIGNALS)
    try:
        r = await client.get(url, timeout=25, follow_redirects=True, headers={"User-Agent": UA})
        plain = ec.visible_text(r.text)
    except Exception:
        pass

    user = f"edgecase-{idx % 6}"
    tab, err, loaded, scrolled = None, "", "", ""
    t0 = time.monotonic()

    async def measure() -> tuple[str, dict]:
        """Visible text AND the count of images that have actually decoded.

        Text alone is the wrong metric for lazy_loading: the whole point of a lazy image is that
        it swaps a placeholder for a real src when it enters the viewport, which changes no text
        at all. Measured that way, 17 of 20 lazy-loading pages "failed" while working correctly.
        `complete && naturalWidth > 0` is the honest test — the browser decoded actual pixels."""
        # SIGNALS FIRST, and the two are independent. Serialising outerHTML across the REST bridge
        # is the expensive call and it times out on exactly the pages that grew the most — which
        # used to take the signals down with it and report a page that tripled in size as
        # "nothing moved". Signals are what the scoring reads, so they must not depend on the
        # text call surviving.
        n = await client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", timeout=60, headers=CAMO_HDR,
                              json={"userId": user, "expression": _CONTENT_SIGNALS})
        sig = (n.json() or {}).get("result") if n.status_code < 400 else None
        if not isinstance(sig, dict):
            sig = _ZERO_SIGNALS
        text = ""
        try:
            e = await client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", timeout=120,
                                  json={"userId": user, "expression": _OUTER_HTML},
                                  headers=CAMO_HDR)
            if e.status_code < 400:
                text = ec.visible_text((e.json() or {}).get("result") or "")
        except Exception:
            pass                    # a huge DOM we could not serialise is still a MEASURED page
        return text, {k: int(sig.get(k) or 0) for k in _ZERO_SIGNALS}

    try:
        r = await client.post(f"{CAMOFOX}/tabs", timeout=120,
                              json={"userId": user, "sessionKey": "s", "url": url})
        tab = (r.json() or {}).get("tabId") if r.status_code < 400 else None
        if not tab:
            raise RuntimeError(f"tab create failed: HTTP {r.status_code} {r.text[:120]}")
        await client.post(f"{CAMOFOX}/tabs/{tab}/wait", timeout=60,
                          json={"userId": user, "timeout": 8000, "waitForNetwork": True})
        loaded, sig_loaded = await measure()
        # Scroll to the bottom repeatedly, via evaluate. NOT the REST /scroll endpoint: that is
        # Playwright mouse.wheel under Camoufox humanize, and it returns HTTP 500 after a 30s
        # server-side timeout (measured — the same defect this work fixed in camofox_client).
        # Bounded, because an infinite feed never stops growing.
        # Each round is swallowed on its own. A round that times out used to raise straight past
        # the SECOND measurement, so the row reported "nothing moved" with the post-scroll signals
        # still at zero — on the very pages that grew enough to make the round slow.
        # The expression is the engine's own _SCROLL_DEEPEST_JS, imported not copied, so the
        # harness keeps testing what the crawler actually runs (window.scrollTo is a no-op on an
        # app-shell layout where a div, not the body, is the scroller).
        for _ in range(6):
            try:
                await client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", timeout=45, headers=CAMO_HDR,
                                  json={"userId": user, "expression": _SCROLL_DEEPEST_JS})
                await client.post(f"{CAMOFOX}/tabs/{tab}/wait", timeout=30,
                                  json={"userId": user, "timeout": 1500, "waitForNetwork": True})
            except Exception as exc:
                err = err or f"scroll round timed out: {type(exc).__name__}"
                break
        # Apply the crawler's OWN lazy-image resolver, imported from the engine rather than
        # reimplemented here — otherwise this measures the harness, not the shipped code path.
        # Scrolling alone leaves natively-lazy images undecoded (they are the common case now:
        # loading="lazy" with no data-src at all), which is why the lazy_loading category scored
        # 3/20 before this existed.
        # In-page content switchers, the engine's own step 3b. A year/tab/category control loads
        # real content over AJAX and changes no URL, so neither scrolling nor link extraction can
        # reach it. Measured on scrapethissite.com/pages/ajax-javascript/: clicking the first such
        # control takes the DOM from 101 nodes to 182 and paints a 17-row table.
        try:
            for i in range(4):
                e = await client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", timeout=30,
                                      headers=CAMO_HDR,
                                      json={"userId": user,
                                            "expression": f"({_INPAGE_CLICK_JS.strip()})({i})"})
                tot = (e.json() or {}).get("result") if e.status_code < 400 else -1
                if not isinstance(tot, (int, float)) or tot < 0 or i >= tot:
                    break
                await client.post(f"{CAMOFOX}/tabs/{tab}/wait", timeout=30,
                                  json={"userId": user, "timeout": 1500, "waitForNetwork": True})
        except Exception as exc:
            err = err or f"in-page click timed out: {type(exc).__name__}"
        # The engine's step 3: click "load more"/"show more", then scroll again. Omitting it meant
        # the benchmark scored the crawler as unable to reach content the crawler DOES reach —
        # phys.org, europarl, kickstarter and wikiart all expose a control the shipped
        # _LOADMORE_JS already finds, and all four reported "nothing moved". Measured on phys.org:
        # 26,853 -> 56,167 chars and 140 -> 252 decoded images from this block alone.
        # Tagged in-page and clicked in-page, because the REST bridge has no locator API; the
        # engine's own click is a real Playwright click on the same tagged element.
        for _ in range(5):
            try:
                # INVOKED, not just evaluated. _SCROLL_DEEPEST_JS and _LAZY_RESOLVE_JS are IIFEs,
                # but _LOADMORE_JS is a bare arrow function that Playwright calls for the engine —
                # and the REST /evaluate bridge does not call it. Sent raw it returns a FUNCTION,
                # `result` is falsy, and the loop broke on its first turn: measured as
                # newatlas/vsco/kickstarter/wikiart reporting byte-identical before and after.
                e = await client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", timeout=30,
                                      headers=CAMO_HDR,
                                      json={"userId": user,
                                            "expression": f"({_LOADMORE_JS.strip()})()"})
                if e.status_code >= 400 or not (e.json() or {}).get("result"):
                    break
                await client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", timeout=30, headers=CAMO_HDR,
                                  json={"userId": user, "expression":
                                        "document.querySelector('[data-__lm=\"1\"]').click(),"
                                        "document.querySelectorAll('[data-__lm]').forEach("
                                        "e=>e.removeAttribute('data-__lm'))"})
                await client.post(f"{CAMOFOX}/tabs/{tab}/wait", timeout=30,
                                  json={"userId": user, "timeout": 2000, "waitForNetwork": True})
                await client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", timeout=45, headers=CAMO_HDR,
                                  json={"userId": user, "expression": _SCROLL_DEEPEST_JS})
                await client.post(f"{CAMOFOX}/tabs/{tab}/wait", timeout=30,
                                  json={"userId": user, "timeout": 1500, "waitForNetwork": True})
            except Exception as exc:
                err = err or f"load-more round timed out: {type(exc).__name__}"
                break
        try:
            await client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", timeout=45, headers=CAMO_HDR,
                              json={"userId": user, "expression": _LAZY_RESOLVE_JS})
            await client.post(f"{CAMOFOX}/tabs/{tab}/wait", timeout=30,
                              json={"userId": user, "timeout": 2500, "waitForNetwork": True})
            # Then wait for the DECODE, which is the thing the images signal actually counts. The
            # 2,500 ms above is a guess against the network: flipping 90 images to eager starts 90
            # fetches, and whether they have decoded when the second measurement runs depends on
            # machine load, not on the page. Measured: news.mit.edu reports 9 -> 31 decoded run
            # alone and 9 -> 12 inside the full 107-URL pass at concurrency 12 — same page, same
            # code, opposite verdict. img.decode() resolves when the pixels exist, so ask that
            # instead. Bounded at 8s and every rejection swallowed: a 403 image must stay undecoded.
            await client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", timeout=45, headers=CAMO_HDR,
                              json={"userId": user, "expression": """
(async () => { try {
  const pending = [...document.images].filter(i => !i.complete || !i.naturalWidth);
  await Promise.race([
    Promise.all(pending.map(i => (i.decode ? i.decode() : Promise.resolve()).catch(() => {}))),
    new Promise(r => setTimeout(r, 8000)),
  ]);
  return [...document.images].filter(i => i.complete && i.naturalWidth > 0).length;
} catch (e) { return -1; } })()
"""})
        except Exception as exc:
            err = err or f"lazy-resolve timed out: {type(exc).__name__}"
        scrolled, sig_scrolled = await measure()
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        if tab:
            try:
                await client.delete(f"{CAMOFOX}/tabs/{tab}", timeout=30,
                                    params={"userId": user}, headers=CAMO_HDR)
            except Exception:
                pass

    best = max(len(loaded), len(scrolled))
    if cat == "js_render":
        # Chars are compared against the BEST the render tier reached, not the mid-render snapshot,
        # because what the crawler stores is the final interacted DOM — scrapethissite.com's AJAX
        # table needs a year link clicked, and scoring `loaded` alone called that -5 chars.
        #
        # And chars alone are the wrong instrument here for the same reason they were wrong for
        # infinite scroll: that table is 17 rows of short film titles, so recovering the ENTIRE
        # point of the page is worth +290 characters and would miss a 400-char floor, while the
        # DOM gained 61 nodes and a table that did not exist before. So the structural signals are
        # consulted too, measured browser-side across the interaction.
        why, base_n = "JS execution", len(plain)
        best_after = max(len(loaded), len(scrolled))
        signals = (
            ("chars",  len(plain),           best_after,             400, True),
            ("items",  sig_loaded["items"],  sig_scrolled["items"],    5, False),
            ("nodes",  sig_loaded["nodes"],  sig_scrolled["nodes"],   40, True),
            ("pixels", sig_loaded["height"], sig_scrolled["height"], 600, True),
        )
        hits = [(u, a - b) for u, b, a, floor, prop in signals
                if a - b >= floor and (not prop or a - b >= b * 0.25)]
        ok = bool(hits)
        unit, gain = hits[0] if hits else ("chars", best_after - len(plain))
        moved = ", ".join(f"{u}{g:+d}" for u, g in hits) or "nothing moved"
    else:
        # "Did new content appear", asked five ways and passed if ANY of them moved: different
        # feeds move different signals and a page is not obliged to move all of them. Ordered
        # most-meaningful first, so the reported unit is the strongest evidence available.
        why, base_n = "scrolling", len(loaded)
        # (name, before, after, floor, needs_proportion)
        signals = (
            ("chars",  len(loaded),          len(scrolled),          400, True),
            ("images", sig_loaded["imgs"],   sig_scrolled["imgs"],     5, False),
            ("items",  sig_loaded["items"],  sig_scrolled["items"],    5, False),
            ("nodes",  sig_loaded["nodes"],  sig_scrolled["nodes"],   40, True),
            ("pixels", sig_loaded["height"], sig_scrolled["height"], 600, True),
        )
        # Every signal must clear an absolute floor, so one stray node or one re-decoded thumbnail
        # is never a pass. The COUNTABLE signals stop there: five images that were not decoded
        # before, or five new repeated items, are new content on any page, however big it already
        # was. Requiring a further 25% of base on those punished exactly the pages that were
        # working — pexels.com decoded 50 -> 60 images and scored a failure because 10 < 12.5.
        # The noisy signals (characters, DOM nodes, pixel height) do carry the proportion test,
        # because nav chrome and re-layout move all three on a page that gained nothing.
        hits = [(u, a - b) for u, b, a, floor, prop in signals
                if a - b >= floor and (not prop or a - b >= b * 0.25)]
        ok = bool(hits)
        unit, gain = hits[0] if hits else ("chars", len(scrolled) - len(loaded))
        moved = ", ".join(f"{u}{g:+d}" for u, g in hits) or "nothing moved"
    # A measurement of NOTHING is not a measurement of failure. All four structural signals at zero
    # on BOTH sides means the browser never gave us a page — the tab died, the navigation never
    # settled, or the evaluate call came back empty — so the row says nothing about whether the
    # crawler can recover scrolled content. Measured on 9gag.com and knowyourmeme.com in this pass:
    # 0 nodes, 0 images, 0 height, 0 chars, loaded AND scrolled. The same rule the HTTP probe
    # follows for a request that never returned a page.
    blank = (not sig_loaded["nodes"] and not sig_scrolled["nodes"]
             and not len(loaded) and not len(scrolled))
    return {"category": cat, "url": url,
            "verdict": "pass" if ok else ("error" if (err or blank) else "fail"),
            "expected": f"{why} recovers content", "detected": f"+{gain} {unit} from {why}",
            "action": ec.ACT_RENDER_JS,
            "evidence": f"plain HTTP {len(plain)} chars -> rendered {len(loaded)} -> after scroll "
                        f"{len(scrolled)} chars; images {sig_loaded['imgs']} -> "
                        f"{sig_scrolled['imgs']}; nodes {sig_loaded['nodes']} -> "
                        f"{sig_scrolled['nodes']}; items {sig_loaded['items']} -> "
                        f"{sig_scrolled['items']}; height {sig_loaded['height']} -> "
                        f"{sig_scrolled['height']}px; {why} moved {moved} "
                        f"in {time.monotonic() - t0:.1f}s" + (f"; {err}" if err else ""),
            "detail": {"plain": len(plain), "loaded": len(loaded), "scrolled": len(scrolled),
                       "img_loaded": sig_loaded["imgs"], "img_scrolled": sig_scrolled["imgs"],
                       "sig_loaded": sig_loaded, "sig_scrolled": sig_scrolled},
            "status": None, "redirects": 0,
            "elapsed_s": round(time.monotonic() - t0, 2), "bytes": 0,
            "text_chars": best, "final_url": url, "error": err,
            "corpus_evidence": row.get("evidence", ""), "variant": row.get("variant", "")}


async def paginate_probe(client: httpx.AsyncClient, row: dict) -> dict:
    """Prove the crawler can WALK a paginated listing, which is a frontier property and cannot be
    judged from one response.

    Uses the crawler's own link extractor (`crawler.parse.extract_links`) — not a bespoke regex —
    so a pass means the production code path finds the next page, not that the harness can. Walks
    up to 5 hops, following the same-host link whose URL differs from the current one only in a
    numeric component; that is what a "next page" IS, and keying on it avoids trusting a rel=next
    that many real pagers omit. Reports the chain actually reached, and the unique-byte-size of
    each hop so a pager that silently serves page 1 forever is visible rather than counted as
    success."""
    from crawler import parse
    from crawler.async_engine import _looks_paginated     # the shipped shape test, not a copy
    from crawler.canonicalize import canonicalize_url

    # Canonicalized, because every candidate below is canonicalized before comparison and
    # canonicalize_url STRIPS THE TRAILING SLASH. Comparing a canonical candidate against the raw
    # seed made ".../page/#" never equal ".../page/#/", so quotes.toscrape.com scored 0 discovered
    # while the crawler was in fact following its Next link perfectly. The harness was the bug.
    # `url` stays the CORPUS url — it is this row's identity, and results.jsonl is keyed on it, so
    # recording the canonical form instead silently orphaned every row whose canonical differs
    # (quotes.toscrape.com/page/1/ -> .../page/1) and the report then fell back to a stale verdict.
    # The canonical form is what we WALK with, because every candidate below is canonicalized
    # before comparison and canonicalize_url strips the trailing slash on a bare path.
    url = row["url"]
    start = canonicalize_url(url)
    chain, sizes, seen = [start], [], {start}
    cur, err = start, ""
    t0 = time.monotonic()
    num_re = __import__("re").compile(r"\d+")
    try:
        for _ in range(5):
            r = await client.get(cur, timeout=25, follow_redirects=True,
                                 headers={"User-Agent": UA})
            sizes.append(len(r.text))
            links = [canonicalize_url(u) for u in parse.extract_links(r.text, str(r.url))]
            host = httpx.URL(cur).host
            # The pager POINTERS first — the RFC 8288 `Link:` response header and rel=next/prev in
            # markup — both read with the crawler's own shipped helpers rather than a regex local
            # to this file. That matters twice over: the previous local regex required rel="next"
            # exactly and so missed old.reddit's rel="nofollow next", and it never looked at
            # headers at all, which is the ONLY next-page pointer api.github.com and wp-json emit.
            # rel="NEXT" only. The helpers return prev as well (the crawler wants both — a prev
            # page is still a page), but a walker handed prev picks the LOWER number and marches
            # backwards into `seen`, which is why techcrunch and wp-json stalled at 2 hops.
            cands = []
            for href in (parse.links_from_link_header(r.headers.get("link"), str(r.url), {"next"})
                         + parse.extract_rel_links(r.text, str(r.url), {"next"})):
                try:
                    u = canonicalize_url(str(httpx.URL(str(r.url)).join(href)))
                except Exception:
                    continue
                if u not in seen and u not in cands:
                    cands.append(u)
            if not cands:
                # Same host, same shape, one number larger somewhere in the URL.
                cands = [u for u in links
                         if httpx.URL(u).host == host and u not in seen
                         and num_re.sub("#", u) == num_re.sub("#", cur)
                         and [int(n) for n in num_re.findall(u)]
                         > [int(n) for n in num_re.findall(cur)]]
            if not cands:
                # Last resort: any unseen same-host link the SHIPPED shape test calls a pager.
                # Covers cursor/keyset pagers that publish no rel at all (MediaWiki's ?pagefrom=).
                cands = [u for u in links
                         if u not in seen and _looks_paginated(u)
                         and _same_host(u, cur)]
            if not cands:
                break
            cur = min(cands, key=lambda u: [int(n) for n in num_re.findall(u)] or [0])
            seen.add(cur)
            chain.append(cur)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:160]
    # Second, weaker-but-fairer measure: how many DISTINCT page links did the crawler's extractor
    # surface from page 1? The engine enqueues every link it finds, so discovering the pager is
    # what actually decides whether the listing gets crawled — walking it in order is this
    # harness's convenience, not the crawler's mechanism. Counting only the chain understated the
    # crawler, because a pager whose links this walker cannot order is still fully enqueued.
    discovered = 0
    try:
        r0 = await client.get(start, timeout=25, follow_redirects=True, headers={"User-Agent": UA})
        host0 = httpx.URL(start).host
        shape = num_re.sub("#", start)
        found = {canonicalize_url(u) for u in parse.extract_links(r0.text, str(r0.url))}
        found |= {canonicalize_url(u) for u in parse.extract_rel_links(r0.text, str(r0.url))}
        found |= {canonicalize_url(u)
                  for u in parse.links_from_link_header(r0.headers.get("link"), str(r0.url))}
        # Per-URL, not one try around the whole comprehension: httpx.URL() raises on a malformed
        # href, and one bad link among gutenberg's 46 anchors zeroed the ENTIRE count — reported
        # as "0 discovered" on a page whose pager was found correctly.
        hits = set()
        for u in found:
            try:
                if (_same_host(u, start) and u != start
                        and (num_re.sub("#", u) == shape or _looks_paginated(u))):
                    hits.add(u)
            except Exception:
                continue
        discovered = len(hits)
    except Exception:
        pass
    walked = len(chain) >= 3 and len(set(sizes)) > 1
    ok = walked or discovered >= 3
    how = "walked" if walked else ("discovered" if ok else "neither")
    # Render fallback, only when the HTML tiers found nothing. A "Load more" BUTTON creates no URL
    # at all — it fires fetch('/ajax/products?offset=20') and appends the result — so no amount of
    # parsing can reach it and the honest test is whether the crawler's render tier can. This
    # mirrors what the engine now does in _click_discover (a Playwright "request" listener), via a
    # fetch/XHR shim because the CamoFox REST bridge exposes evaluate, not interception.
    if not ok:
        found = await _xhr_pager_probe(client, start)
        if found:
            ok, how = True, "ajax"
            err = (err + "; " if err else "") + f"AJAX pager: {found[0]}"
            discovered = max(discovered, len(found))
    return {"category": row["category"], "url": url,
            "verdict": "pass" if ok else "fail",
            "expected": "crawler reaches the other pages",
            "detected": f"{len(chain)} walked, {discovered} discovered",
            "action": ec.ACT_STORE,
            "evidence": f"[{how}] followed {len(chain)} pages via crawler.parse.extract_links ("
                        + " -> ".join(u.split("/")[-1][:22] or "/" for u in chain)
                        + f"), body sizes {sizes}; extractor also surfaced {discovered} distinct "
                        f"same-shape page links from page 1" + (f"; {err}" if err else ""),
            "detail": {"chain": chain, "sizes": sizes, "discovered": discovered},
            "status": None, "redirects": 0,
            "elapsed_s": round(time.monotonic() - t0, 2), "bytes": sum(sizes),
            "text_chars": 0, "final_url": cur, "error": err,
            "corpus_evidence": row.get("evidence", ""), "variant": row.get("variant", "")}


_XHR_RECORDER_JS = """
(() => { try {
  if (window.__mallory_xhr) return 1;
  window.__mallory_xhr = [];
  const rec = u => { try { window.__mallory_xhr.push(String(u)); } catch(e){} };
  const of = window.fetch;
  window.fetch = function (i, ...r) { rec(i && i.url ? i.url : i); return of.apply(this, [i, ...r]); };
  const oo = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u, ...r) { rec(u); return oo.apply(this, [m, u, ...r]); };
  return 1;
} catch (e) { return -1; } })()
"""
# The engine's own load-more finder, imported so the harness clicks what the crawler clicks.
# Same widening as the engine's own _LOADMORE_JS, and for the same measured reason: a tag list of
# button/a/[role=button]/[onclick] finds nothing on newatlas.com (a <span> with an addEventListener
# handler) or vsco.co (a <grain-button> custom element). Kept as its own copy rather than importing
# _LOADMORE_JS because this one CLICKS in page — the engine tags and lets Playwright click — but the
# finder must not be allowed to drift narrower than the shipped one again.
_CLICK_LOADMORE_JS = """
(() => { try {
  const rx = /load more|show more|view more|see more|load all|next/i;
  let hit = null, bestN = 1e9;
  for (const e of document.querySelectorAll('*')) {
    const t = (e.textContent || '').trim();
    if (t.length > 40 || !rx.test(t) || e.offsetParent === null) continue;
    const n = e.querySelectorAll('*').length;
    if (n < bestN) { bestN = n; hit = e; }
  }
  if (!hit) return 0;
  hit.click();
  return 1;
} catch (e) { return -1; } })()
"""


async def _render_html(client: httpx.AsyncClient, url: str) -> str:
    """Rendered outerHTML via the live CamoFox service, or "" on any failure.

    A browser that will not start is not evidence about the page, so every failure returns "" and
    the caller keeps whatever the HTTP tier concluded."""
    user, tab = "edgecase-cap", None
    try:
        r = await client.post(f"{CAMOFOX}/tabs", timeout=120,
                              json={"userId": user, "sessionKey": "cap", "url": url})
        tab = (r.json() or {}).get("tabId") if r.status_code < 400 else None
        if not tab:
            return ""
        await client.post(f"{CAMOFOX}/tabs/{tab}/wait", timeout=60,
                          json={"userId": user, "timeout": 9000, "waitForNetwork": True})
        e = await client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", timeout=60, headers=CAMO_HDR,
                              json={"userId": user, "expression": _OUTER_HTML})
        return (e.json() or {}).get("result") or "" if e.status_code < 400 else ""
    except Exception:
        return ""
    finally:
        if tab:
            try:
                await client.delete(f"{CAMOFOX}/tabs/{tab}", timeout=30,
                                    params={"userId": user}, headers=CAMO_HDR)
            except Exception:
                pass


async def _xhr_pager_probe(client: httpx.AsyncClient, url: str) -> list[str]:
    """Click the page's own "load more" and report the pager URLs its JS actually requested.

    Returns [] on any failure — this is a fallback, and a browser that will not start is not
    evidence that a site has no pager."""
    from crawler.async_engine import _looks_paginated

    user, tab = "edgecase-xhr", None
    try:
        r = await client.post(f"{CAMOFOX}/tabs", timeout=120,
                              json={"userId": user, "sessionKey": "pg", "url": url})
        tab = (r.json() or {}).get("tabId") if r.status_code < 400 else None
        if not tab:
            return []
        await client.post(f"{CAMOFOX}/tabs/{tab}/wait", timeout=60,
                          json={"userId": user, "timeout": 6000, "waitForNetwork": True})
        # Recorder BEFORE the click, or the request we are looking for has already gone.
        await client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", timeout=30, headers=CAMO_HDR,
                          json={"userId": user, "expression": _XHR_RECORDER_JS})
        for _ in range(3):
            await client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", timeout=30, headers=CAMO_HDR,
                              json={"userId": user, "expression": _CLICK_LOADMORE_JS})
            await client.post(f"{CAMOFOX}/tabs/{tab}/wait", timeout=30,
                              json={"userId": user, "timeout": 2000, "waitForNetwork": True})
        e = await client.post(f"{CAMOFOX}/tabs/{tab}/evaluate", timeout=30, headers=CAMO_HDR,
                              json={"userId": user,
                                    "expression": "JSON.stringify(window.__mallory_xhr||[])"})
        seen_urls = json.loads((e.json() or {}).get("result") or "[]") if e.status_code < 400 else []
    except Exception:
        return []
    finally:
        if tab:
            try:
                await client.delete(f"{CAMOFOX}/tabs/{tab}", timeout=30,
                                    params={"userId": user}, headers=CAMO_HDR)
            except Exception:
                pass
    out = []
    for u in seen_urls:
        try:
            absu = str(httpx.URL(url).join(u))
        except Exception:
            continue
        # Same shape test the engine applies, so the harness cannot pass on a URL the crawler
        # would refuse to enqueue.
        if _looks_paginated(absu) and _same_host(absu, url) and absu not in out:
            out.append(absu)
    return out


def _same_host(a: str, b: str) -> bool:
    """httpx.URL() raises on a malformed href; a bad link must cost that link, not the measurement."""
    try:
        return httpx.URL(a).host == httpx.URL(b).host
    except Exception:
        return False


CAPTCHA_FAMILY = {"recaptcha": {"recaptcha_v2", "recaptcha_v3", "recaptcha_enterprise"},
                  "hcaptcha": {"hcaptcha"}, "captcha_image_text": {"image_text"}}


async def captcha_probe(client: httpx.AsyncClient, row: dict) -> dict:
    """For the captcha classes the question is IDENTIFICATION, not blocking.

    A captcha demo page — or a login form carrying a widget — is not withholding anything, so
    `diagnose()` correctly reports it as fine. What has to be right is that the crawler can name
    the widget and pull out the sitekey, because that is what decides whether a solve is even
    attemptable and which solver could do it. So this probe asks `captcha.detect_http` (and
    `looks_image_text` for the OCR family) directly, and passes only when the identified kind is
    in the family the row was collected for."""
    from crawler import captcha as cap

    url = row["url"]
    t0 = time.monotonic()
    try:
        r = await client.get(url, timeout=25, follow_redirects=True, headers={"User-Agent": UA})
    except Exception as exc:
        return {"category": row["category"], "url": url, "verdict": "error", "detected": "",
                "expected": "", "action": "", "evidence": f"{type(exc).__name__}: {exc}"[:150],
                "detail": {}, "status": None, "redirects": 0, "elapsed_s": 0.0, "bytes": 0,
                "text_chars": 0, "final_url": url, "error": str(exc)[:150],
                "corpus_evidence": row.get("evidence", ""), "variant": row.get("variant", "")}
    det = cap.detect_http(r.status_code, dict(r.headers), r.text)
    sitekey = det.sitekey
    kinds = list(det.all_kinds)
    # identify_widgets answers "what widget is here", which is the question for these rows, and it
    # must run ALWAYS. It used to be skipped whenever `kind` was already in the captcha family —
    # and `image_text` is in that family, so any captcha-named <img> anywhere on the page
    # pre-empted the widget lookup entirely. Measured on strikingly.com/s/signup and 2captcha.com,
    # both of which carry a real hCaptcha and were reported as image_text.
    for wk, wsk in cap.identify_widgets(r.text):
        if wk not in kinds:
            kinds.append(wk)
        sitekey = sitekey or wsk
    # Weaker tier: the vendor named in a server-rendered config blob. Proves the site provisions
    # that vendor, not that this page mounts it — so it is appended AFTER the widgets, never ahead.
    for ck, csk in cap.config_vendors(r.text):
        if ck not in kinds:
            kinds.append(ck)
        sitekey = sitekey or csk
    if cap.looks_image_text(r.text) and "image_text" not in kinds:
        kinds.append("image_text")               # a FALLBACK now, not a pre-emption
    kinds = ([k for k in cap._ORDER if k in kinds]
             + [k for k in kinds if k not in cap._ORDER])
    kind = kinds[0] if kinds else None
    fam = CAPTCHA_FAMILY.get(row["category"], set())
    ok = bool(fam & set(kinds))                  # a page can legitimately carry more than one
    via = "http"
    if not ok:
        # Render fallback — what the CRAWLER actually does with a Cloudflare 403: escalate to the
        # C3 stealth tier. Scoring only the raw-HTTP answer under-reports the shipped behaviour,
        # because a signup page behind a managed challenge exposes nothing at HTTP level by
        # construction (kick.com's entire body is a 79-byte JSON refusal stub) and names its
        # captcha plainly once the challenge clears. Both the DOM-evidence path and the HTML-scan
        # path are run on the rendered page: they miss different things.
        html = await _render_html(client, url)
        if html:
            rk = list(cap.detect_http(200, {}, html).all_kinds)
            for wk, wsk in cap.identify_widgets(html):
                if wk not in rk:
                    rk.append(wk)
                sitekey = sitekey or wsk
            for ck, csk in cap.config_vendors(html):
                if ck not in rk:
                    rk.append(ck)
                sitekey = sitekey or csk
            if cap.looks_image_text(html) and "image_text" not in rk:
                rk.append("image_text")
            if fam & set(rk):
                ok, via = True, "render"
                kinds = ([k for k in cap._ORDER if k in rk]
                         + [k for k in rk if k not in cap._ORDER])
                kind = kinds[0]
    return {"category": row["category"], "url": url,
            "verdict": "pass" if ok else "fail",
            "expected": "/".join(sorted(fam)), "detected": kind or "none",
            "action": ec.ACT_SOLVE_CAPTCHA if ok else "",
            "evidence": f"[{via}] HTTP {r.status_code}, {len(r.text)} bytes; identified {kind or 'nothing'}"
                        + (f", sitekey={det.sitekey[:28]}" if det.sitekey else ", no sitekey exposed")
                        + f", solvability={cap.solvability(kind) if kind else 'n/a'}",
            "detail": {"kind": kind, "sitekey": det.sitekey, "all": det.all_kinds},
            "status": r.status_code, "redirects": len(r.history),
            "elapsed_s": round(time.monotonic() - t0, 2), "bytes": len(r.text),
            "text_chars": len(ec.visible_text(r.text)), "final_url": str(r.url), "error": "",
            "corpus_evidence": row.get("evidence", ""), "variant": row.get("variant", "")}


async def burst_probe(client: httpx.AsyncClient, row: dict, n: int = 8) -> dict:
    """Rate limiting cannot be observed in one request — that is the entire nature of it. Send a
    small, deliberately polite burst and stop the instant a throttle appears.

    Capped at 8 and aborted on the first 429 so this stays a measurement and not an attack. A row
    that never throttles inside 8 requests is reported as not_reproduced, not as a detector miss:
    published quotas are usually far above 8, and pretending otherwise would mean hammering
    somebody's API to make a number look better."""
    url = row["url"]
    codes, v, err = [], None, ""
    t0 = time.monotonic()
    for i in range(n):
        try:
            r = await client.get(url, timeout=15, follow_redirects=True,
                                 headers={"User-Agent": UA})
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"[:120]
            break
        codes.append(r.status_code)
        v = ec.diagnose(url, status=r.status_code, headers=dict(r.headers), body=r.text,
                        final_url=str(r.url))
        if v.case in (ec.CASE_RATE_LIMIT, ec.CASE_ANTIBOT, ec.CASE_CAPTCHA):
            break                                    # throttled — stop immediately, be polite
        await asyncio.sleep(0.15)
    hit = v.case if v else "none"
    ok = hit == ec.CASE_RATE_LIMIT
    return {"category": row["category"], "url": url,
            "verdict": "pass" if ok else ("not_reproduced" if hit in ("ok", "none") else "fail"),
            "expected": ec.CASE_RATE_LIMIT, "detected": hit,
            "action": v.action if v else "", "elapsed_s": round(time.monotonic() - t0, 2),
            "evidence": f"burst of {len(codes)} requests -> statuses {codes}; "
                        + (v.evidence if v else "no response") + (f"; {err}" if err else ""),
            "detail": (v.detail if v else {}) | {"codes": codes},
            "status": codes[-1] if codes else None, "redirects": 0, "bytes": 0, "text_chars": 0,
            "final_url": url, "error": err, "corpus_evidence": row.get("evidence", ""),
            "variant": row.get("variant", "")}


def urlspace_probe(row: dict) -> dict:
    """A single faceted URL is not a trap — the EXPLOSION is, and that is a frontier property, so
    it has to be tested against the frontier's own guards rather than against one response.

    Replays the exact guard sequence `_enqueue_candidate` applies (looks_like_trap, then the
    per-(host,path) query-variant counter) over 30 synthetic permutations of the corpus URL, and
    reports how many were admitted before the cap bit. No network: the whole point is that these
    URLs are never fetched."""
    from urllib.parse import urlsplit

    from crawler import errors
    from crawler.canonicalize import canonicalize_url

    cap = int(os.environ.get("CRAWLER_MAX_QUERY_VARIANTS", "20"))
    base = row["url"].split("?")[0]
    variants = [f"{base}?sort=price&color=c{i}&page={i}&view=grid" for i in range(30)]
    counts: dict[str, int] = {}
    admitted, dropped = 0, 0
    for u in variants:
        cl = canonicalize_url(u)
        if errors.looks_like_trap(cl):
            dropped += 1
            continue
        qp = urlsplit(cl)
        if qp.query:
            key = f"{qp.hostname}{qp.path}"
            n = counts.get(key, 0) + 1
            if n > cap:
                dropped += 1
                continue
            counts[key] = n
        admitted += 1
    ok = dropped > 0 and admitted <= cap
    return {"category": row["category"], "url": row["url"],
            "verdict": "pass" if ok else "fail",
            "expected": f"frontier admits at most {cap} query variants per host+path",
            "detected": f"{admitted} admitted, {dropped} dropped",
            "action": ec.ACT_DROP_TRAP,
            "evidence": f"replayed the engine's _enqueue_candidate guards over 30 permutations of "
                        f"{base}: {admitted} enqueued, {dropped} refused by the "
                        f"CRAWLER_MAX_QUERY_VARIANTS={cap} cap (no fetch spent on any of them)",
            "detail": {"admitted": admitted, "dropped": dropped, "cap": cap},
            "status": None, "redirects": 0, "elapsed_s": 0.0, "bytes": 0, "text_chars": 0,
            "final_url": row["url"], "error": "", "corpus_evidence": row.get("evidence", ""),
            "variant": row.get("variant", "")}


# The expectation for a status code is the CODE'S OWN MEANING, taken from the RFC (and from
# Cloudflare's docs for the 52x range), not from a corpus author's opinion — so it lives here and
# cannot be edited into agreement with the crawler. What is asserted is the ACTION, i.e. the policy,
# because `gone` and `http_error` are both honest NAMES for a 404 while `mark_gone` vs `retry_later`
# is the difference between never touching a dead URL again and hammering it forever.
STATUS_POLICY: dict[int, set[str]] = {
    400: {ec.ACT_SKIP}, 402: {ec.ACT_SKIP}, 405: {ec.ACT_SKIP}, 406: {ec.ACT_SKIP},
    409: {ec.ACT_SKIP}, 418: {ec.ACT_SKIP}, 422: {ec.ACT_SKIP},
    401: {ec.ACT_SKIP},                                    # auth wall: no amount of retrying helps
    403: {ec.ACT_SKIP, ec.ACT_ESCALATE_C3, ec.ACT_SOLVE_CAPTCHA},   # often anti-bot, worth a tier up
    404: {ec.ACT_MARK_GONE}, 410: {ec.ACT_MARK_GONE},      # dead: must never be re-fetched
    408: {ec.ACT_RETRY_LATER, ec.ACT_BACKOFF},             # Request Timeout — the server, not us
    425: {ec.ACT_RETRY_LATER, ec.ACT_BACKOFF},             # Too Early — replay later, by definition
    429: {ec.ACT_BACKOFF},                                 # must honour Retry-After
    451: {ec.ACT_SKIP},                                    # legal block; retrying is pointless
    500: {ec.ACT_RETRY_LATER}, 501: {ec.ACT_RETRY_LATER, ec.ACT_SKIP},
    502: {ec.ACT_RETRY_LATER}, 504: {ec.ACT_RETRY_LATER},
    503: {ec.ACT_BACKOFF, ec.ACT_RETRY_LATER},             # Retry-After is common here
    507: {ec.ACT_RETRY_LATER}, 508: {ec.ACT_RETRY_LATER},
    520: {ec.ACT_RETRY_LATER}, 521: {ec.ACT_RETRY_LATER}, 530: {ec.ACT_RETRY_LATER},
    522: {ec.ACT_RETRY_LATER}, 523: {ec.ACT_RETRY_LATER}, 524: {ec.ACT_RETRY_LATER},
    525: {ec.ACT_RETRY_LATER, ec.ACT_SKIP}, 526: {ec.ACT_RETRY_LATER, ec.ACT_SKIP},
}


async def status_probe(client: httpx.AsyncClient, row: dict) -> dict:
    """4xx / 5xx: assert the crawler applies the right POLICY to the status it actually received.

    Redirects are NOT followed here — following them would measure the destination's status
    instead of the one this row is a case for. The observed status wins over the corpus's claimed
    status: if an endpoint has stopped serving its error the row is `not_reproduced`, never
    silently re-scored against whatever it serves now."""
    url = row["url"]
    t0 = time.monotonic()
    status = headers = body = err = None
    try:
        r = await client.get(url, timeout=25, follow_redirects=False,
                             headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        status, headers = r.status_code, dict(r.headers)
        ctype = r.headers.get("content-type", "")
        body = r.text if (not ctype or "text" in ctype or "json" in ctype or "xml" in ctype) else ""
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
    elapsed = round(time.monotonic() - t0, 2)
    v = ec.diagnose(url, status=status, headers=headers, body=body, final_url=url,
                    redirects=0, elapsed_s=elapsed, error=err)
    want = STATUS_POLICY.get(status or 0)
    claimed = row.get("status")
    if status is None:
        verdict = "error"
    elif want is None:
        # 2xx/3xx, or a code nobody wrote a policy for: the error did not reproduce today.
        verdict = "not_reproduced"
    else:
        verdict = "pass" if v.action in want else "fail"
    return {"category": row["category"], "url": url, "verdict": verdict,
            "expected": f"HTTP {status} -> action in {sorted(want)}" if want else "an error status",
            "detected": v.action, "action": v.action,
            "evidence": f"HTTP {status} -> case={v.case}, action={v.action}"
                        + (f" (corpus recorded {claimed})" if claimed and claimed != status else "")
                        + f"; {str(v.evidence)[:100]}" + (f"; {err}" if err else ""),
            "detail": {"status": status, "want": sorted(want) if want else [], "case": v.case,
                       "retry_after": (headers or {}).get("retry-after")},
            "status": status, "redirects": 0, "elapsed_s": elapsed, "bytes": len(body or ""),
            "text_chars": len(ec.visible_text(body or "")), "final_url": url,
            "error": (err or "")[:200], "corpus_evidence": row.get("evidence", ""),
            "variant": row.get("variant", "")}


async def broken_html_probe(client: httpx.AsyncClient, row: dict) -> dict:
    """Malformed markup must not cost us the page.

    Two measurements, and both have to hold. First, prove the markup really IS broken: a strict
    (non-recovering) parse has to fail or log errors — otherwise the row is valid HTML and proves
    nothing. Second, run the crawler's OWN parse.py over the same bytes and require it to come back
    with real content. Recovery that returns an empty document is not recovery."""
    from lxml import etree

    from crawler import parse as cparse

    url = row["url"]
    t0 = time.monotonic()
    body, status, err, r = "", None, None, None
    # These are mostly 25-year-old pages on slow university/archive hosts; one shot at concurrency
    # times out on them and reports a network error as a parser result. Two tries, patient budget.
    for attempt in (1, 2):
        try:
            r = await client.get(url, timeout=45, follow_redirects=True,
                                 headers={"User-Agent": UA})
            status, body, err = r.status_code, r.text, None
            break
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            if attempt == 1:
                await asyncio.sleep(3)

    # Defect count from lxml's own recovering parser error log — the parser's complaints ARE the
    # evidence of malformedness, so nothing here is asserted by eye. A strict parse that RAISES is
    # the strongest evidence there is, not the absence of evidence: scoring that as 0 marked
    # feeds.bbci.co.uk (XML pushed through the HTML parser) "not a broken-HTML case".
    defects, how = 0, "lxml error_log"
    try:
        p = etree.HTMLParser(recover=True)
        etree.fromstring(body or "<html/>", p)
        defects = len(p.error_log)
    except Exception as exc:
        defects, how = 999, f"strict parse RAISED {type(exc).__name__}"
    if not defects:
        # lxml's error log stays silent on defects that are not markup errors: a declared charset
        # the body cannot actually be decoded as, or an HTTP charset that contradicts <meta>.
        # georgewbush-whitehouse.archives.gov is exactly that and logged 0.
        m = _META_CHARSET_RE.search(body[:4096] if body else "")
        meta_cs = (m.group(1) or m.group(2) or "").strip().lower() if m else ""
        http_cs = ""
        if err is None:
            http_cs = (r.headers.get("content-type", "").lower().split("charset=") + [""])[1].strip(' ";')
        if meta_cs and http_cs and meta_cs.replace("utf8", "utf-8") != http_cs.replace("utf8", "utf-8"):
            defects, how = 1, f"charset conflict: HTTP says {http_cs!r}, <meta> says {meta_cs!r}"

    text = links = title = None
    crashed = ""
    try:
        text = cparse.visible_text(body)
        links = cparse.extract_links(body, url)
        title = cparse.title_of(body)
    except Exception as exc:                     # a parser that raises has already lost the page
        crashed = f"{type(exc).__name__}: {exc}"[:160]
    recovered = bool(text and len(text) >= 200) or bool(links)
    if err or status is None:
        verdict = "error"
    elif defects <= 0:
        verdict = "not_reproduced"               # well-formed today; not a broken-HTML case
    else:
        verdict = "pass" if (recovered and not crashed) else "fail"
    return {"category": row["category"], "url": url, "verdict": verdict,
            "expected": "recovering parse yields text and/or links",
            "detected": f"{len(text or '')} chars, {len(links or [])} links",
            "action": ec.ACT_STORE,
            "evidence": f"HTTP {status}, {len(body)} bytes; {defects} markup defects ({how}); "
                        f"crawler parse recovered {len(text or '')} chars of text, "
                        f"{len(links or [])} links, title={(title or '')[:40]!r}"
                        + (f"; PARSER RAISED {crashed}" if crashed else "")
                        + (f"; {err}" if err else ""),
            "detail": {"defects": defects, "how": how, "chars": len(text or ""),
                       "links": len(links or []), "crashed": crashed},
            "status": status, "redirects": 0, "elapsed_s": round(time.monotonic() - t0, 2),
            "bytes": len(body), "text_chars": len(text or ""), "final_url": url,
            "error": (err or crashed)[:200], "corpus_evidence": row.get("evidence", ""),
            "variant": row.get("variant", "")}


async def domain_change_probe(client: httpx.AsyncClient, row: dict) -> dict:
    """A seed that redirects to another registered domain must move the crawl WITH it — and a page
    deep in the crawl that does the same must not.

    Both halves are asserted against the shipped scope predicate (`harvest._allowed_offsite`) and
    the shipped widening rule from `_enqueue_links` (`if depth == 0 and not same_site(base, url)`),
    imported rather than reimplemented, driven by a real redirect measured on the live internet.
    They are the two halves of one bug: fixing either alone reintroduces the other — a seed
    redirect that is not followed crawls exactly 1 page, and a deep redirect that IS followed once
    dragged 105 unrelated hosts and 4,412 pages into an rtx.com crawl."""
    from crawler.canonicalize import canonicalize_url, same_site
    from crawler.harvest import _allowed_offsite

    url = row["url"]
    t0 = time.monotonic()
    final, status, hops, err = url, None, 0, None
    try:
        r = await client.get(url, timeout=25, follow_redirects=True, headers={"User-Agent": UA})
        status, final, hops = r.status_code, str(r.url), len(r.history)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"

    moved = not same_site(final, url)
    seeds = {canonicalize_url(url)}
    child = final.rstrip("/") + "/some-child-page"
    strict = os.environ.get("CRAWLER_STRICT_DOMAIN")
    os.environ["CRAWLER_STRICT_DOMAIN"] = "1"        # the production default; no one-hop rescue
    try:
        deep_ok = _allowed_offsite(canonicalize_url(child), canonicalize_url(url), set(seeds))
        widened = set(seeds)
        if not same_site(final, url):                # exactly what _enqueue_links does at depth 0
            widened.add(canonicalize_url(final))
        seed_ok = _allowed_offsite(canonicalize_url(child), canonicalize_url(final), widened)
    finally:
        if strict is None:
            os.environ.pop("CRAWLER_STRICT_DOMAIN", None)
        else:
            os.environ["CRAWLER_STRICT_DOMAIN"] = strict

    if err or not status:
        verdict = "error"
    elif not moved:
        verdict = "not_reproduced"                   # no cross-domain redirect today
    else:
        verdict = "pass" if (seed_ok and not deep_ok) else "fail"
    return {"category": row["category"], "url": url, "verdict": verdict,
            "expected": "seed redirect widens scope; the same redirect at depth>0 does not",
            "detected": f"seed_follows={seed_ok}, deep_rebases={deep_ok}",
            "action": ec.ACT_STORE,
            "evidence": f"{hops} hop(s) HTTP {status}: {url} -> {final}"
                        + (" (registered domain changed)" if moved else " (same registered domain)")
                        + f"; child on the new host: allowed as a SEED redirect={seed_ok} "
                          f"(must be True, else the crawl stops at 1 page), allowed at DEPTH>0="
                          f"{deep_ok} (must be False, else the crawl adopts the whole new domain)"
                        + (f"; {err}" if err else ""),
            "detail": {"final": final, "hops": hops, "moved": moved,
                       "seed_ok": seed_ok, "deep_ok": deep_ok},
            "status": status, "redirects": hops, "elapsed_s": round(time.monotonic() - t0, 2),
            "bytes": 0, "text_chars": 0, "final_url": final, "error": (err or "")[:200],
            "corpus_evidence": row.get("evidence", ""), "variant": row.get("variant", "")}


async def main_async(a) -> int:
    only = set(a.only.split(",")) if a.only else None
    rows = load_corpus(only, a.limit)
    if not rows:
        print("no corpus rows — are the research agents still writing edgecases/corpus/*.json?")
        return 1
    if a.render:
        rows = [r for r in rows if r["category"] in RENDER_ONLY]
    if a.paginate:
        rows = [r for r in rows if r["category"] in CRAWL_ONLY]
    if a.urls_file:
        # Settle only the rows a previous pass could not judge. Without this, re-running the
        # render pass over a whole category overwrites rows that pass 1 had ALREADY proved from
        # HTTP alone, replacing a cheap correct verdict with a browser measurement that answers a
        # different question — which is how 14 detected js_render rows became 11.
        wanted = {u.strip() for u in Path(a.urls_file).read_text(encoding="utf-8").split() if u.strip()}
        rows = [r for r in rows if r["url"] in wanted]
    print(f"{len(rows)} URLs across {len({r['category'] for r in rows})} categories"
          f"{' (render pass)' if a.render else ''}")

    sem = asyncio.Semaphore(a.concurrency)
    # ONE request in flight per origin, on top of the global cap. A redirect_loop row is 15-30
    # requests and a rate_limit row is a deliberate burst, so 12-way concurrency across a corpus
    # that concentrates 10 rows on httpbin.org was bursting the very hosts it then scored. Measured:
    # the same 50 redirect_loop URLs scored 41/50 and 35/50 on two runs WITH DIFFERENT FAILING SETS,
    # and every row that flipped sits on a host carrying 2-5 multi-hop rows. A benchmark whose
    # result depends on its own scheduling is not measuring the crawler.
    host_sems: dict[str, asyncio.Semaphore] = {}

    def _host_sem(u: str) -> asyncio.Semaphore:
        try:
            host = httpx.URL(u).host or ""
        except Exception:
            host = ""
        return host_sems.setdefault(host, asyncio.Semaphore(1))

    out: list[dict] = []
    async with httpx.AsyncClient(verify=True, http2=False) as client:   # verify STAYS on
        async def one(row, i=0):
            async with sem, _host_sem(row["url"]):
                cat = row["category"]
                if a.paginate:
                    fn = paginate_probe(client, row)
                elif a.render:
                    fn = render_probe(client, row, i)
                elif cat in CAPTCHA_FAMILY:
                    fn = captcha_probe(client, row)        # identification, not blocking
                elif cat == "rate_limit":
                    fn = burst_probe(client, row)          # one request can never show a throttle
                elif cat in ("http_4xx", "http_5xx"):
                    fn = status_probe(client, row)         # scored on POLICY, not on the label
                elif cat == "broken_html":
                    fn = broken_html_probe(client, row)
                elif cat == "domain_change":
                    fn = domain_change_probe(client, row)
                elif cat == "url_space_loop":
                    async def _u(r=row):
                        return urlspace_probe(r)           # frontier guard replay, no network
                    fn = _u()
                else:
                    fn = probe(client, row, a.timeout)
                try:
                    res = await fn
                except Exception as exc:
                    res = {"category": row["category"], "url": row["url"], "verdict": "error",
                           "detected": "", "evidence": f"harness error: {exc}", "error": str(exc)}
                out.append(res)
                mark = {"pass": "ok ", "fail": "FAIL", "needs_render": "rndr",
                        "needs_crawl": "crwl"}.get(res["verdict"], "?   ")
                print(f"  {mark} {res['category']:<18} {res.get('detected',''):<16} {row['url'][:64]}")
        await asyncio.gather(*(one(r, i) for i, r in enumerate(rows)))

    mode = "a" if a.append else "w"
    # ponytail: --out exists so parallel investigations can each measure a category without
    # clobbering the shared results.jsonl the report is built from.
    dest = Path(a.out) if a.out else RESULTS
    with dest.open(mode, encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(redact(r), ensure_ascii=False) + "\n")

    tally: dict[str, dict[str, int]] = {}
    for r in out:
        tally.setdefault(r["category"], {}).setdefault(r["verdict"], 0)
        tally[r["category"]][r["verdict"]] += 1
    print(f"\n{'category':<20} {'pass':>5} {'fail':>5} {'other':>6}")
    for cat in sorted(tally):
        t = tally[cat]
        other = sum(v for k, v in t.items() if k not in ("pass", "fail"))
        print(f"{cat:<20} {t.get('pass', 0):>5} {t.get('fail', 0):>5} {other:>6}")
    total_p = sum(t.get("pass", 0) for t in tally.values())
    total_f = sum(t.get("fail", 0) for t in tally.values())
    print(f"\n{total_p} pass / {total_f} fail  ->  {dest}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Run the crawler edge-case corpus.")
    p.add_argument("--only", help="comma-separated categories")
    p.add_argument("--limit", type=int, help="max URLs per category")
    p.add_argument("--concurrency", type=int, default=12)
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--render", action="store_true", help="render pass via CamoFox")
    p.add_argument("--paginate", action="store_true", help="walk paginated listings")
    p.add_argument("--urls-file", help="restrict to the URLs listed in this file (one per line)")
    p.add_argument("--append", action="store_true", help="append to results.jsonl")
    p.add_argument("--out", help="write rows here instead of results.jsonl")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

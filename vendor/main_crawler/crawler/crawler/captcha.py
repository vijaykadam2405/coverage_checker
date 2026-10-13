"""CAPTCHA detection, token injection and solve verification for the C3 (CamoFox) tier.

Split from camofox_client so the hard part — deciding WHAT is on the page and whether a solve
actually worked — is pure Python over a dict of DOM evidence, and can be tested without a browser.

Three things measured on live pages drove this design:

1. `https://accounts.hcaptcha.com/demo` was classified `recaptcha_v2` with `sitekey=None`.
   hCaptcha writes BOTH `h-captcha-response` and `g-recaptcha-response` (drop-in compat), so a
   detector that checks reCAPTCHA first always wins on an hCaptcha page. With the wrong type and a
   null sitekey no solver task could be built, so the solve dead-ended before it started.
   => hCaptcha is now tested BEFORE reCAPTCHA, and the order is pinned by a test.

2. `scrapingcourse.com/antibot-challenge` (Cloudflare managed challenge, `Cf-Mitigated: challenge`)
   was detected as NO captcha at all: it has no `.cf-turnstile` and no widget iframe, only a
   `cf-turnstile-response` input and `#challenge-form`. The single most common real-world wall was
   invisible to the crawler.

3. `grecaptcha.setResponse()`, `hcaptcha.setResponse()` and `turnstile.setResponse()` — the three
   APIs the old injector called — do not exist. Measured `typeof` on a live hCaptcha page: all three
   `undefined`, as were the `grecaptcha`/`hcaptcha` globals themselves. Injection is therefore pure
   DOM (write the response field) plus firing the site's own callback.

The honesty rule, which is the whole point of `VERIFY_JS`: writing a token into the response field
proves nothing, because WE wrote it. A solve is only confirmed by state the SITE controls.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# ── Solvability classes ───────────────────────────────────────────────────────
# What can actually be done about each wall, as opposed to what we would like to be true.

TOKEN_INJECTABLE = {          # out-of-band solver token + fire the page callback works
    "recaptcha_v2", "recaptcha_v2_invisible", "recaptcha_enterprise", "hcaptcha", "turnstile",
}
WAIT_ONLY = {                 # session-bound: no detached token can satisfy these. Render and wait.
    "cloudflare_managed", "imperva", "kasada", "perimeterx", "akamai",
    # ServicePipe's loader computes a cookie in the page and reloads; there is no detached token to
    # buy, and its own options blob says "is_captcha":false - nothing to solve, only to execute.
    "servicepipe",
}
VENDOR_TASK = {               # solvable, but only via a provider-specific task type
    "datadome", "arkose", "geetest", "awswaf", "mtcaptcha", "keycaptcha", "capy",
    "image_text",             # classic distorted text -> ImageToText / OCR task
}
NOT_INJECTABLE = {            # no DOM sink the site reads; needs execute() override pre-navigation
    "recaptcha_v3",
    # Cloudflare's INTERACTIVE tier (_cf_chl_opt.cType == 'interactive'). Unlike the managed tier it
    # never self-clears, and the checkbox only advances for a client that already scored well enough
    # — so a click we synthesise is accepted and then silently ignored. Measured on zoopla.co.uk:
    # zero Turnstile iframes are ever injected, in Docker AND on the host with a real GPU, so there
    # is not even a widget to click. Waiting cannot help and a paid solver cannot help (no sitekey,
    # and a managed-challenge token is session-bound). Route it to the network-path ladder instead of
    # burning a full render timeout on it. What DOES clear it: scripts/cf_clearance.py.
    "cloudflare_interactive",
}
SELF_SOLVING = {              # proof-of-work / no human task: just let it finish
    # Anubis is spreading fast across academic and government sites — this crawler's own targets.
    # It is a SHA-256 proof-of-work priced to deter bulk crawling, not a puzzle to defeat: paying
    # the few hundred ms of CPU is the cooperative behaviour and it simply passes.
    "friendly_captcha", "altcha", "anubis",
}


def solvability(kind: str | None) -> str:
    if not kind:
        return "none"
    for name, s in (("token_injectable", TOKEN_INJECTABLE), ("wait_only", WAIT_ONLY),
                    ("vendor_task", VENDOR_TASK), ("not_injectable", NOT_INJECTABLE),
                    ("self_solving", SELF_SOLVING)):
        if kind in s:
            return name
    return "unknown"


@dataclass
class Detection:
    kind: str | None = None            # canonical captcha id, e.g. hcaptcha
    sitekey: str | None = None
    solvability: str = "none"
    blocking: bool = False             # page content is WITHHELD until this clears
    all_kinds: list[str] = field(default_factory=list)   # everything seen, priority order
    evidence: dict = field(default_factory=dict)

    @property
    def found(self) -> bool:
        return self.kind is not None


# ── Evidence gathering (one round trip) ───────────────────────────────────────
# Returns raw booleans + sitekeys. Deliberately does NOT decide anything: classification is Python
# so it can be unit-tested against captured evidence without a live browser.

EVIDENCE_JS = r"""
(() => {
  const out = {};
  const q  = s => { try { return document.querySelector(s); } catch(e) { return null; } };
  const has = s => !!q(s);
  const attr = (s,a) => { const e=q(s); return e && e.getAttribute ? e.getAttribute(a) : null; };
  // Every script/iframe/link URL on the page — vendor detection keys off these.
  let urls = [];
  try {
    urls = [...document.querySelectorAll('script[src],iframe[src],link[href]')]
      .map(e => e.src || e.href).filter(Boolean);
  } catch(e) {}
  const u = sub => urls.some(s => s.indexOf(sub) !== -1);
  const body = () => { try { return document.body ? document.body.innerText : ''; } catch(e) { return ''; } };
  const html = () => { try { return document.documentElement.outerHTML; } catch(e) { return ''; } };

  out.title = (document.title || '').slice(0,200);
  out.url = location.href;
  out.text_head = body().slice(0,400);
  out.html_len = html().length;

  // --- Cloudflare managed challenge (interstitial; NOT an embedded widget) ---
  out.cf_challenge_form  = has('#challenge-form');
  out.cf_challenge_stage = has('#challenge-stage') || has('#challenge-running');
  out.cf_chl_widget      = has('[id^="cf-chl-widget-"]');
  out.cf_orchestrate     = u('/cdn-cgi/challenge-platform/');
  out.cf_response_input  = has('input[name="cf-turnstile-response"]');
  out.cf_just_a_moment   = /just a moment|checking your browser|performing security verification|enable javascript and cookies/i.test(out.title + ' ' + out.text_head);
  // Cloudflare names its own challenge TIER here, and the two behave completely differently:
  //   managed / non-interactive -> self-clears once the fingerprint is coherent; just wait.
  //   interactive               -> a checkbox that only advances if the client already scored well
  //                                enough. Clicking it when it did not is silently ignored.
  // Measured: zoopla.co.uk = interactive (inert checkbox, zero Turnstile iframes ever injected);
  // scrapingcourse + dzone = managed (both self-clear, and pass identically with the click DISABLED).
  out.cf_ctype = (function(){ try { return (window._cf_chl_opt && window._cf_chl_opt.cType) || null; }
                              catch(e) { return null; } })();
  // --- Turnstile standalone widget ---
  out.turnstile_widget = has('.cf-turnstile[data-sitekey]') || has('[data-sitekey][class*="turnstile"]');
  out.turnstile_js     = u('challenges.cloudflare.com/turnstile');
  out.sitekey_turnstile = attr('.cf-turnstile[data-sitekey]','data-sitekey')
                       || attr('[class*="turnstile"][data-sitekey]','data-sitekey');

  // --- hCaptcha (checked before reCAPTCHA: it writes g-recaptcha-response too) ---
  out.hcaptcha_widget = has('.h-captcha') || has('[data-hcaptcha-widget-id]');
  out.hcaptcha_resp   = has('textarea[name="h-captcha-response"], textarea[id^="h-captcha-response"]');
  out.hcaptcha_js     = u('hcaptcha.com/1/api') || u('js.hcaptcha.com');
  out.hcaptcha_frame  = u('newassets.hcaptcha.com') || u('hcaptcha.com/captcha');
  out.sitekey_hcaptcha = attr('.h-captcha[data-sitekey]','data-sitekey');
  out.hcaptcha_invisible = (attr('.h-captcha','data-size') === 'invisible');

  // --- reCAPTCHA ---
  out.recaptcha_widget  = has('.g-recaptcha');
  out.recaptcha_resp    = has('textarea[id^="g-recaptcha-response"], textarea[name^="g-recaptcha-response"]');
  out.recaptcha_frame   = u('google.com/recaptcha') || u('recaptcha.net/recaptcha');
  out.recaptcha_js      = u('recaptcha/api.js') || u('recaptcha/releases');
  out.recaptcha_ent_js  = u('recaptcha/enterprise');
  out.recaptcha_cfg     = (typeof window.___grecaptcha_cfg !== 'undefined');
  out.sitekey_recaptcha = attr('.g-recaptcha[data-sitekey]','data-sitekey');
  out.recaptcha_invisible = (attr('.g-recaptcha','data-size') === 'invisible');
  // v3 declares its sitekey in the script URL as ?render=<key> and renders no widget.
  out.recaptcha_v3_render = (() => {
    const s = urls.find(x => x.indexOf('recaptcha') !== -1 && x.indexOf('render=') !== -1);
    if (!s) return null;
    const m = s.match(/[?&]render=([^&]+)/);
    return (m && m[1] !== 'explicit') ? m[1] : null;
  })();

  // --- Other vendors (URL / marker based) ---
  out.datadome  = u('captcha-delivery.com') || u('js.datadome.co') || has('#datadome-captcha');
  out.arkose    = u('funcaptcha.com') || u('arkoselabs.com') || has('#FunCaptcha') || has('[id*="arkose"]');
  out.geetest   = u('geetest.com') || has('.geetest_holder') || has('[class^="geetest_"]');
  out.awswaf    = u('awswaf.com') || has('#awswaf-captcha') || (typeof window.AwsWafIntegration !== 'undefined');
  out.perimeterx = u('perimeterx.net') || u('px-cloud.net') || has('#px-captcha') || /px-captcha/i.test(html().slice(0,20000));
  out.imperva   = u('_Incapsula_Resource') || has('#main-iframe[src*="_Incapsula_Resource"]');
  out.kasada    = u('/149e9513-01fa-4fb0-aad4-566afd725d1b/') || (typeof window.KPSDK !== 'undefined');
  out.akamai    = u('/akam/') || has('#sec-cpt-if') || /_abck|bm_sz/.test(document.cookie || '');
  out.mtcaptcha = u('mtcaptcha.com') || has('#mtcaptcha') || has('.mtcaptcha');
  out.friendly  = u('friendlycaptcha') || has('.frc-captcha');
  out.altcha    = has('altcha-widget') || u('altcha');
  // KeyCAPTCHA sets s_s_c_user_id / s_s_c_web_server_sign script vars before the widget mounts;
  // #div_for_keycaptcha may be empty until then, so key on the config vars too. Measured on
  // 2captcha's keycaptcha demo, which mounts nothing detectable but carries s_s_c_user_id.
  out.keycaptcha = u('keycaptcha.com') || has('#div_for_keycaptcha')
                   || (typeof window.s_s_c_user_id !== 'undefined') || has('[id^="s_s_c_"]');
  out.capy      = u('capy.me') || has('#capy');
  // Classic distorted-text image captcha: an image whose URL says captcha, next to a text input.
  out.image_text = (has('img[src*="captcha" i]') || has('img[id*="captcha" i]'))
                   && has('input[type="text"], input:not([type])');

  // Vendor globals + the APIs the OLD injector called (all three are expected to be undefined —
  // this is recorded so a regression that reintroduces them is visible in the evidence).
  out.g_grecaptcha = typeof window.grecaptcha;
  out.g_hcaptcha   = typeof window.hcaptcha;
  out.g_turnstile  = typeof window.turnstile;
  return out;
})()
"""


# Priority order. First match wins for `kind`.
#   - Cloudflare managed first: it is an interstitial covering the whole page, so nothing else on
#     that DOM is meaningful until it clears.
#   - hCaptcha before reCAPTCHA: hCaptcha writes g-recaptcha-response, so the reverse order
#     misclassifies every hCaptcha page (measured on accounts.hcaptcha.com/demo).
#   - reCAPTCHA v3 last of the reCAPTCHAs: a v2 widget plus a v3 script is a v2 page.
_ORDER = [
    "cloudflare_managed", "imperva", "kasada", "perimeterx", "akamai", "servicepipe", "datadome",
    "arkose", "geetest", "awswaf", "mtcaptcha", "keycaptcha", "capy",
    "hcaptcha", "turnstile",
    "recaptcha_enterprise", "recaptcha_v2", "recaptcha_v3",
    "friendly_captcha", "altcha", "anubis", "image_text",
]

# Walls that withhold the page content entirely (as opposed to a widget sitting on a form).
_BLOCKING = {"cloudflare_managed", "cloudflare_interactive",
             "imperva", "kasada", "perimeterx", "akamai", "datadome", "awswaf", "servicepipe"}


def _cf_tier(ev: dict) -> str:
    """Split the Cloudflare interstitial into its two tiers using Cloudflare's own `cType`.

    They need opposite handling and were previously one class, so an interactive challenge burned a
    full render timeout waiting for something that never self-clears:
      managed / non-interactive -> wait_only, clears itself once the fingerprint is coherent
      interactive               -> not_injectable, no client-side action can clear it
    Falls back to managed when cType is absent (older interstitials, or evidence gathered by an
    HTML-only path), because wait-then-fail is the safer default of the two.
    """
    return ("cloudflare_interactive"
            if str(ev.get("cf_ctype") or "").lower() == "interactive" else "cloudflare_managed")


def _present(ev: dict, kind: str) -> bool:
    g = ev.get
    if kind == "cloudflare_managed":
        # The interstitial, not an embedded widget. A bare cf-turnstile-response input is NOT
        # enough to call it managed — a standalone widget has one too — so require a challenge-page
        # marker AND the absence of a real `.cf-turnstile[data-sitekey]`. The "just a moment" text
        # counts as a marker: on scrapingcourse.com/antibot-challenge that was the clearest signal.
        # Getting this wrong the other way costs real money: a managed challenge classified as a
        # standalone widget would be sent to a paid solver for a token that cannot work on it.
        struct = (g("cf_challenge_form") or g("cf_challenge_stage") or g("cf_orchestrate")
                  or g("cf_chl_widget") or g("cf_just_a_moment"))
        return bool(struct) and not g("turnstile_widget")
    if kind == "turnstile":
        # Standalone widget only. Managed challenge is handled above and must not land here.
        return bool(g("turnstile_widget") or (g("cf_response_input") and not _present(ev, "cloudflare_managed")))
    if kind == "hcaptcha":
        return bool(g("hcaptcha_widget") or g("hcaptcha_resp") or g("hcaptcha_js") or g("hcaptcha_frame"))
    if kind == "recaptcha_enterprise":
        return bool(g("recaptcha_ent_js"))
    if kind == "recaptcha_v2":
        # A v2 page has a real checkbox WIDGET. The google.com/recaptcha frame and the
        # g-recaptcha-response textarea are BOTH shared with v3 — v3 loads the same frame and
        # writes the same response field — so keying v2 on those alone tags every v3 page as v2.
        # That mislabel is not cosmetic: v2 is token_injectable and v3 is not, so it makes the
        # crawler attempt an injection that provably cannot work. Measured on 2captcha's v3 demo
        # (recaptcha_widget=False, recaptcha_v3_render=<key>, recaptcha_resp=True).
        if g("recaptcha_widget"):
            return True
        if g("recaptcha_v3_render"):
            return False                      # it is v3, not v2 — let the v3 branch claim it
        return bool(g("recaptcha_frame") or (g("recaptcha_resp") and not _present(ev, "hcaptcha")))
    if kind == "recaptcha_v3":
        # No checkbox, sitekey declared as ?render=<key> in the script URL.
        return bool(g("recaptcha_v3_render") and not g("recaptcha_widget"))
    if kind == "image_text":
        return bool(g("image_text"))
    if kind == "friendly_captcha":
        return bool(g("friendly"))
    return bool(g(kind))


def _sitekey_for(ev: dict, kind: str) -> str | None:
    if kind == "hcaptcha":
        return ev.get("sitekey_hcaptcha")
    if kind == "turnstile":
        return ev.get("sitekey_turnstile")
    if kind in ("recaptcha_v2", "recaptcha_enterprise"):
        return ev.get("sitekey_recaptcha")
    if kind == "recaptcha_v3":
        return ev.get("recaptcha_v3_render")
    # Cloudflare picks the managed-challenge sitekey itself and embeds it in an obfuscated
    # orchestrate payload. It is genuinely not extractable from the DOM — returning None here is
    # the honest answer, and is why cloudflare_managed is WAIT_ONLY rather than token_injectable.
    return None


def classify(ev: dict | None) -> Detection:
    """Decide what is on the page from gathered DOM evidence. Pure function — no I/O."""
    if not isinstance(ev, dict) or not ev:
        return Detection()
    kinds = [k for k in _ORDER if _present(ev, k)]
    if not kinds:
        return Detection(evidence=ev)
    kind = kinds[0]
    sub = kind
    if kind == "recaptcha_v2" and ev.get("recaptcha_invisible"):
        sub = "recaptcha_v2_invisible"
    elif kind == "cloudflare_managed":
        sub = _cf_tier(ev)          # managed self-clears; interactive never will
    return Detection(kind=sub, sitekey=_sitekey_for(ev, kind), solvability=solvability(sub),
                     blocking=kind in _BLOCKING, all_kinds=kinds, evidence=ev)


# Cheap pre-filter for a distorted-text captcha gate: an <img> whose src/id/alt says "captcha".
# Runs on already-captured HTML (no browser round-trip), so it can gate the expensive OCR-solve on
# the render hot path — only pages that actually carry a captcha image pay for the classify+evaluate.
# "hcaptcha" and "recaptcha" both CONTAIN "captcha", so a branded asset like
# <img src="/img/hcaptcha-hero.png"> matched and routed the page to the image-text OCR solver — a
# wasted C3 render plus a vision-model call, for a widget that has no image to read. Measured on
# hcaptcha.com.
# The exclusion is deliberately the two VENDOR NAMES and nothing more. A general "not preceded by a
# letter" boundary was tried first and broke the exact family this crawler most needs: real gates
# embed the word mid-identifier — parivahan.gov.in serves `src=".../DispplayCaptcha;jsessionid=..."`
# and sso.rajasthan.gov.in serves `src="SSOCaptchaHandler.ashx"`, both preceded by a letter.
_IMG_TEXT_HTML_RE = re.compile(
    r'<img\b[^>]*\b(?:src|id|alt)\s*=\s*["\'][^"\']*(?<!h)(?<!re)captcha', re.I)
# ...but excluding exactly two vendor names is not enough: EVERY other vendor whose brand ends in
# "captcha" fires it. Measured on 2captcha.com, where <img src=".../twocaptcha-logo.svg"
# alt="Logo of «2Captcha»"> matched 47 times and the page was reported as an image-text gate.
# So the image attributes are scanned tag-by-tag and brand tokens are removed first; a hit only
# counts if the word "captcha" SURVIVES that removal. Brand tokens only, deliberately NOT
# `captcha\.com` — that would strip a site's own domain, and democaptcha.com serves a real gate.
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_IMG_ATTR_RE = re.compile(r'\b(?:src|id|alt)\s*=\s*(["\'])(?P<val>[^"\']*)\1', re.I)
_CAPTCHA_BRAND_RE = re.compile(
    r"(?:h|re|two|2|mt|fun|anti|friendly|ar|cut|solve|nope|lemin|tencent|atb|key|geetest|death"
    r"|bypass|multi|smart)[\s\-_]?captchas?|capsolver|captchafox|captchaai", re.I)

# Cloudflare's own challenge-tier label, printed into the interstitial's inline _cf_chl_opt blob.
_CF_CTYPE_RE = re.compile(r"""cType:\s*['"]([a-z-]+)['"]""", re.I)


def looks_image_text(html: str | None) -> bool:
    if not html:
        return False
    for tag in _IMG_TAG_RE.finditer(html):
        for a in _IMG_ATTR_RE.finditer(tag.group(0)):
            v = a.group("val")
            if "captcha" in v.lower() and "captcha" in _CAPTCHA_BRAND_RE.sub("", v).lower():
                return True
    return False


# Widget IDENTIFICATION keys on the vendor SCRIPT URL, which `_HTTP_SIGNATURES` deliberately does
# not — the script stays in the DOM after a challenge is passed, so keying `blocking` on it made
# is_captcha_wall fire on the very page that proved success. Identification is a different
# question from blocking: "what widget is on this page" is still true after it clears, and a v3
# widget never withholds anything at all. Kept apart so neither answer contaminates the other.
_WIDGET_RE = (
    # hCaptcha FIRST — the same invariant _ORDER and classify() already follow, and which this
    # module's own docstring states: hCaptcha writes g-recaptcha-response too, so testing reCAPTCHA
    # first misclassifies every hCaptcha page. This tuple was ordered the other way round.
    ("hcaptcha", re.compile(
        r"(?:js\.)?hcaptcha\.com/1/api\.js|\bnewassets\.hcaptcha\."
        r"|class\s*=\s*[\"'][^\"']*\bh-captcha\b|\bh-captcha-response\b"
        r"|\bdata-hcaptcha-(?:widget-id|sitekey)\b"
        # hCaptcha's first-party "custom domains" build serves the widget from the SITE's own
        # hosts, so no hcaptcha.com URL ever appears. Measured on paypal.com/signup/account:
        # data-captcha-type="hcaptcha" plus .../hcaptcha/hcaptcha_fph.html?siteKey=<uuid>. Its
        # iframe is name="recaptcha" — a legacy misnomer that traps a naive detector.
        r"|data-captcha-type\s*=\s*[\"']hcaptcha[\"']|hcaptcha_fph\.html"
        r"|\bhcaptcha_eval_start_time", re.I)),
    ("recaptcha_v3", re.compile(r"recaptcha/(?:api|enterprise)\.js\?[^\"']*\brender=([\w\-]{30,})", re.I)),
    ("recaptcha_enterprise", re.compile(r"recaptcha/enterprise\.js", re.I)),
    ("recaptcha_v2", re.compile(
        r"(?:google\.com|recaptcha\.net)/recaptcha/api\.js"
        # NOT a bare `g-recaptcha`: that matches a stylesheet rule (see _HTTP_SIGNATURES above).
        r"|class\s*=\s*[\"'][^\"']*\bg-recaptcha\b|\bg-recaptcha-response\b"
        r"|data-captcha-type\s*=\s*[\"']recaptcha[\"']", re.I)),
    ("turnstile", re.compile(
        r"challenges\.cloudflare\.com/turnstile"
        r"|class\s*=\s*[\"'][^\"']*\bcf-turnstile\b|\bcf-turnstile-response\b", re.I)),
)
_SITEKEY_RE = re.compile(r'data-sitekey\s*=\s*["\']([\w\-]{20,})["\']', re.I)


def identify_widgets(html: str | None) -> list[tuple[str, str | None]]:
    """EVERY captcha widget named in the page, in _ORDER priority.

    A page can carry more than one — strikingly.com/s/signup has an image-text gate AND an
    hCaptcha — and returning a single kind silently discards half the answer, which is how a real
    hCaptcha page was reported as image_text."""
    found: dict[str, str | None] = {}
    for kind, rx in _WIDGET_RE:
        m = rx.search(html or "")
        if m:
            found[kind] = m.group(1) if (kind == "recaptcha_v3" and m.groups()) else None
    if "recaptcha_v3" in found:
        found.pop("recaptcha_v2", None)          # a ?render= script is v3, not v2
    sk = _SITEKEY_RE.search(html or "")
    return [(k, found[k] or (sk.group(1) if sk else None)) for k in _ORDER if k in found]


def identify_widget(html: str | None) -> tuple[str | None, str | None]:
    """(kind, sitekey) for the highest-priority captcha WIDGET present on the page — regardless of
    whether it is currently withholding anything. Use for recording and reporting; use
    `detect_http` to decide whether the crawler was actually blocked."""
    w = identify_widgets(html)
    return w[0] if w else (None, None)


# The widget is injected by JS, but the CONFIG naming the vendor and carrying the sitekey is
# server-rendered into the HTML by every SSR/SPA framework. Measured: tumblr, vimeo, spotify,
# instructables and a Nuxt playground all ship the sitekey in plain HTML while shipping no widget.
#
# This is a WEAKER claim than a widget: it proves the SITE provisions that vendor, not that THIS
# page mounts it (en.wikipedia.org ships an hCaptcha sitekey in mw.config on every article). So it
# is reported separately, must never become `kind`, never set `blocking`, and never trigger a paid
# solve. Measured on 70 non-captcha control pages: 5 site-wide configs found, 0 false `kind`.
_CFG_RE = re.compile(
    r'(?P<vendor>h-?captcha|re-?captcha|turnstile)(?P<mid>[A-Za-z0-9_\-]{0,24})'
    r'[\s"\':}{,\]\[]{0,4}(?:(?:site|public|client|checkbox|invisible|prod|web)[_\-]?)?key'
    r'[\s"\':=]{0,14}(?:\{[\s"\']{0,4}value[\s"\':]{0,6})?["\'](?P<key>[\w\-]{20,60})["\']', re.I)
_CFG_ATTR_RE = re.compile(          # paypal: name="_adsRecaptchaSiteKey" value="6Ld..."
    r'name\s*=\s*["\'][^"\']*(?P<vendor>h-?captcha|re-?captcha|turnstile)'
    r'(?P<mid>[A-Za-z0-9_\-]{0,24})key["\'][^>]{0,80}?value\s*=\s*["\'](?P<key>[\w\-]{20,60})["\']',
    re.I)
# A vendor whose own config says it is switched off is not on the page. Measured on vimeo.com/join:
# turnstileSiteKey is present but "isTurnstileEnabled":false.
_CFG_DISABLED_RE = re.compile(
    r'["\']?is(?P<vendor>h-?captcha|re-?captcha|turnstile)\w{0,12}enabled["\']?\s*:\s*false', re.I)
# Shape-check the value so a random GUID cannot be mistaken for a sitekey.
_KEYSHAPE = {
    "recaptcha_v2": re.compile(r"^6L[\w\-]{30,50}$"),
    "recaptcha_v3": re.compile(r"^6L[\w\-]{30,50}$"),
    "hcaptcha": re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.I),
    "turnstile": re.compile(r"^0x[\w\-]{10,}$"),
}


def config_vendors(html: str | None) -> list[tuple[str, str]]:
    """[(kind, sitekey)] for vendors named in a server-rendered JS/JSON config blob.

    WEAKER evidence than identify_widgets: it proves provisioning, not presence. Keep it in its own
    tier — never let it decide `kind` or `blocking`."""
    if not html:
        return []
    off = {m.group("vendor").lower().replace("-", "") for m in _CFG_DISABLED_RE.finditer(html)}
    out: list[tuple[str, str]] = []
    for m in list(_CFG_RE.finditer(html)) + list(_CFG_ATTR_RE.finditer(html)):
        v = m.group("vendor").lower().replace("-", "")
        if v in off:
            continue
        mid, key = m.group("mid").lower(), m.group("key")
        kind = ("turnstile" if v == "turnstile" else "hcaptcha" if v == "hcaptcha"
                else "recaptcha_v3" if "v3" in mid else "recaptcha_v2")
        if _KEYSHAPE[kind].match(key) and (kind, key) not in out:
            out.append((kind, key))
    return out


# ── Token injection ───────────────────────────────────────────────────────────

def injection_js(kind: str, token: str) -> str:
    """JS that writes a solver token into the page and fires the site's own success callback.

    Pure DOM. The vendor `setResponse()` APIs the previous version called do not exist — measured
    `undefined` on a live page for all three vendors.
    """
    tok = json.dumps(token)
    if kind in ("recaptcha_v2", "recaptcha_v2_invisible", "recaptcha_enterprise"):
        fields = ('textarea[id^="g-recaptcha-response"], textarea[name^="g-recaptcha-response"], '
                  'textarea.g-recaptcha-response')
        walk = r"""
    // reCAPTCHA keeps render() params in ___grecaptcha_cfg.clients. The PUBLIC param names
    // (sitekey, callback, size) survive minification because the vendor reads them by name from
    // the site's object — only the nesting keys around them are minified. So: recurse, find the
    // object owning a `sitekey` key, take its `callback`.
    const cfg = window.___grecaptcha_cfg;
    if (cfg && cfg.clients) {
      const seen = new Set(); const stack = [{o: cfg.clients, p: 'clients', d: 0}];
      while (stack.length) {
        const {o, p, d} = stack.pop();
        if (!o || typeof o !== 'object' || d > 6 || seen.has(o)) continue;
        seen.add(o);
        if ('sitekey' in o) {
          let cb = o.callback;
          if (cb === undefined) cb = o['promise-callback'];
          if (cb === undefined) {
            const hit = Object.entries(o).find(([k, v]) => typeof v === 'function'
              && /callback/i.test(k) && !/(expired|error|chalexpired|close|open)/i.test(k));
            if (hit) cb = hit[1];
          }
          if (typeof cb === 'function') call(cb, p + '.callback');
          else if (typeof cb === 'string' && typeof window[cb] === 'function') call(window[cb], cb);
          out.configs++;
          continue;
        }
        for (const [k, v] of Object.entries(o))
          if (v && typeof v === 'object') stack.push({o: v, p: p + '.' + k, d: d + 1});
      }
    }"""
    elif kind == "hcaptcha":
        # hCaptcha populates BOTH response fields; fill both or a drop-in-compat form reads an
        # empty g-recaptcha-response and rejects the submit.
        fields = ('textarea[name="h-captcha-response"], textarea[id^="h-captcha-response"], '
                  'textarea[name="g-recaptcha-response"], textarea[id^="g-recaptcha-response"]')
        walk = """
    for (const w of document.querySelectorAll('.h-captcha')) {
      try { w.setAttribute('data-hcaptcha-response', TOKEN); } catch (e) {}
    }"""
    elif kind == "turnstile":
        fields = 'input[name="cf-turnstile-response"], input[name="g-recaptcha-response"]'
        walk = ""
    else:
        return ""
    return r"""
(() => {
  const TOKEN = %s;
  const out = {kind: %s, fields: 0, callbacks: [], configs: 0, error: null};
  const done = new Set();
  const call = (fn, label) => {
    if (typeof fn !== 'function' || done.has(fn)) return;   // dedupe: data-callback and the
    done.add(fn);                                            // registry often name the same fn,
    try { fn(TOKEN); out.callbacks.push(label); }            // and a double call = double submit
    catch (e) { out.callbacks.push(label + ' THREW:' + e); }
  };
  try {
    for (const f of document.querySelectorAll(%s)) {
      f.value = TOKEN;
      if (f.tagName === 'TEXTAREA') f.innerHTML = TOKEN;   // some handlers read textContent
      out.fields++;
      // Harmless for the vendors (they bind no listener on their own output field) but it is what
      // unlocks a bespoke form that enables its submit button on change.
      try { f.dispatchEvent(new Event('input', {bubbles: true})); } catch (e) {}
      try { f.dispatchEvent(new Event('change', {bubbles: true})); } catch (e) {}
    }
    for (const el of document.querySelectorAll('[data-callback]')) {
      const n = el.getAttribute('data-callback');
      if (n && typeof window[n] === 'function') call(window[n], 'data-callback:' + n);
    }%s
  } catch (e) { out.error = String(e); }
  return out;
})()
""" % (tok, json.dumps(kind), json.dumps(fields), walk)


# Prefer clicking the real control: form.submit() fires neither the submit event nor validation, so
# it silently skips an onsubmit handler that attaches the token or computes a signature.
SUBMIT_JS = r"""
(() => {
  const out = {submitted: false, method: null, form: false, error: null};
  try {
    const resp = document.querySelector('textarea[id^="g-recaptcha-response"], '
      + 'textarea[name="h-captcha-response"], input[name="cf-turnstile-response"]');
    const form = (resp && resp.closest('form')) || document.querySelector('#challenge-form, form');
    out.form = !!form;
    if (!form) return out;
    const btn = form.querySelector('button[type="submit"], input[type="submit"], button:not([type])')
             || form.querySelector('button, input[type="button"]');
    if (btn) { btn.click(); out.submitted = true; out.method = 'click'; return out; }
    if (typeof form.requestSubmit === 'function') {
      form.requestSubmit(); out.submitted = true; out.method = 'requestSubmit'; return out;
    }
    form.submit(); out.submitted = true; out.method = 'submit(bypasses onsubmit)';
  } catch (e) { out.error = String(e); }
  return out;
})()
"""


# ── Verification ──────────────────────────────────────────────────────────────
# Decides on state the SITE controls. A filled response field proves nothing: we filled it.

VERIFY_JS = r"""
(() => {
  const out = {solved: false, reasons: [], token: {}, error: null};
  try {
    const val = s => { const e = document.querySelector(s); return e ? String(e.value || '') : null; };
    for (const [k, s] of [['recaptcha', 'textarea[id^="g-recaptcha-response"]'],
                          ['hcaptcha',  'textarea[name="h-captcha-response"]'],
                          ['turnstile', 'input[name="cf-turnstile-response"]']]) {
      const v = val(s);
      if (v !== null) out.token[k] = {len: v.length, head: v.slice(0, 4)};
    }
    const title = document.title || '';
    const body = document.body ? document.body.innerText : '';
    out.url = location.href;
    out.interstitial = /just a moment|checking your browser|performing security verification/i.test(title + ' ' + body.slice(0, 400));
    out.challenge_form = !!document.getElementById('challenge-form');
    out.success_text = /challenge\s*success|verification success|verified successfully|access granted/i.test(body);
    out.widget_gone = !document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"]');

    if (out.success_text) { out.solved = true; out.reasons.push('success-text'); }
    if (!out.interstitial && !out.challenge_form) {
      out.solved = true; out.reasons.push('no-interstitial');
    }
    // The classic false positive: our own write, nothing else moved. Name it and refuse it.
    const wrote = Object.values(out.token).some(t => t && t.len > 20);
    if (wrote && !out.solved) out.reasons.push('TOKEN-PRESENT-BUT-UNVERIFIED');
  } catch (e) { out.error = String(e); }
  return out;
})()
"""


def verified(v: dict | None) -> bool:
    """True only when the SITE showed change. Never trusts a token we wrote ourselves."""
    if not isinstance(v, dict):
        return False
    if v.get("interstitial") or v.get("challenge_form"):
        return False           # still walled, whatever else the page says
    return bool(v.get("solved"))


# ── Fingerprint coherence ─────────────────────────────────────────────────────
# Why this lives in the captcha module: an incoherent fingerprint is the reason a captcha cannot be
# solved, so it belongs with the diagnosis rather than three layers away.
#
# Measured inside the running CamoFox container:
#     navigator.userAgent  = "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) ... Firefox/135.0"
#     navigator.platform   = "Linux armv81"          <- ARM, on an x86_64 host, claiming x86_64
#
# Real Firefox on x86_64 Linux reports platform "Linux x86_64". Camoufox samples its platform string
# from a device corpus that includes ARM handsets without constraining it to match the UA it built.
# On scrapingcourse.com/antibot-challenge the consequence is exact and repeatable: Cloudflare mounts
# the widget host, then never injects the challenge iframe. The slot holds only the hidden response
# input, the accessibility tree contains no checkbox, and the page sits unchanged for 75s+. It is not
# that the challenge was failed — it was never offered.
#
# This cannot be fixed from here (the value comes from the vendored browser, which exposes no launch
# hook), so the job of this code is to NAME it. An unsolvable captcha with a known cause is a bug
# report; an unsolvable captcha with no cause is a mystery that gets retried forever.

FINGERPRINT_JS = r"""
({ua: navigator.userAgent, platform: navigator.platform,
  cores: navigator.hardwareConcurrency, langs: (navigator.languages||[]).join(','),
  webdriver: navigator.webdriver, plugins: (navigator.plugins||[]).length,
  screen: [screen.width, screen.height].join('x'),
  outer: [window.outerWidth, window.outerHeight].join('x')})
"""

# UA architecture token -> the platform strings a real browser would report with it.
_UA_ARCH = (
    ("x86_64", ("Linux x86_64", "Win32", "MacIntel")),
    ("Win64",  ("Win32",)),
    ("aarch64", ("Linux aarch64", "Linux armv8l", "Linux armv81", "MacIntel")),
    ("armv",   ("Linux armv7l", "Linux armv8l", "Linux armv81")),
)


def fingerprint_flaws(fp: dict | None) -> list[str]:
    """Contradictions an anti-bot check can spot in one line of JS. Empty list = coherent.

    Only reports things that are provably inconsistent WITH EACH OTHER, never 'this looks unusual' —
    an unusual-but-consistent fingerprint is just a rare machine, and flagging those would bury the
    real defects in noise."""
    if not isinstance(fp, dict):
        return []
    flaws: list[str] = []
    ua, plat = str(fp.get("ua") or ""), str(fp.get("platform") or "")
    if ua and plat:
        for token, allowed in _UA_ARCH:
            if token in ua:
                if plat not in allowed:
                    flaws.append(f"userAgent claims {token} but navigator.platform is {plat!r} "
                                 f"(a real browser would report one of {allowed})")
                break
    if fp.get("webdriver"):
        flaws.append("navigator.webdriver is true — the automation flag is not masked")
    return flaws


# ── HTTP-level detection (no browser needed) ──────────────────────────────────
# Most walls announce themselves in the response headers. Recognising one at C1 costs nothing and
# tells the engine WHICH wall it hit, so it can escalate to C3 or give up deliberately instead of
# retrying a 403 that will never change.
#
# Two rules keep this from crying wolf:
#
#  1. Only a DEFINITIVE marker counts. `cf_clearance` in a Set-Cookie means the site sits behind
#     Cloudflare — which is most of the web — not that we were challenged. `cf-mitigated: challenge`
#     means we were challenged. Conflating them would report a wall on every successful fetch.
#  2. Body matching keys on structural markers only — element ids, script paths — never on the word
#     "cloudflare" or "captcha" in prose. This crawler reads defence and procurement pages that
#     discuss security vendors by name; a substring match would flag those as blocked.

# vendor -> (header rules, cookie-name markers, body markers)
#   header rule: (name, expected substring or None for "present at all")
_HTTP_SIGNATURES: dict[str, tuple[tuple, tuple, tuple]] = {
    "cloudflare_managed": ((("cf-mitigated", "challenge"),), (), ("/cdn-cgi/challenge-platform/", "cf_chl_opt", "cf-browser-verification")),
    "datadome":  ((("x-datadome-cid", None), ("x-dd-b", None)), ("datadome=",), ("ct.captcha-delivery.com", "geo.captcha-delivery.com")),
    "perimeterx": ((("x-px-authorization", None),), ("_px3=", "_pxhd="), ("window._pxAppId", "_pxAction")),
    # Akamai's DENIAL page carries none of the Bot Manager sensor markers — no _abck cookie, no
    # bmak. sensor JS — so a 403 "Access Denied / Reference #" page matched nothing and fell
    # through to a bare http_403. Measured on adidas.com/us/confirmed (403, page_owner="AKAMAI",
    # "Reference Error: 18.9df43717...") and g2a.com (403, Server: AkamaiGHost, errors.edgesuite.net).
    # The edgesuite/AkamaiGHost strings appear only on Akamai's own error pages, not on the
    # ordinary pages it fronts, so they do not fire on healthy Akamai-served content.
    "akamai":    ((("x-akamai-session-info", None), ("server", "akamai")), ("_abck=", "bm_sz="),
                  ("bmak.", "#sec-cpt-if", "errors.edgesuite.net",
                   # Akamai's own denial page, quoted from the live body (adidas.com/us/confirmed):
                   #   "page_owner": "AKAMAI"   and   Reference Error: 18.b7f43717.1784908431...
                   # Note the JSON form — an earlier guess at `page_owner = "AKAMAI"` matched
                   # nothing. Both are error-page-only strings, and `blocking` additionally
                   # requires a refusal status, so healthy Akamai-fronted pages stay unaffected.
                   'page_owner": "AKAMAI"', "Reference Error: ")),
    # ServicePipe - a Russian anti-bot/anti-DDoS front, common on .ru media and streaming. It ships
    # a JS challenge as a complete 1.9 KB page with no text at all, so without a signature it reads
    # as an ordinary SPA shell and the crawler spends a render on it. Measured on okko.tv: header
    # 'X-Sp-Crid: 812194816:1', <script src="https://servicepipe.ru/loaders/<hash>.js">, a
    # <js-challenge-loader> element and an empty #id_captcha_frame_div; the <noscript> refreshes to
    # a one-shot path. All three body markers are challenge-page-only - they are gone once the
    # challenge clears - so this cannot fire on content ServicePipe served successfully.
    "servicepipe": ((("x-sp-crid", None),), (),
                    ("servicepipe.ru/loaders/", "<js-challenge-loader", "id_captcha_frame_div")),
    "imperva":   ((("x-iinfo", None), ("x-cdn", "Incapsula")), ("incap_ses_", "visid_incap_", "reese84="), ("_Incapsula_Resource",)),
    "kasada":    ((("x-kpsdk-ct", None), ("x-kpsdk-cd", None)), (), ("149e9513-01fa-4fb0-aad4-566afd725d1b",)),
    # The last two markers are the WAF SDK's own globals, and they are what catches the FIRST-PARTY
    # build: AWS WAF can serve challenge.js from the customer's own domain, in which case neither
    # "awswaf.com" nor "/awswaf/" ever appears. Measured on booking.com (HTTP 202, 3,962 bytes):
    # the script is <script src="https://www.booking.com/__challenge_<id>/.../challenge.js">, the body
    # is <div id="challenge-container"></div>, and the only vendor strings in the whole page are
    # window.awsWafCookieDomainList and AwsWafIntegration. Read without them, an anti-bot wall with
    # an empty <title> looked like a page that merely needed a JS render, and the crawler paid for a
    # browser render of a wall.
    "awswaf":    ((("x-amzn-waf-action", None),), ("aws-waf-token=",),
                  ("awswaf.com", "/awswaf/", "awsWafCookieDomainList", "AwsWafIntegration")),
    "anubis":    ((), ("techaro.lol-anubis-auth",), ('id="anubis_challenge"', "/.within.website/x/cmd/anubis/")),
    # Widget types key on the WIDGET, never on the vendor script URL. Measured on
    # scrapingcourse.com/table-parsing: a clean 200 that loads the turnstile script site-wide was
    # reported as a Turnstile wall, which would have written a false captcha_type onto a page that
    # fetched perfectly. Sites load these scripts globally and mount the widget on one page.
    # Structural markers only. The bare "<name> " form (a class token plus a trailing space) was
    # meant to catch class="g-recaptcha foo", but it also matches a CSS RULE: on nopecha.com a
    # ".g-recaptcha { padding-top:16px }" stylesheet rule at byte 114 beat the real
    # js.hcaptcha.com script tag at byte 462, and the page was reported as reCAPTCHA. Dropping the
    # closing quote instead keeps multi-class matching without matching stylesheets.
    "turnstile": ((), (), ("cf-turnstile-response", 'class="cf-turnstile', "class='cf-turnstile")),
    "hcaptcha":  ((), (), ("h-captcha-response", 'class="h-captcha', "class='h-captcha")),
    "recaptcha_v2": ((), (), ("g-recaptcha-response", 'class="g-recaptcha', "class='g-recaptcha")),
    "arkose":    ((), (), ("client-api.arkoselabs.com", "cdn.funcaptcha.com/fc")),
    "geetest":   ((), (), ("gcaptcha4.geetest.com", "static.geetest.com")),
    "keycaptcha": ((), (), ("keycaptcha.com", "s_s_c_user_id", "s_s_c_web_server_sign")),
}

# A wall only withholds content on these. On a 200 the same marker usually just means the widget is
# embedded in a page we successfully read — worth recording, not worth calling a block.
_BLOCK_STATUSES = {401, 403, 405, 406, 429, 456, 503, 999}


def detect_http(status: int | None, headers: dict | None = None,
                body: str | None = None) -> Detection:
    """Identify an anti-bot wall from the HTTP response alone. No browser, no cost.

    `blocking` is True only when the response actually withheld content, so a page that merely
    embeds a captcha widget is reported without being treated as a failure."""
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    cookie_blob = " ".join(v for k, v in h.items() if k in ("set-cookie", "cookie"))
    snippet = (body or "")[:60000]
    hits: list[str] = []

    for vendor, (hdr_rules, cookies, body_marks) in _HTTP_SIGNATURES.items():
        if any((name in h) and (val is None or val.lower() in h[name].lower())
               for name, val in hdr_rules):
            hits.append(vendor)
            continue
        if any(cm in cookie_blob for cm in cookies):
            hits.append(vendor)
            continue
        if any(bm in snippet for bm in body_marks):
            hits.append(vendor)

    # LinkedIn's bespoke refusal. A status nothing else uses, so it needs no marker.
    if status == 999 and "linkedin" not in hits:
        hits.append("perimeterx")

    # A Cloudflare interstitial is small, refused, and says so in the title.
    if (status in (403, 503) and "cloudflare_managed" not in hits
            and "<title>just a moment" in snippet.lower()):
        hits.append("cloudflare_managed")

    # `/cdn-cgi/challenge-platform/` also serves Cloudflare's JS-Detection beacon (jsd/main.js),
    # which is injected into pages that were NOT challenged at all. Require a real challenge marker
    # or a refusal status before calling it an interstitial. Measured on mediafire.com: a clean 200
    # carrying a real reCAPTCHA widget was labelled cloudflare_managed off that one string.
    if "cloudflare_managed" in hits and status not in _BLOCK_STATUSES:
        if not ("challenge" in h.get("cf-mitigated", "").lower() or "cf_chl_opt" in snippet
                or "cf-browser-verification" in snippet
                or "<title>just a moment" in snippet.lower()):
            hits = [k for k in hits if k != "cloudflare_managed"]

    if not hits:
        return Detection()
    ordered = [k for k in _ORDER if k in hits] or hits
    kind = ordered[0]
    # Cloudflare prints its challenge tier into the interstitial as `cType: '...'`, so the HTTP path
    # can tell managed (self-clears — worth a render) from interactive (never does — don't spend one)
    # without a browser at all. Measured: zoopla=interactive, scrapingcourse/dzone=managed.
    ctype = None
    if kind == "cloudflare_managed":
        m = _CF_CTYPE_RE.search(snippet)
        ctype = m.group(1).lower() if m else None
        kind = _cf_tier({"cf_ctype": ctype})
    # AWS WAF names the action it took in a response header, and its challenge responses carry
    # HTTP **202 Accepted** - a SUCCESS status, so the _BLOCK_STATUSES test alone reads a wall as a
    # clean fetch and the crawler stores a zero-byte page as a document. Measured on
    # espn.com/watch (202, Content-Length 0, 'x-amzn-waf-action: challenge', Server: CloudFront,
    # 'X-Cache: Error from cloudfront') and again on goodreads.com. The header is emitted only when
    # the WAF actually acted, so it IS the refusal rather than a fingerprint of the vendor - which
    # is why it is allowed to override the status, and why the value is checked instead of the mere
    # presence of the name (a 'count' action is telemetry, not a block).
    waf_acted = h.get("x-amzn-waf-action", "").strip().lower() in ("challenge", "captcha", "block")
    return Detection(kind=kind, solvability=solvability(kind),
                     blocking=((status in _BLOCK_STATUSES) or (kind == "awswaf" and waf_acted))
                              and kind in _BLOCKING,
                     all_kinds=ordered,
                     evidence={"status": status, "source": "http", "cf_ctype": ctype})

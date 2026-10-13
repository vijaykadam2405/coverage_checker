"""Detect the wall before trying to climb it — and never claim a solve we cannot prove.

The evidence dicts below are REAL, captured from live pages through CamoFox on :9377, not invented.
Each one pins a defect that was measured in the shipped code:

  hcaptcha demo      -> was classified `recaptcha_v2` with sitekey None, so no solver task could
                        ever be built. hCaptcha writes `g-recaptcha-response` as well as its own
                        field, so any detector that checks reCAPTCHA first loses on every hCaptcha
                        page. Order is a correctness property here, not a style choice.
  cloudflare managed -> was classified as NO captcha at all. `.cf-turnstile` is absent on a managed
                        interstitial, so the single commonest real-world wall was invisible.
  vendor globals     -> grecaptcha/hcaptcha/turnstile all measured `undefined`, and so were the
                        three `setResponse` APIs the old injector called. Injection must be DOM-only.

Runnable directly:  python tests/test_captcha.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import captcha  # noqa: E402

# ── Real captures ─────────────────────────────────────────────────────────────

HCAPTCHA_DEMO = {   # https://accounts.hcaptcha.com/demo
    "title": "hCAPTCHA Demo", "url": "https://accounts.hcaptcha.com/demo",
    "text_head": "?hl=XXX Use this to change hl JS API parameter.", "html_len": 7814,
    "hcaptcha_widget": True, "hcaptcha_resp": True, "hcaptcha_js": True, "hcaptcha_frame": True,
    "sitekey_hcaptcha": "a5f74b19-9e45-40e0-b45d-47ff91b7a6c2",
    # The trap: hCaptcha populates the reCAPTCHA-compat field too.
    "recaptcha_resp": True, "recaptcha_js": True,
    "recaptcha_widget": False, "recaptcha_frame": False, "sitekey_recaptcha": None,
    "g_grecaptcha": "undefined", "g_hcaptcha": "undefined", "g_turnstile": "undefined",
}

CLOUDFLARE_MANAGED = {   # https://www.scrapingcourse.com/antibot-challenge
    "title": "Just a moment...", "url": "https://www.scrapingcourse.com/antibot-challenge",
    "text_head": "www.scrapingcourse.com Performing security verification", "html_len": 27010,
    "cf_just_a_moment": True, "cf_response_input": True, "cf_orchestrate": True,
    "cf_challenge_form": True, "cf_challenge_stage": True,
    "turnstile_widget": False, "turnstile_js": True, "sitekey_turnstile": None,
}

CLEAN_PAGE = {   # https://www.scrapingcourse.com/javascript-rendering — renders fine, no wall
    "title": "JS Rendering Challenge to Learn Web Scraping - ScrapingCourse.com",
    "text_head": "Scraping Course JS Rendering Challenge", "html_len": 28351,
    "turnstile_js": True,   # the script is present site-wide; that alone is NOT a captcha
}

TURNSTILE_STANDALONE = {   # a real embedded widget, e.g. Cloudflare's 3x000...FF test key
    "title": "Login", "turnstile_widget": True, "turnstile_js": True,
    "sitekey_turnstile": "3x00000000000000000000FF", "cf_response_input": True,
}


def t_hcaptcha_is_not_mistaken_for_recaptcha() -> None:
    d = captcha.classify(HCAPTCHA_DEMO)
    assert d.kind == "hcaptcha", f"got {d.kind!r} — the g-recaptcha-response trap caught it again"
    assert d.sitekey == "a5f74b19-9e45-40e0-b45d-47ff91b7a6c2", "sitekey lost; no solver task possible"
    assert d.solvability == "token_injectable"


def t_hcaptcha_is_ordered_before_recaptcha() -> None:
    """Pin the ordering itself: a future edit that sorts _ORDER alphabetically would silently
    reintroduce the misclassification, and every hCaptcha page would go unsolved again."""
    assert captcha._ORDER.index("hcaptcha") < captcha._ORDER.index("recaptcha_v2")


def t_cloudflare_managed_challenge_is_detected() -> None:
    d = captcha.classify(CLOUDFLARE_MANAGED)
    assert d.kind == "cloudflare_managed", f"got {d.kind!r} — the commonest wall is invisible again"
    assert d.blocking, "a managed challenge withholds the whole page; it must be marked blocking"


def t_managed_challenge_is_never_sent_to_a_paid_solver() -> None:
    """Cloudflare mints the managed-challenge token in-session and validates it against the
    fingerprint and cf_clearance cookie. A detached token bought per-sitekey cannot satisfy it, and
    the sitekey is not even extractable from the DOM. Sending it to a solver spends money for a
    token that provably cannot work, so this must classify as wait_only."""
    d = captcha.classify(CLOUDFLARE_MANAGED)
    assert d.solvability == "wait_only"
    assert d.sitekey is None
    assert captcha.injection_js("cloudflare_managed", "tok") == "", "managed challenge got an injector"


def t_standalone_turnstile_is_not_confused_with_the_interstitial() -> None:
    d = captcha.classify(TURNSTILE_STANDALONE)
    assert d.kind == "turnstile"
    assert d.solvability == "token_injectable", "a real widget IS solvable; do not downgrade it"
    assert d.sitekey == "3x00000000000000000000FF"


def t_a_loaded_vendor_script_alone_is_not_a_captcha() -> None:
    """scrapingcourse serves the turnstile script on every page including ones with no challenge.
    Treating a script tag as a wall would make the crawler report captchas on clean pages."""
    d = captcha.classify(CLEAN_PAGE)
    assert not d.found, f"clean page reported {d.kind!r}"
    assert captcha.classify({}).found is False
    assert captcha.classify(None).found is False


def t_injection_uses_dom_fields_not_the_fabricated_vendor_apis() -> None:
    """grecaptcha.setResponse / hcaptcha.setResponse / turnstile.setResponse do not exist — all
    three measured `undefined` on a live page. Calling them was why no solve ever landed."""
    for kind, field in (("recaptcha_v2", "g-recaptcha-response"),
                        ("hcaptcha", "h-captcha-response"),
                        ("turnstile", "cf-turnstile-response")):
        js = captcha.injection_js(kind, "TOK")
        assert field in js, f"{kind} injector does not write {field}"
        assert ".setResponse(" not in js, f"{kind} injector calls a nonexistent vendor API"
        assert '"TOK"' in js, f"{kind} injector never embeds the token"


def t_hcaptcha_injection_fills_both_response_fields() -> None:
    """hCaptcha writes h-captcha-response AND g-recaptcha-response. A drop-in-compat form reads the
    latter, so filling only the former submits an empty token."""
    js = captcha.injection_js("hcaptcha", "TOK")
    assert "h-captcha-response" in js and "g-recaptcha-response" in js


def t_recaptcha_injection_walks_the_config_registry_for_the_callback() -> None:
    """Writing the textarea is not enough: the site waits on the callback passed to
    grecaptcha.render(), which lives only in ___grecaptcha_cfg.clients and never in the DOM."""
    js = captcha.injection_js("recaptcha_v2", "TOK")
    assert "___grecaptcha_cfg" in js and "'sitekey' in o" in js
    assert "expired" in js, "the walk must not fire the expired/error callback as if it were success"


def t_submit_prefers_clicking_over_form_submit() -> None:
    """form.submit() fires neither the submit event nor validation, so it skips an onsubmit handler
    that attaches the token — the request posts without it and the solve is wasted."""
    js = captcha.SUBMIT_JS
    assert js.index("btn.click()") < js.index("form.submit()")
    assert "requestSubmit" in js


def t_our_own_token_write_is_never_accepted_as_proof() -> None:
    """The response field is a sink we control, so a non-empty value after injection is trivially
    true. It is the single biggest source of fake successes."""
    token_written_but_still_walled = {
        "solved": False, "interstitial": True, "challenge_form": True,
        "token": {"turnstile": {"len": 400, "head": "0.AB"}},
        "reasons": ["TOKEN-PRESENT-BUT-UNVERIFIED"],
    }
    assert not captcha.verified(token_written_but_still_walled)
    # Even a page that claims solved is refused while the interstitial is still up.
    assert not captcha.verified({"solved": True, "interstitial": True})
    assert not captcha.verified({"solved": True, "challenge_form": True})
    assert not captcha.verified(None)
    # ...and a genuine clearance is accepted.
    assert captcha.verified({"solved": True, "interstitial": False, "challenge_form": False,
                             "reasons": ["no-interstitial"]})


def t_every_detected_kind_has_an_honest_solvability_verdict() -> None:
    """A kind with no verdict would silently read as 'unknown' at runtime and get no handling."""
    for kind in captcha._ORDER:
        sub = "recaptcha_v2_invisible" if kind == "recaptcha_v2" else kind
        assert captcha.solvability(sub) != "unknown", f"{sub} has no solvability class"
    assert captcha.solvability(None) == "none"


def t_recaptcha_v3_is_admitted_to_be_unsolvable_here() -> None:
    """v3 renders no widget and has no response field the site reads — it takes the token straight
    from a promise. Claiming it is injectable would make the crawler buy tokens it cannot use."""
    assert captcha.solvability("recaptcha_v3") == "not_injectable"
    assert captcha.injection_js("recaptcha_v3", "TOK") == ""


# Real capture from 2captcha.com/demo/recaptcha-v3 through CamoFox: no widget, sitekey only in the
# ?render= script URL, but the shared google.com frame and g-recaptcha-response textarea are both
# present exactly as on a v2 page.
RECAPTCHA_V3_DEMO = {
    "title": "Google reCAPTCHA v3 demo: Sample Form",
    "recaptcha_widget": False, "recaptcha_frame": True, "recaptcha_resp": True,
    "recaptcha_js": True, "recaptcha_v3_render": "6Lcyqq8oAAAAAJE7eVJ3aZp_hnJcI6LgGdYD8lge",
    "sitekey_recaptcha": None,
}


def t_a_v3_page_is_not_misread_as_injectable_v2() -> None:
    """The single most costly captcha misclassification: v3 shares the google.com frame and the
    g-recaptcha-response field with v2, so the old detector called it recaptcha_v2 — which is
    token_injectable. The crawler would then buy and inject a token that v3 has no DOM sink to
    receive, wasting the spend on every v3 page. It must resolve to v3 / not_injectable."""
    d = captcha.classify(RECAPTCHA_V3_DEMO)
    assert d.kind == "recaptcha_v3", f"v3 demo classified as {d.kind!r}"
    assert d.solvability == "not_injectable"
    assert d.sitekey == "6Lcyqq8oAAAAAJE7eVJ3aZp_hnJcI6LgGdYD8lge"


def t_a_real_v2_widget_still_wins_over_a_v3_script() -> None:
    """The fix must not overcorrect: a page with an actual .g-recaptcha checkbox is v2 and
    injectable even if a v3 script is also loaded — the widget is the ground truth."""
    both = dict(RECAPTCHA_V3_DEMO, recaptcha_widget=True, sitekey_recaptcha="6LeIxAcTAAAAAJcZ")
    d = captcha.classify(both)
    assert d.kind == "recaptcha_v2"
    assert d.solvability == "token_injectable"


# ── HTTP-level detection ──────────────────────────────────────────────────────

def t_cloudflare_challenge_is_recognised_from_headers_alone() -> None:
    """Captured from a real scrapingcourse.com/antibot-challenge response. Recognising the wall at
    C1 costs nothing; without it the engine spends a full C3 render to learn the same thing."""
    d = captcha.detect_http(403, {"cf-mitigated": "challenge", "server": "cloudflare"},
                            "<title>Just a moment...</title>")
    assert d.kind == "cloudflare_managed"
    assert d.blocking, "a 403 challenge withheld the page; that is blocking"
    assert d.solvability == "wait_only"


def t_being_behind_cloudflare_is_not_being_blocked_by_it() -> None:
    """Most of the web serves through Cloudflare and sets cf_clearance on perfectly good 200s.
    Treating that cookie as a wall would report a block on every successful fetch."""
    d = captcha.detect_http(200, {"server": "cloudflare", "set-cookie": "cf_clearance=abc; __cf_bm=xyz"},
                            "<html><body>Tender notice: annual procurement</body></html>")
    assert not d.found, f"a normal Cloudflare-fronted 200 was reported as {d.kind!r}"


def t_a_page_that_merely_discusses_anti_bot_vendors_is_not_a_wall() -> None:
    """This crawler reads defence and procurement pages. Substring-matching prose for 'cloudflare'
    or 'captcha' would flag a tender for cybersecurity services as blocked."""
    prose = ("<html><body><h1>Tender: WAF services</h1><p>Bidders must support Cloudflare, "
             "Akamai and Imperva. CAPTCHA integration required. DataDome experience preferred."
             "</p></body></html>")
    d = captcha.detect_http(200, {"content-type": "text/html"}, prose)
    assert not d.found, f"prose about vendors was misread as {d.kind!r} — structural markers only"


def t_an_embedded_widget_on_a_served_page_is_reported_but_not_blocking() -> None:
    """A 200 that contains a captcha widget was still delivered. Recording the widget is useful;
    calling it a block would fail a page we actually retrieved."""
    d = captcha.detect_http(200, {}, '<div class="h-captcha" data-sitekey="x"></div>')
    assert d.kind == "hcaptcha"
    assert not d.blocking


def t_a_site_wide_vendor_script_is_not_a_widget() -> None:
    """Measured live: scrapingcourse.com/table-parsing is a clean 200 that loads the Turnstile
    script on every page. Matching the script URL reported a Turnstile wall on a page that fetched
    perfectly, which would have written a false captcha_type into the document provenance."""
    d = captcha.detect_http(200, {}, '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>')
    assert not d.found, f"a bare script tag was read as {d.kind!r}"
    # ...but the actual widget on the same page still counts.
    d2 = captcha.detect_http(200, {}, '<div class="cf-turnstile" data-sitekey="x"></div>')
    assert d2.kind == "turnstile"


def t_vendor_specific_headers_are_each_recognised() -> None:
    for headers, cookies, expect in (
        ({"x-datadome-cid": "abc"}, "", "datadome"),
        ({"x-px-authorization": "3"}, "", "perimeterx"),
        ({"x-kpsdk-ct": "z"}, "", "kasada"),
        ({"x-amzn-waf-action": "captcha"}, "", "awswaf"),
        ({"x-cdn": "Incapsula"}, "", "imperva"),
        ({"x-akamai-session-info": "v"}, "", "akamai"),
        ({}, "set-cookie:_abck=xyz", "akamai"),
    ):
        hh = dict(headers)
        if cookies:
            hh["set-cookie"] = cookies.split(":", 1)[1]
        d = captcha.detect_http(403, hh, "")
        assert d.kind == expect, f"{headers or cookies} -> {d.kind!r}, wanted {expect}"


def t_anubis_proof_of_work_is_detected_and_not_treated_as_an_enemy() -> None:
    """Anubis is spreading fast on academic and government sites — exactly this crawler's targets.
    It is proof-of-work: there is nothing to bypass, the render just pays the CPU cost. Classifying
    it as a block would abandon pages that would have loaded on their own."""
    d = captcha.detect_http(200, {}, '<script id="anubis_challenge">{}</script>')
    assert d.kind == "anubis"
    assert d.solvability == "self_solving"


# ── Fingerprint coherence ─────────────────────────────────────────────────────

# Captured from the running CamoFox container on an x86_64 host.
REAL_CAMOFOX_FP = {
    "ua": "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0",
    "platform": "Linux armv81", "cores": 16, "langs": "en-US",
    "webdriver": False, "plugins": 5, "screen": "1920x1080", "outer": "1920x1056",
}


def t_the_camofox_arch_contradiction_is_caught() -> None:
    """The UA claims Linux x86_64 while navigator.platform reports ARM. That pair is checkable in
    one line of JS, and on scrapingcourse.com/antibot-challenge Cloudflare's response is to mount
    the widget host and then never inject the challenge iframe — the slot holds only the hidden
    response input and the page is unchanged after 75s. The challenge is not failed, it is never
    offered, so no amount of waiting or retrying recovers the page."""
    flaws = captcha.fingerprint_flaws(REAL_CAMOFOX_FP)
    assert flaws, "the measured ARM/x86_64 contradiction is no longer detected"
    assert "armv81" in flaws[0] and "x86_64" in flaws[0]


def t_a_coherent_fingerprint_raises_nothing() -> None:
    """False positives here would be worse than silence: every C3 render would carry a bogus
    warning and the real defect would be invisible in the noise."""
    for fp in ({"ua": "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Firefox/135.0",
                "platform": "Linux x86_64", "webdriver": False},
               {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/140",
                "platform": "Win32", "webdriver": False},
               {"ua": "Mozilla/5.0 (Linux; aarch64) Firefox/135.0",
                "platform": "Linux aarch64", "webdriver": False}):
        assert captcha.fingerprint_flaws(fp) == [], f"false positive on {fp['platform']}"
    assert captcha.fingerprint_flaws(None) == []
    assert captcha.fingerprint_flaws({}) == []


def t_an_unmasked_automation_flag_is_reported() -> None:
    fp = dict(REAL_CAMOFOX_FP, platform="Linux x86_64", webdriver=True)
    assert any("webdriver" in f for f in captcha.fingerprint_flaws(fp))


# ── Engine wiring ─────────────────────────────────────────────────────────────

def t_a_captcha_wall_is_not_filed_as_a_crashed_browser() -> None:
    """C3 returning nothing used to `return False` with no reason recorded, so every captcha wall
    reached the caller as a generic render failure. "The browser died" and "a captcha stopped us"
    need completely different fixes, and the backlog could not tell them apart."""
    import inspect
    from crawler import async_engine as ae
    # The reason logic now lives in _flag_captcha_limitation (shared by the C3-gave-nothing path AND
    # the rendered-interstitial path), which _try_camofox_fallback calls when a captcha is detected.
    src = inspect.getsource(ae.AsyncEngine._try_camofox_fallback)
    i_check = src.index("if captcha_info.captcha_type:")
    i_ret = src.index("return False", i_check)
    assert i_check < i_ret, "the captcha must be flagged before the early return"
    assert "_flag_captcha_limitation" in src[i_check:i_ret], "detected captcha must be flagged"

    flag = inspect.getsource(ae.AsyncEngine._flag_captcha_limitation)
    assert "errors.NEEDS_CAPTCHA_SOLVER" in flag and "errors.CAPTCHA_FAILED" in flag
    assert "captcha_info.solvability" in flag, "the detail must say which wall, not just 'captcha'"
    assert "_log_captcha_limitation" in flag, "the URL must be maintained in the captcha log"


def t_captcha_reasons_are_retryable_and_explain_themselves() -> None:
    """A reason with no detail line shows up in the batch report as a bare slug, which tells the
    operator nothing about what would actually change the outcome."""
    from crawler import errors
    for r in (errors.CAPTCHA_FAILED, errors.NEEDS_CAPTCHA_SOLVER):
        assert errors.bucket_reason(r) == "retryable", f"{r} bucketed as terminal"
        assert len(errors.reason_detail(r)) > 60, f"{r} has no useful detail line"
    # ...and it must stay distinguishable from a genuine browser crash.
    assert errors.CAPTCHA_FAILED != errors.RENDER_CRASH


def t_http_detection_survives_junk_input() -> None:
    for args in ((None, None, None), (200, {}, ""), (403, None, None), (0, {"x": "y"}, "  ")):
        assert not captcha.detect_http(*args).found


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} CAPTCHA TESTS PASSED")


def test_captcha_suite() -> None:
    """pytest only collects `test_*`, and this file follows the repo's `t_*` + main() convention —
    so without this wrapper `pytest tests/` silently collects zero tests from here and reports
    success. A test that never runs is indistinguishable from one that passes."""
    main()


if __name__ == "__main__":
    main()

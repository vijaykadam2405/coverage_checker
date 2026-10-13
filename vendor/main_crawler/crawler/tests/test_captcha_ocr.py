"""Image-text captcha OCR-solve: the local vision model reads a distorted-text gate (ASP.NET
CaptchaImage.axd and the like) and types the answer in the C3 stealth browser, so the crawler stores
the real page not the gate. The gate is detected in the Chromium render (async_engine flags it) and
solved in camofox (crawler.camofox_client._solve_image_text) — the browser the site lets through.

Proven live on joinindianarmy.nic.in (default.aspx -> Authentication.aspx). These tests pin the pure
pre-filter and the camofox retry/submit loop with a fake client + stubbed OCR — no browser, no Ollama.

Runnable directly:  python tests/test_captcha_ocr.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import camofox_client as cc  # noqa: E402
from crawler import captcha  # noqa: E402

# The real gate markup (trimmed): <img alt="Captcha" src=".../CaptchaImage.axd?guid=...">, txtCaptchaCode.
GATE_HTML = ('<html><body>Please write text as shown in following image to enter into the website.'
             '<img id="" alt="Captcha" src="https://www.joinindianarmy.nic.in/CaptchaImage.axd?guid=abc">'
             '<input id="txtCaptchaCode" name="txtCaptchaCode" type="text">'
             '<input type="submit" name="btnSubmit" value="ENTER WEBSITE"></body></html>')
NORMAL_HTML = '<html><body><img src="/img/hero.jpg"><p>About the Indian Army</p></body></html>'


class _FakeResp:
    def __init__(self, result):
        self._result = result

    @property
    def status_code(self):
        return 200

    def json(self):
        return {"result": self._result}


class _FakeClient:
    """Answers the C3 /evaluate calls: grab -> data URL until the correct code is submitted, then
    'NONE' (inside). Records each filled answer and each submit click."""

    def __init__(self, secret: str):
        self.secret = secret
        self.solved = False
        self.filled: list[str] = []
        self.clicks = 0

    def post(self, url, json=None, headers=None):
        expr = (json or {}).get("expression", "")
        if expr == cc._IMG_TEXT_GRAB_JS:
            return _FakeResp("NONE" if self.solved else "data:image/png;base64,SU1H")
        if "i.value=" in expr:                          # the fill expression (answer inlined)
            m = re.search(r'i\.value=("(?:[^"\\]|\\.)*")', expr)
            ans = json_loads(m.group(1)) if m else ""
            self.filled.append(ans)
            self._last = ans
            return _FakeResp(True)
        if "input[type=submit]" in expr:                # the submit click
            self.clicks += 1
            if getattr(self, "_last", None) == self.secret:
                self.solved = True
            return _FakeResp(True)
        return _FakeResp(None)


def json_loads(s):
    return json.loads(s)


class _patch:
    """Save/restore module attrs so a test's monkeypatching never leaks into the shared pytest
    process (a global `cc._settle = lambda` once broke test_cf_click + test_crawler_api)."""

    def __init__(self, **kw):
        self.kw = kw

    def __enter__(self):
        self.orig = {k: getattr(cc, k) for k in self.kw}
        for k, v in self.kw.items():
            setattr(cc, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.orig.items():
            setattr(cc, k, v)


def _run(secret, ocr_answers, max_tries=3):
    """Solve with a fake client; ocr_answers is the sequence the vision model 'reads'."""
    import os
    it = iter(ocr_answers)
    os.environ["CRAWLER_OCR_MAX_TRIES"] = str(max_tries)
    c = _FakeClient(secret)
    with _patch(_ocr_image_text=lambda data_url: next(it, ""),
                _settle=lambda *a, **k: None):
        ok = cc._solve_image_text(c, "http://x", "u", "tab")
    return ok, c


def t_pre_filter_only_fires_on_a_captcha_image() -> None:
    """The cheap html gate (async_engine uses it) must catch the CaptchaImage.axd <img> but not a
    normal page, or every rendered page pays for a classify+evaluate on the hot path."""
    assert captcha.looks_image_text(GATE_HTML) is True
    assert captcha.looks_image_text(NORMAL_HTML) is False
    assert captcha.looks_image_text("") is False
    assert captcha.looks_image_text(None) is False
    # "hcaptcha" and "recaptcha" both CONTAIN "captcha", so a branded asset used to match and send
    # the page to the OCR solver — a C3 render plus a vision-model call for a widget with no image
    # to read. Measured on hcaptcha.com, whose homepage carries exactly this.
    assert captcha.looks_image_text('<img src="/img/hcaptcha-hero.png" alt="hCaptcha">') is False
    assert captcha.looks_image_text('<img src="/assets/recaptcha-badge.svg">') is False
    # …while the real gates still fire, including a bare filename with no leading path.
    assert captcha.looks_image_text('<img src="captcha.php?id=9">') is True
    assert captcha.looks_image_text('<img src="/inc/securimage/captcha_show.php">') is True
    # Real gates embed the word MID-IDENTIFIER, preceded by a letter. A general "not preceded by a
    # letter" boundary was tried for the hcaptcha fix above and silently broke both of these —
    # which is the .gov.in family this crawler most depends on. Live markup, quoted.
    assert captcha.looks_image_text(
        '<img id="form_rcdl:j_idt78" src="/rcdlstatus/DispplayCaptcha;jsessionid=A1?noise_cd=2" />'
    ) is True, "parivahan.gov.in"
    assert captcha.looks_image_text(
        '<img id="cpBody_ssoCaptcha_imgCaptcha" src="SSOCaptchaHandler.ashx">'
    ) is True, "sso.rajasthan.gov.in"


def t_first_try_solve_types_and_submits() -> None:
    ok, c = _run("76S84C", ["76S84C"])
    assert ok is True
    assert c.filled == ["76S84C"], "the OCR answer must be typed into the field"
    assert c.clicks == 1, "the submit must be clicked exactly once"


def t_retries_a_misread_then_succeeds() -> None:
    """OCR is not 100% on distorted text — a wrong read must re-grab a fresh captcha and retry, not
    give up. (Measured live: a run misread once, then entered on the 2nd read.)"""
    ok, c = _run("DZR4KZ", ["WRONG1", "DZR4KZ"])
    assert ok is True
    assert c.clicks == 2, "one failed attempt, then the successful one"


def t_gives_up_after_max_tries() -> None:
    """A gate we can't read must return False (caller records the wall / skips) rather than loop
    forever or report a false success."""
    ok, c = _run("SECRET", ["no", "no", "no", "no", "no"], max_tries=3)
    assert ok is False
    assert c.clicks == 3, "exactly max_tries attempts, no more"


def t_no_image_means_already_inside() -> None:
    """If the grab finds no captcha image (already past the gate), report solved without typing."""
    c = _FakeClient("X"); c.solved = True
    with _patch(_settle=lambda *a, **k: None):
        assert cc._solve_image_text(c, "http://x", "u", "tab") is True
    assert c.filled == [] and c.clicks == 0


def t_solve_captcha_routes_image_text_to_the_ocr_solver() -> None:
    """solve_captcha must dispatch an image_text detection to the local OCR solver (solvability
    vendor_task otherwise dead-ends at the commercial path, which has no image_text task)."""
    import os
    os.environ["CRAWLER_CAPTCHA_OCR"] = "1"
    called = {"n": 0}
    def fake_solve(c, base, user, tab_id):
        called["n"] += 1
        return True
    with _patch(detect_captcha=lambda *a: cc.CaptchaInfo(
                    captcha_type="image_text", solvability="vendor_task", blocking=True),
                _solve_image_text=fake_solve):
        info = cc.solve_captcha(None, "http://x", "u", "tab", "http://page")
    assert called["n"] == 1, "image_text must be routed to _solve_image_text"
    assert info.solved is True and info.solver == "local_vlm_ocr" and info.cost_usd == 0.0


def t_cloudflare_interactive_is_split_from_managed() -> None:
    """Cloudflare's two interstitial tiers need OPPOSITE handling and used to be one class:

      managed / non-interactive -> self-clears once the fingerprint is coherent (wait_only)
      interactive               -> never self-clears; the checkbox only advances a client that
                                   already scored well enough, so our click is accepted and then
                                   silently ignored (measured on zoopla: zero Turnstile iframes are
                                   ever injected, in Docker AND on a real GPU). not_injectable.

    Collapsing them made the crawler spend a full render timeout on a wall that provably cannot
    clear. cType is Cloudflare's own label, measured live: zoopla=interactive, scrapingcourse and
    dzone=managed.
    """
    inter = ("<html><head><title>Just a moment...</title></head><body>"
             "<script>window._cf_chl_opt={cvId:'3',cType:'interactive',cRay:'a20'};</script>"
             "<script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'></script>"
             "</body></html>")
    managed = inter.replace("cType:'interactive'", "cType:'managed'")

    di = captcha.detect_http(403, {"cf-mitigated": "challenge"}, inter)
    dm = captcha.detect_http(403, {"cf-mitigated": "challenge"}, managed)
    assert di.kind == "cloudflare_interactive", f"interactive misread as {di.kind}"
    assert di.solvability == "not_injectable", "interactive must not be treated as waitable"
    assert dm.kind == "cloudflare_managed" and dm.solvability == "wait_only"
    assert di.blocking and dm.blocking, "both tiers withhold the page"

    # Same split from live DOM evidence (EVIDENCE_JS reads window._cf_chl_opt.cType).
    base = {"cf_challenge_form": True, "cf_orchestrate": True, "cf_just_a_moment": True,
            "title": "Just a moment...", "text_head": "Verifying you are human"}
    assert captcha.classify({**base, "cf_ctype": "interactive"}).kind == "cloudflare_interactive"
    assert captcha.classify({**base, "cf_ctype": "managed"}).kind == "cloudflare_managed"
    # Absent cType (older interstitials) must fall back to managed — wait-then-fail is the safer
    # default, since calling a winnable challenge unwinnable silently drops pages we could have had.
    assert captcha.classify(base).kind == "cloudflare_managed"


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} CAPTCHA-OCR TESTS PASSED")


def test_captcha_ocr_suite() -> None:
    main()


if __name__ == "__main__":
    main()

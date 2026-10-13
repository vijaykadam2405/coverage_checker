"""Every JS expression we send to CamoFox's /evaluate must actually parse.

A bare-string-splice bug once made the detect expression emit an invalid object literal, so every
captcha-detect call returned 500 and the C3 tier never detected a single captcha. Nothing caught it
because the failure was swallowed into an empty CaptchaInfo() — a silent, total loss of a feature.

The offline classifier tests cannot catch that class of bug: they exercise Python over a dict of
evidence and never touch the JavaScript that produces the dict. So parse it for real, with node.

Runnable directly:  python tests/test_camofox_js.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import captcha  # noqa: E402

NODE = shutil.which("node")


def parses(expr: str) -> tuple[bool, str]:
    """True if `expr` is a valid JS expression. new Function() parses without executing it."""
    if not NODE:  # ponytail: structural fallback; the real check needs a JS parser
        return (expr.count("(") == expr.count(")") and expr.count("{") == expr.count("}"),
                "no node — bracket-balance check only")
    src = f"new Function({json.dumps('return (' + expr + ')')}); console.log('ok')"
    p = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
    return p.returncode == 0, ((p.stderr or "").strip().splitlines() or [""])[0]


def main() -> None:
    checked = []

    from crawler import camofox_client as cc  # noqa: E402
    for name, expr in (("EVIDENCE_JS", captcha.EVIDENCE_JS),
                       ("SUBMIT_JS", captcha.SUBMIT_JS),
                       ("VERIFY_JS", captcha.VERIFY_JS),
                       ("FINGERPRINT_JS", captcha.FINGERPRINT_JS),
                       ("_CF_WIDGET_JS", cc._CF_WIDGET_JS)):
        ok, err = parses(expr)
        assert ok, f"{name} emits invalid JS: {err}\n{expr[:400]}"
        checked.append(name)

    # Injection JS is built per captcha type with an embedded token — the token is attacker-adjacent
    # data from a third-party solver, so it is json.dumps()'d rather than concatenated. Prove a
    # hostile token cannot break out of the string and change the meaning of the expression.
    for kind in sorted(captcha.TOKEN_INJECTABLE):
        for token in ("normaltoken", '"); alert(1); ("', "back\\slash", "new\nline", "</script>"):
            expr = captcha.injection_js(kind, token)
            assert expr, f"injection_js({kind!r}) returned nothing"
            ok, err = parses(expr)
            assert ok, f"injection_js({kind!r}, {token!r}) emits invalid JS: {err}"
        checked.append(f"injection_js:{kind}")

    # Types that cannot be token-injected must return an empty string, not broken JS.
    for kind in ("cloudflare_managed", "recaptcha_v3", "datadome", "nonsense"):
        assert captcha.injection_js(kind, "t") == "", f"{kind} produced an injector it should not"

    # Every evidence key the classifier reads must be produced by the evidence expression, or that
    # signature is dead code that silently never fires.
    for key in ("cf_challenge_form", "cf_just_a_moment", "turnstile_widget", "hcaptcha_widget",
                "recaptcha_widget", "recaptcha_v3_render", "sitekey_hcaptcha", "sitekey_turnstile",
                "sitekey_recaptcha", "datadome", "arkose", "geetest", "awswaf", "kasada"):
        assert f"out.{key}" in captcha.EVIDENCE_JS or f'"{key}"' in captcha.EVIDENCE_JS, \
            f"classifier reads {key!r} but EVIDENCE_JS never sets it"

    print(f"ALL CAMOFOX JS TESTS PASSED — {len(checked)} expressions "
          f"({'node' if NODE else 'bracket-balance fallback'})")


def test_camofox_js_parses() -> None:
    """See test_captcha.py: without a `test_*` wrapper pytest collects nothing from this file, and
    the JS syntax check — the one thing that catches the bug this file exists for — never runs."""
    main()


if __name__ == "__main__":
    main()

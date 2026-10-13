"""Fetch a URL that sits behind a Cloudflare managed challenge ("Just a moment...").

    python scripts/solve_challenge.py <url> [--html out.html]

Run this on the WINDOWS HOST, not in Docker. That is the whole point — see below.

WHY NOT C3 / CAMOFOX
--------------------
Measured 2026-07-22 against https://www.scrapingcourse.com/antibot-challenge, which never yields
to the containerised stack. Every one of these failed, all with the same symptom (title stays
"Just a moment...", ZERO Turnstile iframes ever injected, no cf_clearance cookie — Cloudflare
hard-blocks *before* offering a widget, so no click technique can apply):

    Camoufox 135, coherent Windows fingerprint, Xvfb geometry fixed   FAIL
    Camoufox 152 (current Firefox)                                    FAIL
    Camoufox + residential US exit IP                                 FAIL
    Chromium (SeleniumBase UC) + OS-level PyAutoGUI click, in Docker   FAIL
    ...same, with WebGL enabled and --gpus all                        FAIL

The decisive difference is NOT the engine and NOT the IP — it is the graphics stack:

    in Docker : webgl = "ANGLE (Mesa, llvmpipe (LLVM 19.1.7 256 bits), OpenGL ES 3.2)"  -> blocked
    on host   : webgl = "ANGLE (Intel, Intel(R) Graphics ... Direct3D11 vs_5_0 ps_5_0)" -> PASSES

llvmpipe is a software rasteriser, and it cannot be faked away in JS: spoofing the renderer STRING
leaves the capability surface (extension list, shader precision, MAX_TEXTURE_SIZE) still reporting
software, and those are cross-checked. It cannot be fixed inside Docker on this machine either —
WSL2 passes CUDA/compute through to containers but not OpenGL, and /usr/lib/wsl (the d3d12 path)
is not exposed to the Docker VM. Verified, not assumed.

The host also supplies the other half: PyAutoGUI clicks through the real OS input queue, so the
event carries a genuine screenX/screenY and isTrusted. CDP's Input.dispatchMouseEvent sets
screenX == clientX, which Cloudflare has fingerprinted since Feb 2025.

RESULT: 4/4 successful runs, 15-45s each. The site returns its own confirmation string,
"You bypassed the Antibot challenge! :D".

COST: this drives a REAL Chrome window and moves the REAL mouse pointer for the click. It is not
suitable for bulk crawling and will interrupt whoever is at the keyboard. Use it for the handful
of URLs the crawler records as needs_network_path, not as a crawl tier.

Setup (once):
    python -m venv ucvenv && ucvenv/Scripts/python -m pip install seleniumbase pyautogui
    ucvenv/Scripts/python -m seleniumbase get uc_driver
"""
from __future__ import annotations

import argparse
import json
import sys
import time

CHALLENGE = ("just a moment", "checking your browser", "cf-chl", "challenge-platform",
             "verifying you are human", "enable javascript and cookies")

GL_JS = ("var c=document.createElement('canvas');"
         "var g=c.getContext('webgl')||c.getContext('experimental-webgl');"
         "if(!g)return 'no-webgl';"
         "var d=g.getExtension('WEBGL_debug_renderer_info');"
         "return d?g.getParameter(d.UNMASKED_RENDERER_WEBGL):'no-debug-ext';")


def solve(url: str, attempts: int = 3) -> dict:
    """Return {ok, title, chars, html, webgl, elapsed}. ok=True only when the challenge is GONE."""
    from seleniumbase import sb_cdp

    t0 = time.time()
    out: dict = {"ok": False, "title": "", "chars": 0, "html": "", "webgl": "", "elapsed": 0.0}
    # CDP Mode + solve_captcha(), NOT uc_gui_click_captcha(). The latter was superseded upstream in
    # 2025: it was built when Turnstile lived in an iframe and mapped the widget to SCREEN coords for
    # a PyAutoGUI click, but Turnstile now renders inside a CLOSED SHADOW ROOT that no DOM traversal
    # reaches. solve_captcha() reads the OUTER light-DOM container's box model and dispatches the
    # click at viewport coordinates, which lands on whatever is topmost — shadow root included.
    # It also needs no PyAutoGUI, so it no longer hijacks the real mouse pointer.
    # MEASURED 2026-07-24 on zoopla.co.uk/to-rent (cType='interactive'): the old path left the
    # checkbox inert even on a real GPU; this one returned 464,358 chars of real listings.
    sb = sb_cdp.Chrome(url, incognito=True)
    try:
        for i in range(attempts):
            time.sleep(4)
            if not any(m in (sb.get_title() or "").lower() for m in CHALLENGE):
                break
            try:
                sb.solve_captcha()
            except Exception as exc:                      # noqa: BLE001 — a failed click is data
                print(f"  click {i + 1}: {type(exc).__name__}: {str(exc)[:90]}", file=sys.stderr)
            time.sleep(4)
            if i + 1 < attempts and any(m in (sb.get_title() or "").lower() for m in CHALLENGE):
                sb.open(url)                              # re-arm: a fresh challenge often passes

        time.sleep(2)
        title = sb.get_title() or ""
        html = sb.get_page_source() or ""
        # Judge on CONTENT, never on "no exception": the failure mode is a 200 that is still the
        # interstitial, which looks like success to any status-code check.
        marker = any(m in (title + " " + html[:4000]).lower() for m in CHALLENGE)
        try:
            out["webgl"] = str(sb.evaluate(GL_JS))[:90]
        except Exception:                                 # noqa: BLE001
            out["webgl"] = "probe-failed"
        out.update(ok=(not marker and len(html) > 1000), title=title, chars=len(html), html=html,
                   elapsed=round(time.time() - t0, 1))
    finally:
        try:
            sb.driver.stop()
        except Exception:                                 # noqa: BLE001
            pass
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch a Cloudflare-challenged URL via host Chrome.")
    ap.add_argument("url")
    ap.add_argument("--html", help="write the retrieved page HTML here")
    ap.add_argument("--attempts", type=int, default=3)
    a = ap.parse_args(argv)

    r = solve(a.url, attempts=a.attempts)
    if a.html and r["html"]:
        with open(a.html, "w", encoding="utf-8") as f:
            f.write(r["html"])
    print(json.dumps({k: v for k, v in r.items() if k != "html"}, indent=1))
    if r["ok"] and "llvmpipe" in r["webgl"]:
        print("note: passed despite a software renderer — unexpected, worth re-checking",
              file=sys.stderr)
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

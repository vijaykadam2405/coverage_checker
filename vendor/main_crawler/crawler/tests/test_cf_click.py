"""The simple solution to the interactive Cloudflare challenge: a coordinate click.

An interactive Turnstile ("Verify you are human") never self-clears, and its checkbox is inside a
cross-origin challenges.cloudflare.com iframe that no selector can reach. The CamoFox /click endpoint
only accepted ref/selector, so it was unclickable — which is why C3 sat on the interstitial for 75s+
and recorded the page as blocked. A small server patch wires page.mouse (a TRUSTED, humanized click)
to a pixel coordinate; clicking the checkbox that way clears the challenge (verified live on
scrapingcourse.com/antibot-challenge: "You bypassed the Antibot challenge!").

These tests pin the client behaviour and the presence of the server patch without needing a browser.

Runnable directly:  python tests/test_cf_click.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import camofox_client as cc  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]


class _FakeResp:
    def __init__(self, status: int, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    """Records posts and answers the evaluate (widget box) then the click."""

    def __init__(self, slot, click_ok=True):
        self.slot = slot
        self.click_ok = click_ok
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json))
        if url.endswith("/evaluate"):
            return _FakeResp(200, {"result": self.slot})
        if url.endswith("/click"):
            if not self.click_ok:                       # unpatched server: coordinates rejected
                return _FakeResp(400, {"error": "ref or selector required"})
            coords = (json or {}).get("coordinates") or {}
            return _FakeResp(200, {"clicked": True, "method": "coordinates",
                                   "x": coords.get("x"), "y": coords.get("y")})
        return _FakeResp(200, {})


def t_checkbox_click_targets_the_left_of_the_widget() -> None:
    """The checkbox renders ~30px in from the widget's left edge, vertically centred. A click at the
    widget's centre would miss it (that's the label/logo), so the offset matters."""
    slot = {"x": 832, "y": 304, "h": 69}
    c = _FakeClient(slot)
    assert cc._click_turnstile_checkbox(c, "http://x", "u", "tab") is True
    click = next(j for url, j in c.calls if url.endswith("/click"))
    x, y = click["coordinates"]["x"], click["coordinates"]["y"]
    assert x == 862, f"x={x}: not aimed at the checkbox (left edge + 30)"
    assert y == 338, f"y={y}: not vertically centred in the widget"


def t_a_coordinate_click_is_sent_not_a_selector() -> None:
    """The whole point: the checkbox is in a cross-origin iframe, so it must be a coordinate click.
    A regression back to selector/body clicking would silently stop solving Cloudflare."""
    c = _FakeClient({"x": 500, "y": 300, "h": 60})
    cc._click_turnstile_checkbox(c, "http://x", "u", "tab")
    click = next(j for url, j in c.calls if url.endswith("/click"))
    assert "coordinates" in click and "selector" not in click and "ref" not in click


def t_unpatched_server_is_a_safe_no_op() -> None:
    """On a CamoFox without the coordinate patch, /click 400s. The solver must report failure
    quietly, never raise — a crawl on an old browser image must not crash here."""
    c = _FakeClient({"x": 500, "y": 300, "h": 60}, click_ok=False)
    assert cc._click_turnstile_checkbox(c, "http://x", "u", "tab") is False


def t_no_widget_found_is_a_safe_no_op() -> None:
    c = _FakeClient(None)
    assert cc._click_turnstile_checkbox(c, "http://x", "u", "tab") is False
    # ...and it must not have attempted a click with no target.
    assert not any(url.endswith("/click") for url, _ in c.calls)


def t_the_server_patch_is_present_in_both_vendor_copies() -> None:
    """The client sends coordinates, but they only work if the CamoFox server accepts them. Both
    build sources must carry the patch or a rebuilt image silently loses the capability."""
    # The monorepo carries both copies; the standalone production repo only the first. Check every
    # copy that IS present, and require at least one — a missing vendor tree must fail loudly rather
    # than let the loop pass vacuously.
    copies = [p for p in (_REPO / "vendor/camofox-browser/server.js",
                          _REPO / "Production crawler/vendor/camofox-browser/server.js")
              if p.exists()]
    assert copies, "no vendor/camofox-browser/server.js found — cannot verify the patch"
    for p in copies:
        rel = p.relative_to(_REPO)
        src = p.read_text(encoding="utf-8", errors="ignore")
        assert "coordinate click dispatched" in src, f"{rel} lost the coordinate-click patch"
        assert "ref, selector, or coordinates required" in src, f"{rel} still rejects coordinates"
        # ONE mouse.move, not a hand-rolled multi-hop path. Camoufox launches with humanize:true, so
        # the browser humanises every move itself; dispatching 18 of them (steps:6 then steps:12) cost
        # ~11s on example.com and blew the 30s handler timeout on a Cloudflare challenge — the click
        # 500'd exactly where it was needed. Guard against the multi-step path coming back.
        assert "steps: 12" not in src and "steps: 6" not in src, \
            f"{rel} is back to multi-step mouse moves — each one gets humanised, the click times out"


def t_a_passed_cloudflare_page_is_not_recorded_as_a_wall() -> None:
    """The success page still references challenges.cloudflare.com/turnstile (the widget script stays
    in the DOM after a pass) and keeps a filled cf-turnstile-response input. Keying is_captcha_wall on
    those made the crawler record "You bypassed the Cloudflare challenge!" as needs_captcha_solver —
    the exact page that proves success was filed as a failure. Measured live on
    scrapingcourse.com/cloudflare-challenge."""
    from crawler import errors
    passed = ('<html><head><script src="https://challenges.cloudflare.com/turnstile/v0/api.js">'
              '</script></head><body>You bypassed the Cloudflare challenge! :D'
              '<input name="cf-turnstile-response" value="0.xxx"></body></html>')
    assert errors.is_captcha_wall(passed) is False, "passed page still flagged as an uncleared wall"
    # ...but an ACTIVE interstitial (title + text + orchestrate path, all gone post-pass) must fire.
    wall = ('<html><head><title>Just a moment...</title></head><body>Verify you are human. '
            '/cdn-cgi/challenge-platform/h/g/orchestrate</body></html>')
    assert errors.is_captcha_wall(wall) is True, "active interstitial no longer detected"
    # the widget-script markers that persist after a pass must be gone from the marker list
    for persistent in ("challenges.cloudflare.com/turnstile", "cf-turnstile-response", "hcaptcha.com/"):
        assert persistent not in errors._CAPTCHA_WALL_MARKERS, f"{persistent!r} still a wall marker"


def t_a_cleared_cloudflare_site_is_not_a_wall_via_the_jsd_script() -> None:
    """Cloudflare injects /cdn-cgi/challenge-platform/scripts/jsd/main.js into EVERY page it serves,
    cleared ones included. Keying is_captcha_wall on the bare "/cdn-cgi/challenge-platform" prefix
    therefore threw away real content: measured on dzone.com, C3 passed the challenge and captured
    245KB of genuine article HTML, which was then filed as an uncleared wall and reported as
    needs_captcha_solver. Only the /h/<seed>/orchestrate/ path (gone after a pass) means interstitial."""
    from crawler import errors
    cleared = ('<html><head><title>DZone: Programming &amp; DevOps news</title>'
               "<script>var a=document.createElement('script');"
               "a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script></head>"
               '<body><h1>Over 2 million developers</h1><p>Real article content.</p></body></html>')
    assert errors.is_captcha_wall(cleared) is False, "a cleared Cloudflare page is not a wall"
    # ...but the live interstitial (orchestrate path + its title) still must fire.
    wall = ('<html><head><title>Just a moment...</title></head><body>'
            '<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1"></script>'
            '</body></html>')
    assert errors.is_captcha_wall(wall) is True, "active interstitial no longer detected"
    assert "/cdn-cgi/challenge-platform" not in errors._CAPTCHA_WALL_MARKERS, \
        "the bare prefix is back — it matches the always-present JSD script"


def t_the_challenge_loop_budgets_against_the_wall_clock() -> None:
    """/wait returns the instant the network idles — ~230ms on a challenge page, not settle_ms. The
    loop used to credit itself the full settle_ms per iteration, so it spun the entire 45s budget in
    ~1.3s and gave up while the challenge was still running (measured on dzone.com: 5 iterations in
    1.2s, then the interstitial was reported as unbypassed even though it self-clears at ~10s).
    The budget must be a real deadline and an early /wait must be slept out."""
    import inspect
    src = inspect.getsource(cc._settle)
    # strip comments — the fix's own comment quotes the old buggy line, which would false-positive
    code = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    assert "waited += settle_ms" not in code, "back to crediting a fixed cost per iteration"
    assert "time.monotonic()" in src and "deadline" in src, "loop must budget on the wall clock"
    assert "time.sleep(" in src, "an early /wait must be slept out, or the budget spins away"


def t_the_settle_loop_actually_calls_the_checkbox_click() -> None:
    """Wiring check: an interactive challenge only clears if _settle drives the click. If a refactor
    dropped the call, Cloudflare would silently regress to unsolved."""
    import inspect
    src = inspect.getsource(cc._settle)
    assert "_click_turnstile_checkbox" in src
    assert "CRAWLER_CAMOFOX_CF_CLICK" in src, "the disable-gate is gone"


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} CF-CLICK TESTS PASSED")


def test_cf_click_suite() -> None:
    main()


if __name__ == "__main__":
    main()

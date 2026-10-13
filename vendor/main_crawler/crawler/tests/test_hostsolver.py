"""C4 host solver — the tier that clears a Cloudflare INTERACTIVE wall from the real GPU on the host.

The solver itself needs a desktop browser, so it is not exercised here; what IS pinned is the
contract the crawler depends on:

  * it is OFF unless CRAWLER_HOST_SOLVER_URL is set, so a deployment without the host service behaves
    exactly as before (wall recorded, crawl continues) instead of erroring;
  * every failure mode — unreachable, HTTP error, a 200 that is still the interstitial — returns
    None, never raises, because this is the last rung and must degrade to "record the wall and move
    on";
  * a recovered page is handed to the SAME store + link-enqueue path as C3, so the crawl continues
    into the now-cleared site rather than rescuing one URL.

Runnable directly:  python tests/test_hostsolver.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import hostsolver  # noqa: E402


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload

    def json(self):
        return self._p


class _Client:
    """Stand-in httpx.Client: returns a queued response or raises a queued exception."""
    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None):
        if self._exc:
            raise self._exc
        return self._resp

    def get(self, url):
        if self._exc:
            raise self._exc
        return self._resp


def _with(monkey_url, client):
    """Run hostsolver.solve() with a patched env + httpx client."""
    import httpx
    prev = os.environ.get("CRAWLER_HOST_SOLVER_URL")
    orig = httpx.Client
    if monkey_url is None:
        os.environ.pop("CRAWLER_HOST_SOLVER_URL", None)
    else:
        os.environ["CRAWLER_HOST_SOLVER_URL"] = monkey_url
    httpx.Client = lambda *a, **k: client
    try:
        return hostsolver.solve("https://www.zoopla.co.uk/to-rent")
    finally:
        httpx.Client = orig
        if prev is None:
            os.environ.pop("CRAWLER_HOST_SOLVER_URL", None)
        else:
            os.environ["CRAWLER_HOST_SOLVER_URL"] = prev


def t_disabled_when_no_url_is_set() -> None:
    assert _with(None, _Client()) is None
    prev = os.environ.pop("CRAWLER_HOST_SOLVER_URL", None)
    try:
        assert hostsolver.enabled() is False
    finally:
        if prev is not None:
            os.environ["CRAWLER_HOST_SOLVER_URL"] = prev


def t_unreachable_service_returns_none_not_an_exception() -> None:
    """The host may simply not be running. That must record the wall and move on, never crash the
    crawl."""
    import httpx
    assert _with("http://localhost:1", _Client(exc=httpx.ConnectError("refused"))) is None


def t_http_error_returns_none() -> None:
    assert _with("http://x", _Client(resp=_Resp(500, {}))) is None


def t_a_200_that_is_still_the_interstitial_is_not_success() -> None:
    """The failure mode is a 200 whose body is still 'Just a moment'. The SERVICE judges ok on
    content; the client must honour ok=False and not store the wall as a page."""
    assert _with("http://x", _Client(resp=_Resp(200, {"ok": False, "html": "Just a moment...",
                                                      "chars": 15, "solved": False}))) is None
    # ok=True but empty html is also not usable.
    assert _with("http://x", _Client(resp=_Resp(200, {"ok": True, "html": ""}))) is None


def t_a_real_clear_is_returned() -> None:
    good = {"ok": True, "html": "<html>" + "x" * 2000 + "</html>", "chars": 2013,
            "via": "cold", "solved": True, "elapsed": 10.5}
    r = _with("http://x", _Client(resp=_Resp(200, good)))
    assert r is not None and r["ok"] and len(r["html"]) > 1000
    assert r["via"] == "cold" and r["solved"] is True


def t_engine_c4_is_gated_and_recovers_into_the_normal_path() -> None:
    """The engine attempt must (a) only fire for cloudflare_interactive with the solver enabled,
    and (b) on success run the recovered HTML through build_document -> gate -> finalize, so the
    crawl continues rather than just logging a rescue. Driven with stubs — no browser, no network."""
    import asyncio

    from crawler import async_engine as ae
    from crawler.models import Job

    inter = ("<html><head><title>Just a moment...</title></head><body><script>"
             "window._cf_chl_opt={cvId:'3',cType:'interactive',cRay:'a20'};</script></body></html>")

    class _Ctx:
        def __init__(self):
            # Real Job so gate.evaluate sees every field it reads; keyword off so the article-shaped
            # body isn't gate-dropped for lacking a corpus keyword.
            self.job = Job(job_id="j", job_type="news", seed_urls=["https://www.zoopla.co.uk/"],
                           keyword=False, max_pages=50, max_depth=2, capture=["html", "text"])
            self.seed = None
            self.fetcher = None
            self.kp = None
            self.crawl_run_id = "run1"
            self.reasons: list = []
            self.recovered: list = []
            self.seen: set = set()
            self.seen_hashes: set = set()
            self.dropped_by_gate = 0
            self.kept = 0
        def bump_reason(self, r): self.reasons.append(r)
        def bump_recovered(self, r): self.recovered.append(r)

    async def run():
        e = ae.AsyncEngine.__new__(ae.AsyncEngine)
        enqueued = []
        finalized = {}
        e._enqueue_links = lambda ctx, fr, url, depth: enqueued.append((url, fr.status))

        async def fake_finalize(ctx, doc, hp, fr, url):
            finalized["url"] = url
            finalized["captcha_solver"] = doc.captcha_solver
        e._finalize = fake_finalize

        item = ae.WorkItem(url="https://www.zoopla.co.uk/to-rent", depth=0, ctx=_Ctx())
        item.last_body = inter

        # Solver returns a real cleared page.
        import crawler.hostsolver as hs
        orig_solve, orig_enabled = hs.solve, hs.enabled
        hs.solve = lambda url: {"ok": True, "html": "<html><body>" + "listing " * 400
                                + "</body></html>", "via": "cold", "solved": True, "chars": 3200}
        hs.enabled = lambda: True
        try:
            ok = await e._solve_on_host(item, "remedy")
        finally:
            hs.solve, hs.enabled = orig_solve, orig_enabled

        assert ok is True, "a cleared page must report handled"
        assert enqueued and enqueued[0][0] == item.url, "child links must be enqueued (crawl continues)"
        assert finalized.get("url") == item.url, "the recovered page must reach _finalize (gets stored)"
        assert finalized.get("captcha_solver") == "host_cdp_solve_captcha", \
            "provenance must record it was solved on the host"
        assert "served_by_host_solver" in item.ctx.recovered
    asyncio.run(run())


def t_c4_runs_even_when_c3_captcha_flagged_the_wall() -> None:
    """The bug that stopped a solved site after page 1: C3 CamoFox flags an interactive challenge as
    a captcha limitation (captcha_flagged=True), and the ladder's `if item.captcha_flagged: return`
    used to fire BEFORE the C4 host-solve — so a zoopla seed reached C4 and was solved, but all 50
    of its children were captcha-flagged by C3 and dead-ended, and only page 1 was kept.

    The C4 attempt must come first for an interactive wall. Verified at the source, since driving
    the full C3 ladder needs a browser."""
    import inspect
    import re

    from crawler import async_engine as ae

    src = inspect.getsource(ae.AsyncEngine._run_c3_ladder)
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    solve_at = code.find("_solve_on_host")
    flag_return = re.search(r"if item\.captcha_flagged:\s*\n\s*return", code)
    assert solve_at != -1, "C4 host-solve is not in the c3 ladder"
    assert flag_return, "the captcha_flagged early-return is gone (unexpected)"
    assert solve_at < flag_return.start(), \
        "C4 host-solve must run BEFORE the captcha_flagged return, or flagged children never reach it"


def t_proactive_c4_routes_a_known_host_straight_to_the_solver() -> None:
    """Once C4 clears a host, every other page on it is walled the same way. Routing them straight
    to the warm solver (~5s) instead of repeating the httpx->render->C3(57s)->C4 ladder per page is
    what lets a solved site actually crawl to max_pages. Pinned at the source: the proactive block
    must key on host_needs_c4 and call the solver before the normal ladder."""
    import inspect

    from crawler import async_engine as ae

    src = inspect.getsource(ae.AsyncEngine._process)
    assert "host_needs_c4" in src, "no proactive-C4 routing in _process"
    assert src.find("host_needs_c4") < src.find("Proactive C3"), \
        "proactive C4 must be checked before the C3 ladder, or the doomed C3 step still runs"
    # And the set is populated on a successful solve.
    assert "host_needs_c4.add" in inspect.getsource(ae.AsyncEngine._solve_on_host)


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} HOST-SOLVER TESTS PASSED")


def test_hostsolver_suite() -> None:
    main()


if __name__ == "__main__":
    main()

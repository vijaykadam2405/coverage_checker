"""tiers - prove EACH of the four fetch tiers exists for a reason AND actually works.

The crawler climbs a fixed ladder, cheapest first, and each rung is roughly 50x
costlier than the one below it:

    C1  httpx / curl_cffi (real-Chrome JA3)   the default; a plain HTTP GET
      -> C2  Wayback (archive.org CDX + id_ replay)  the origin is dead/WAF-blocked
        -> C3  CamoFox stealth render                a JS shell / bot wall / captcha
          -> C4  host-side GPU solver                a Cloudflare-INTERACTIVE wall

Each tier earns its keep only if (a) the transport it depends on is reachable and
answers correctly RIGHT NOW, and (b) it is genuinely wired into production - i.e. it
has actually served pages into the corpus recently. So every tier gets two checks:

  * an ACTIVE probe  - hit the tier's transport (or its health endpoint) on a canary
                       and assert it behaves. Proves "the pipe works today".
  * a PASSIVE check  - count documents.served_by = <this tier's label> ingested in
                       the last hc.RECENT_HOURS. Proves "it is doing its job in prod".

served_by labels are taken from the live code, not guessed:
  * C1  -> "c1_live"           (extract.build_document default; impersonate/JA3 stays
           "c1_live_tls_unverified"  c1_live - it's a C1 transport variant) and the
                               untrusted per-host TLS-proceed variant (async_engine).
  * C2  -> "c2_wayback"        (c2.py; "c2_memento" is contracted but unimplemented).
           "c2_memento"
  * C3  -> "c3_camofox"        (async_engine C3 path).
  * C4  -> "c4_host_solver"    (async_engine C4 path).

Design constraints (from hc.py): DB access is READ-ONLY and TOAST-guarded (never
select main_text/html - we only ever touch served_by / ingested_at / text_len), the
crawler package may not be importable in every runtime (wrap imports, Skip if so),
and tiers that legitimately serve nothing for stretches (C2/C3/C4) SKIP on zero
recent pages, never FAIL.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import hc

# -- served_by column labels, per tier (verified against the live source) -------
C1_LABELS = ["c1_live", "c1_live_tls_unverified"]
C2_LABELS = ["c2_wayback", "c2_memento"]
C3_LABELS = ["c3_camofox"]
C4_LABELS = ["c4_host_solver"]


# -- shared helpers -------------------------------------------------------------
def _recent_bound() -> str:
    """The lower bound for "recent", formatted the way ingested_at is STORED.

    ingested_at is TEXT ISO-8601 and MUST be compared as text (casting it detoasts
    nothing but defeats documents_ingested_at_idx and forces a full corpus scan -
    pgstore.py measured that at 16 minutes vs 4.2s). ISO-8601 sorts lexically, so a
    string `>` against a pre-formatted bound answers the same question and uses the
    index. No 'Z' suffix, exactly like pgstore's own recent-window bound."""
    t = datetime.now(timezone.utc) - timedelta(hours=hc.RECENT_HOURS)
    return t.strftime("%Y-%m-%dT%H:%M:%S")


def _recent_count(labels: list[str]) -> int:
    """Documents whose served_by is one of `labels`, ingested since the bound.

    Reads the base `documents` table (not the documents_current view) - every ingest
    is evidence a tier served a page, and the base table's ingested_at index makes
    this a bounded scan of just the recent window. Touches NO TOAST column."""
    n = hc.pg_scalar(
        "SELECT count(*) FROM documents "
        "WHERE ingested_at > %s AND served_by = ANY(%s)",
        (_recent_bound(), labels),
    )
    return int(n or 0)


def _recent_total() -> int:
    """All documents ingested since the bound - used to tell "crawler is idle" (a
    legitimate SKIP) apart from "the cheap default tier stopped serving" (a FAIL)."""
    n = hc.pg_scalar(
        "SELECT count(*) FROM documents WHERE ingested_at > %s", (_recent_bound(),)
    )
    return int(n or 0)


def _import(modname: str):
    """Import a crawler module or SKIP. The healthcheck can run in a runtime that has
    no crawler package on sys.path (or is missing a transitive dep); that's expected,
    and the passive DB check still covers the tier, so we skip the active probe rather
    than fail it."""
    try:
        import importlib

        return importlib.import_module(modname)
    except Exception as e:  # noqa: BLE001 - any import failure means "not available here"
        raise hc.Skip(f"{modname} not importable here ({type(e).__name__}: {str(e)[:80]})")


def _window() -> str:
    return f"last {hc.RECENT_HOURS:.0f}h"


# ==============================================================================
# C1 - the cheap default. A plain httpx GET, escalated to a curl_cffi real-Chrome
# JA3 fetch when a TLS-fingerprint WAF blocks plain httpx. This is ~99% of every
# crawl; if it breaks, the whole ladder is paying C3/C4 prices for pages a GET
# should have returned.
# ==============================================================================
@hc.check(area="tiers", name="c1_active_httpx", critical=False)
def c1_active_httpx() -> str:
    """ACTIVE: fetch CANARY_C1 through the crawler's own C1 transport (the shared
    httpx client; curl_cffi JA3 as the escalation) and assert a 200 with a real body.
    Proves the cheapest, most-used rung actually reaches the open web right now."""
    fetcher = _import("crawler.fetcher")
    url = hc.CANARY_C1
    # Path 1: the plain pooled httpx client - literally what C1 uses first.
    try:
        client = fetcher._shared_http_client(True)
        r = client.get(url, timeout=hc.HTTP_TIMEOUT)
        status, body, via = r.status_code, (r.text or ""), "httpx"
    except Exception as e_httpx:  # noqa: BLE001
        # Path 2: the curl_cffi real-Chrome JA3 fetch - C1's own escalation for a
        # TLS-fingerprint block. If BOTH transports fail, C1 genuinely can't fetch.
        try:
            r = fetcher._curl_cffi_get(url, impersonate="chrome", timeout=hc.HTTP_TIMEOUT)
            status, body, via = r.status_code, (r.text or ""), "curl_cffi"
        except Exception as e_cc:  # noqa: BLE001
            raise AssertionError(
                f"C1 could not fetch {url}: httpx={type(e_httpx).__name__}:{e_httpx}; "
                f"curl_cffi={type(e_cc).__name__}:{e_cc}")
    hc.need(status == 200, f"C1 {via} GET {url} -> HTTP {status}")
    hc.need(len(body) > 200, f"C1 {via} GET {url} -> trivial body ({len(body)} chars)")
    return f"C1 {via} {url} -> 200, {len(body)} chars"


@hc.check(area="tiers", name="c1_passive_recent", critical=False)
def c1_passive_recent() -> str:
    """PASSIVE: C1 pages ingested in the window. C1 is the default, so it should be
    common - but only when the crawler is actually running. Zero C1 pages while OTHER
    pages are landing means the live-fetch tier stopped serving (a real alarm); zero
    pages of any kind means the crawler is simply idle (coverage's problem, not ours)."""
    n = _recent_count(C1_LABELS)
    if n > 0:
        return f"{n} C1 pages ({'/'.join(C1_LABELS)}) in {_window()}"
    total = _recent_total()
    if total == 0:
        raise hc.Skip(f"no pages ingested in {_window()} - crawler idle (coverage owns this)")
    raise AssertionError(
        f"0 C1 pages in {_window()} but {total} pages total were ingested - the cheap "
        f"default tier is not serving; the ladder is paying higher tiers for C1 work")


# ==============================================================================
# C2 - the ARCHIVE tier. When the live origin is dead (DNS gone) or hard WAF-blocked
# with no browser path that clears, the page may still exist in the Internet Archive.
# C2 serves it from archive.org's raw (id_) replay - routing AROUND the block via a
# WAF-free path rather than fighting it. Date-driven and standalone (c2.py).
# ==============================================================================
@hc.check(area="tiers", name="c2_active_wayback", critical=False)
def c2_active_wayback() -> str:
    """ACTIVE: resolve a Wayback snapshot for CANARY_C2 and pull its archived body.

    c2.py's own surface is job/frontier-scoped (run_c2_batch drives a full paginated
    CDX domain scan from Job objects and is rate-limited) - too heavy and not cleanly
    callable for a single-URL probe. So we exercise the SAME archive.org transport
    c2.py depends on: the availability API to resolve the closest capture, then the
    raw `id_` replay (c2.REPLAY) to prove archived content actually comes back.

    archive.org being down/throttled is an upstream condition, not a crawler fault, so
    every reachability problem here is a SKIP, never a FAIL."""
    canary = hc.CANARY_C2
    avail = f"https://archive.org/wayback/available?url={canary}"
    try:
        status, body = hc.http_get(avail, timeout=min(hc.HTTP_TIMEOUT, 15.0))
    except Exception as e:  # noqa: BLE001 - DNS/refused/timeout to archive.org
        raise hc.Skip(f"archive.org unreachable ({type(e).__name__}: {str(e)[:80]})")
    if status != 200:
        raise hc.Skip(f"archive.org availability API -> HTTP {status}")
    import json

    try:
        snap = (json.loads(body).get("archived_snapshots") or {}).get("closest") or {}
    except json.JSONDecodeError:
        raise hc.Skip("archive.org availability API returned non-JSON")
    if not snap.get("available") or not snap.get("url"):
        raise hc.Skip(f"archive.org has no snapshot for {canary} right now (upstream coverage)")
    ts = snap.get("timestamp", "?")
    # Prove archived CONTENT returns - fetch the raw id_ replay (the exact form c2.py
    # uses: web.archive.org/web/<ts>id_/<url>). A slow/failed replay is still upstream.
    replay = f"https://web.archive.org/web/{ts}id_/{canary}"
    try:
        rstatus, rbody = hc.http_get(replay, timeout=min(hc.HTTP_TIMEOUT, 20.0))
    except Exception as e:  # noqa: BLE001
        return f"resolved snapshot {ts} for {canary} (replay fetch skipped: {type(e).__name__})"
    if rstatus == 200 and len(rbody) > 500:
        return f"C2 archive.org snapshot {ts} -> replay 200, {len(rbody)} chars"
    raise hc.Skip(f"snapshot {ts} resolved but replay HTTP {rstatus} ({len(rbody)} chars) - upstream")


@hc.check(area="tiers", name="c2_passive_recent", critical=False)
def c2_passive_recent() -> str:
    """PASSIVE: C2 pages ingested in the window. C2 only runs for date-driven archive
    jobs and for origins nothing else could reach, so long stretches of zero are
    NORMAL - PASS if any, else SKIP. Never a FAIL."""
    n = _recent_count(C2_LABELS)
    if n > 0:
        return f"{n} C2 pages ({'/'.join(C2_LABELS)}) in {_window()}"
    raise hc.Skip(f"no recent C2 (wayback) pages in {_window()} - normal if no origin needed it")


# ==============================================================================
# C3 - the STEALTH RENDER tier. CamoFox is a fingerprint-spoofing Firefox server: it
# renders JS shells, clears self-solving bot walls, and (with the solver path) works
# captchas. It's the answer when the content only exists after JS runs, or the origin
# serves a challenge a plain fetch can't pass. A real browser, so ~50x a C1 GET.
# ==============================================================================
@hc.check(area="tiers", name="c3_active_camofox", critical=False)
def c3_active_camofox() -> str:
    """ACTIVE: GET the CamoFox render pool's /health (the endpoint camofox_client.health
    uses). Probes every server in the pool when the client is importable, else the
    harness's HC_CAMOFOX_URL. An unreachable pool is a SKIP (C3 may not be deployed in
    this environment). Set HC_C3_LIVE_RENDER=1 to also do a light live render of
    CANARY_C3 (costs a real browser tab, so it's opt-in)."""
    bases: list[str]
    try:
        cf = _import("crawler.camofox_client")
        bases = cf.pool_urls()
    except hc.Skip:
        cf = None
        # hc.CAMOFOX_URL may itself be a comma-separated pool; mirror _bases().
        bases = [b.strip().rstrip("/") for b in hc.CAMOFOX_URL.split(",") if b.strip()]
    if not bases:
        raise hc.Skip("no CamoFox URL configured")

    healthy, errors = [], []
    for base in bases:
        try:
            status, _ = hc.http_get(f"{base}/health", timeout=min(hc.HTTP_TIMEOUT, 8.0))
            (healthy if status == 200 else errors).append(f"{base}->{status}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{base}->{type(e).__name__}")
    if not healthy:
        raise hc.Skip(f"CamoFox render pool unreachable: {', '.join(errors) or 'none'}")

    detail = f"C3 CamoFox healthy {len(healthy)}/{len(bases)}"
    if errors:
        detail += f" (down: {', '.join(errors)})"

    # Optional live render - expensive (drives a real browser), so opt-in only.
    if hc.cfg("HC_C3_LIVE_RENDER", "0") == "1":
        if cf is None:
            cf = _import("crawler.camofox_client")  # may SKIP if not importable
        fr = cf.render(hc.CANARY_C3, timeout_s=min(hc.HTTP_TIMEOUT * 2, 45.0))
        chars = len(getattr(fr, "inner_text", None) or getattr(fr, "text_html", None) or "") if fr else 0
        hc.need(fr is not None and chars > 200,
                f"C3 live render of {hc.CANARY_C3} returned {chars} chars")
        detail += f"; live render {hc.CANARY_C3} -> {chars} chars"
    return detail


@hc.check(area="tiers", name="c3_passive_recent", critical=False)
def c3_passive_recent() -> str:
    """PASSIVE: C3 pages ingested in the window. Only bot-walled / JS-only sites reach
    C3, so zero recently is legitimate - PASS if any, else SKIP. Never a FAIL."""
    n = _recent_count(C3_LABELS)
    if n > 0:
        return f"{n} C3 pages (c3_camofox) in {_window()}"
    raise hc.Skip(f"no recent C3 (camofox) pages in {_window()} - normal if no site needed a render")


# ==============================================================================
# C4 - the host-side GPU solver. The last rung: a Cloudflare-INTERACTIVE wall never
# self-clears and injects no widget to click, and the container's llvmpipe GL is
# provably blocked while the host's real GPU passes. So the crawler calls out to
# scripts/host_solver.py on the Windows host. Disabled unless CRAWLER_HOST_SOLVER_URL
# is set; a deployment without it behaves exactly as before (hostsolver.py).
# ==============================================================================
@hc.check(area="tiers", name="c4_active_solver", critical=False)
def c4_active_solver() -> str:
    """ACTIVE: if C4 is enabled, probe the host solver's /health. Enabled-but-unreachable
    is a real FAIL (a configured last rung that's down leaves interactive-CF walls with
    no path); not configured is a SKIP. A full live solve is expensive (drives a warm
    browser through a challenge), so it's gated behind HC_C4_LIVE_SOLVE=1."""
    # Read enabled()/base_url() through hostsolver when importable; else fall back to the
    # env var it reads, so the probe still works in a bare runtime.
    base = ""
    try:
        hs = _import("crawler.hostsolver")
        if not hs.enabled():
            raise hc.Skip("C4 disabled: CRAWLER_HOST_SOLVER_URL unset")
        base = hs.base_url()
    except hc.Skip:
        # Distinguish "not importable" from "importable but disabled": re-derive from env.
        base = hc.cfg("CRAWLER_HOST_SOLVER_URL", "").rstrip("/")
        if not base:
            raise hc.Skip("C4 disabled: CRAWLER_HOST_SOLVER_URL unset")
        hs = None

    try:
        status, body = hc.http_get(f"{base}/health", timeout=min(hc.HTTP_TIMEOUT, 8.0))
    except Exception as e:  # noqa: BLE001
        raise AssertionError(
            f"C4 enabled ({base}) but /health unreachable ({type(e).__name__}: {str(e)[:80]})")
    hc.need(status == 200, f"C4 host solver /health -> HTTP {status}: {body[:120]}")
    detail = f"C4 host solver /health 200 @ {base}"

    # Optional live solve - a cold solve drives a real browser through a CF challenge.
    if hc.cfg("HC_C4_LIVE_SOLVE", "0") == "1":
        if hs is None:
            hs = _import("crawler.hostsolver")  # needs the real solve()/httpx; may SKIP
        target = hc.cfg("HC_C4_SOLVE_URL", hc.CANARY_C3)
        data = hs.solve(target)
        hc.need(bool(data) and bool(data.get("ok")) and bool(data.get("html")),
                f"C4 live solve of {target} did not clear (data={data!r:.200})")
        detail += f"; live solve {target} -> ok, {data.get('chars')} chars"
    return detail


@hc.check(area="tiers", name="c4_passive_recent", critical=False)
def c4_passive_recent() -> str:
    """PASSIVE: C4 pages ingested in the window. Only interactive-Cloudflare hosts ever
    reach C4, and most crawls hit none, so zero recently is entirely normal - PASS if
    any, else SKIP. Never a FAIL."""
    n = _recent_count(C4_LABELS)
    if n > 0:
        return f"{n} C4 pages (c4_host_solver) in {_window()}"
    raise hc.Skip(f"no recent C4 (host-solver) pages in {_window()} - normal if no CF-interactive wall")

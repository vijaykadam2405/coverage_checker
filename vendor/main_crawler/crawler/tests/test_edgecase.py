"""Edge-case detector: name the obstacle on a page and say what to do about it.

The interesting half of these tests is the NEGATIVE half. Every detector in `crawler.edgecase`
keys on wording that occurs constantly in ordinary prose — "sign in", "cookies", "subscribe",
"page not found", a year in a URL — so the failure mode that matters is not "missed a wall", it
is "threw away a real article". A miss costs one page; a false positive costs every page that
mentions the word. Each detector below is therefore tested in both directions.

Runnable directly:  python tests/test_edgecase.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import edgecase as ec, errors  # noqa: E402

H = {"content-type": "text/html; charset=utf-8"}
NOW_Y = datetime.now(timezone.utc).year
ARTICLE = "<html><body><article>" + "Defence procurement analysis paragraph. " * 200 + "</article></body></html>"


def t_transport_errors_map_to_their_case() -> None:
    assert ec.diagnose("http://x/", error="[Errno 11001] getaddrinfo failed").case == ec.CASE_DNS
    assert ec.diagnose("http://x/", error="CERTIFICATE_VERIFY_FAILED certificate has expired").case == ec.CASE_SSL
    assert ec.diagnose("http://x/", error="ReadTimeout: ").case == ec.CASE_TIMEOUT
    assert ec.diagnose("http://x/", error="ConnectionRefusedError").case == ec.CASE_CONN


def t_server_disconnect_is_a_connection_error_not_other() -> None:
    """Regression: httpx/h11 phrase the mid-request hangup as "Server disconnected without
    sending a response", which matched none of the net::ERR_* needles and fell through to OTHER.
    That mattered beyond tidiness — OTHER is not in errors.WEAK_UNDER_LOAD, so the auto-careful
    throttle built exactly for "host refuses us under concurrency" never saw its own signal.
    Measured live against httpstat.us."""
    v = ec.diagnose("http://x/", error="RemoteProtocolError: Server disconnected without sending a response.")
    assert v.case == ec.CASE_CONN, f"got {v.case}"
    from crawler import errors
    assert errors.classify_failure(None, "Server disconnected without sending a response.") in errors.WEAK_UNDER_LOAD


def t_ssl_is_skipped_never_bypassed() -> None:
    """A broken chain is a real trust failure. The remedy must never be "retry without verify" —
    that would silently accept a MITM for the rest of the crawl."""
    v = ec.diagnose("http://x/", error="SSL: CERTIFICATE_VERIFY_FAILED self-signed certificate")
    assert (v.case, v.action) == (ec.CASE_SSL, ec.ACT_SKIP)


def t_rate_limit_reads_the_headers_and_returns_the_delay() -> None:
    v = ec.diagnose("http://x/", status=429, headers={"Retry-After": "60"})
    assert v.case == ec.CASE_RATE_LIMIT and v.action == ec.ACT_BACKOFF
    assert v.detail["retry_after_s"] == 60.0, "the backoff must carry the server's own number"
    assert "60" in v.evidence
    # A 503 is only a rate limit when the server says when to come back; otherwise it is a plain
    # server error and must not be mistaken for throttling.
    assert ec.diagnose("http://x/", status=503, headers={"Retry-After": "30"}).case == ec.CASE_RATE_LIMIT
    assert ec.diagnose("http://x/", status=503).case == ec.CASE_HTTP_ERROR
    assert ec.diagnose("http://x/", status=200, headers={"X-RateLimit-Remaining": "0"},
                       body=ARTICLE).case == ec.CASE_RATE_LIMIT


def t_geo_block_from_status_and_from_prose() -> None:
    assert ec.diagnose("http://x/", status=451).case == ec.CASE_GEO_BLOCK
    thin = "<html><body><h1>Sorry</h1><p>This video is not available in your country.</p></body></html>"
    assert ec.diagnose("http://x/", status=200, body=thin).case == ec.CASE_GEO_BLOCK
    # …but an article ABOUT geo-blocking is a document, not a block.
    essay = ("<html><body><article>" + "The phrase not available in your country appears in "
             "streaming licensing disputes. " * 120 + "</article></body></html>")
    assert ec.diagnose("http://x/", status=200, body=essay).case == ec.CASE_OK

    # A geo block wearing an ERROR STATUS. Most operators never send the 451 the standard provides:
    # the benchmark measured bwin/betfred/stoiximan answering 403 and tab.com.au answering 500, all
    # with the reason spelled out in the body. Read as a generic server error, a 500 is
    # retry_later — so the crawler would retry a permanent, legally-mandated refusal forever.
    H = {"content-type": "text/html"}
    blocked = ("<html><head><title>Blocked</title></head><body><p>Sorry, this service is not "
               "available in your country due to legal reasons.</p></body></html>")
    for st in (403, 500, 503, 452):
        v = ec.diagnose("https://www.tab.com.au/", status=st, headers=H, body=blocked)
        assert v.case == ec.CASE_GEO_BLOCK, f"HTTP {st} with a geo message read as {v.case}"
        assert v.action == ec.ACT_SKIP, f"HTTP {st} geo block still retries ({v.action})"

    # The specific classifications above this rule must keep winning — it sits in front of the
    # GENERIC 4xx/5xx branch only.
    assert ec.diagnose("https://x/", status=429, headers={**H, "retry-after": "60"},
                       body=blocked).case == ec.CASE_RATE_LIMIT, "a throttle became a geo block"
    assert ec.diagnose("https://x/", status=404, headers=H, body=blocked).case == ec.CASE_GONE
    # And an error page with no such wording stays an ordinary error.
    for st in (403, 500):
        plain = "<html><body><p>Internal Server Error</p></body></html>"
        assert ec.diagnose("https://x/", status=st, headers=H, body=plain).case == ec.CASE_HTTP_ERROR


def t_soft_404_is_caught_but_an_article_about_404s_is_not() -> None:
    """HTTP 200 for a page that does not exist. Nothing upstream flags it, so the body lands in
    the corpus as a real document and dedup fills with near-identical junk."""
    v = ec.diagnose("http://x/nope", status=200,
                    body="<html><head><title>Page Not Found</title></head><body>Try the menu.</body></html>")
    assert (v.case, v.action) == (ec.CASE_SOFT_404, ec.ACT_MARK_GONE)
    # A long page whose title is normal must survive even if the phrase appears in the body.
    blog = ("<html><head><title>How we fixed our soft 404s</title></head><body><article>"
            + "Returning page not found with a 200 status confuses crawlers. " * 150
            + "</article></body></html>")
    assert ec.diagnose("http://x/", status=200, body=blog).case == ec.CASE_OK
    # Sites name the missing ENTITY, not "page" (meetup.com: "Meetup | Group not found").
    assert ec.diagnose("http://x/g/nope", status=200,
                       body="<html><head><title>Meetup | Group not found</title></head><body>"
                            "Sorry, the group you're looking for doesn't exist."
                            "</body></html>").case == ec.CASE_SOFT_404
    # …but that wording is ordinary English, so on a REAL article it must not fire. This is the
    # exact false positive the weak/strong split exists to prevent, and it matters here: a defence
    # corpus is full of headlines like this one.
    news = ("<html><head><title>Weapons not found in Iraq, inspectors say</title></head><body>"
            "<article>" + "The inspection team reported that the declared stockpile does not "
            "exist at the site. " * 160 + "</article></body></html>")
    assert ec.diagnose("http://x/", status=200, body=news).case == ec.CASE_OK, \
        "a news headline containing 'not found' must never be discarded as a soft 404"


def t_auth_wall_needs_the_page_to_be_thin() -> None:
    login = ('<html><body><form action="/session"><input type="password" name="p">'
             '<button>Sign in</button></form></body></html>')
    assert ec.diagnose("http://x/", status=200, body=login).case == ec.CASE_AUTH_WALL
    assert ec.diagnose("http://x/", status=401).case == ec.CASE_AUTH_WALL
    # Landing on a login URL after a redirect is the clearest signal of all.
    assert ec.diagnose("http://x/secret", status=200, body="<html><body>hi</body></html>",
                       final_url="https://x/login?next=/secret").case == ec.CASE_AUTH_WALL
    # A normal article that happens to carry a login box in its header must NOT be dropped —
    # this is the single most damaging false positive available to this detector.
    with_header = ARTICLE.replace("<article>", '<form><input type="password"></form><article>')
    assert ec.diagnose("http://x/", status=200, body=with_header).case == ec.CASE_OK


def t_paywall_hard_vs_soft_decides_whether_the_text_is_salvageable() -> None:
    """A metered paywall ships the whole article and hides it with CSS — that text is readable and
    must be kept. A hard paywall ships a teaser and there is nothing to keep. Same JSON-LD flag,
    opposite action, so the split has to be made on measured length."""
    ld = '<script type="application/ld+json">{"isAccessibleForFree":false}</script>'
    hard = ec.diagnose("http://x/", status=200, body="<html>" + ld + "<p>Subscribe.</p></html>")
    assert (hard.case, hard.action) == (ec.CASE_PAYWALL, ec.ACT_SKIP)
    assert hard.detail["variant"] == "hard"
    soft = ec.diagnose("http://x/", status=200, body="<html>" + ld + ARTICLE + "</html>")
    assert (soft.case, soft.action) == (ec.CASE_PAYWALL, ec.ACT_STORE), "soft paywall text is keepable"
    assert soft.detail["variant"] == "soft"


def t_consent_wall_only_when_it_actually_withholds_content() -> None:
    gate = '<html><body><script src="https://cdn.cookielaw.org/otSDK.js"></script><p>We use cookies.</p></body></html>'
    v = ec.diagnose("http://x/", status=200, body=gate)
    assert v.case == ec.CASE_CONSENT_WALL and v.detail["vendor"] == "onetrust"
    # A OneTrust banner sitting on top of a fully-served article is not a wall. Almost every
    # European news site loads a CMP; treating that as blocking would drop half the web.
    banner = ARTICLE.replace("<article>", '<script src="https://cdn.cookielaw.org/otSDK.js"></script><article>')
    assert ec.diagnose("http://x/", status=200, body=banner).case == ec.CASE_OK


def t_calendar_trap_catches_the_path_form_the_frontier_cap_misses() -> None:
    """The frontier's query-variant cap keys on host+path, so `/events/?date=YYYY-MM` is capped
    after 20 hops. `/events/YYYY-MM/` gets a fresh key every hop, so the cap never fires — and
    looks_like_trap sees two short segments and nothing repeated. That is the hole."""
    from crawler import errors
    url = f"https://x.org/events/{NOW_Y + 9}-04/"
    assert errors.looks_like_trap(url) is None, "precondition: the old heuristic does not fire"
    v = ec.diagnose(url)
    assert (v.case, v.action) == (ec.CASE_CALENDAR_TRAP, ec.ACT_DROP_TRAP)
    assert str(NOW_Y + 9) in v.evidence
    # A bare future year needs a calendar hint, or a product page is destroyed.
    assert ec.diagnose(f"https://x.org/cars/{NOW_Y + 9}-concept-model").case != ec.CASE_CALENDAR_TRAP
    assert ec.diagnose(f"https://x.org/calendar/?year={NOW_Y + 9}").case == ec.CASE_CALENDAR_TRAP
    # This year and next are ordinary content, not a trap.
    assert ec.diagnose(f"https://x.org/events/{NOW_Y}-04/").case != ec.CASE_CALENDAR_TRAP


def t_ssrf_guard_blocks_internal_addresses() -> None:
    """A crawler follows links written by the pages it reads, which makes it an ideal confused
    deputy. parse.extract_links already drops file://, gopher://, javascript: and data:, but an
    ordinary http:// URL aimed at a private address sailed straight through — including the cloud
    metadata endpoint, which answers with live IAM credentials that we would have stored as a
    document."""
    from crawler import parse
    assert ec.private_target("http://169.254.169.254/latest/meta-data/iam/security-credentials/")
    assert ec.private_target("http://127.0.0.1:8099/v1/metrics")
    assert ec.private_target("http://localhost/admin")
    assert ec.private_target("http://10.0.0.5/internal")
    assert ec.private_target("http://192.168.1.1/")
    assert ec.private_target("http://172.17.0.1:5432/")        # the Docker bridge gateway
    assert ec.private_target("http://metadata.google.internal/computeMetadata/v1/")
    # Public hosts must still be crawlable, or the guard has eaten the crawl.
    assert ec.private_target("https://www.sipri.org/research") is None
    assert ec.private_target("https://example.com/") is None
    # And the pre-existing scheme filter must keep doing its half of the job.
    links = parse.extract_links(
        '<a href="file:///C:/Windows/win.ini">a</a><a href="gopher://127.0.0.1:6379/_INFO">b</a>'
        '<a href="javascript:alert(1)">c</a><a href="data:text/html,x">d</a><a href="/ok">e</a>',
        "https://example.com/")
    assert links == ["https://example.com/ok"], f"dangerous scheme survived: {links}"


def t_calendar_trap_covers_the_benchmark_url_forms() -> None:
    """The 64-URL crawler-trap benchmark exposed real gaps: the original matcher only understood
    YYYY-MM (hyphen) and years 19xx-21xx, so it missed the compact and year-only path forms that
    real calendar traps use, and it missed far-future years entirely. All measured live in the
    benchmark (proof URLs return HTTP 200 with the same navigation)."""
    from crawler import errors
    NEXT = NOW_Y + 9
    caught = [
        f"https://events.uic.edu/day/date/{NEXT}0101",              # Localist compact YYYYMMDD
        f"https://www.moongiant.com/calendar/January/{NEXT}/",      # year-only path segment
        f"https://www.calendar-12.com/moon_calendar/{NEXT}/january",
        f"https://googleblog.blogspot.com/{NEXT}_01_01_archive.html",  # YYYY_MM_DD
        f"https://tidesandcurrents.noaa.gov/x.html?bdate={NEXT}0101",
        f"https://x.org/events/{NEXT}-04/",                          # the original hyphen form
    ]
    for u in caught:
        assert ec.calendar_trap(u), f"benchmark calendar form not caught: {u}"

    # The far-future year (3099 etc.) must be handled, not just the 2000s — anchoring to 19/20/21
    # let every benchmark proof URL through.
    assert ec.calendar_trap("https://x.org/calendar/2099/12/"), "year 2099 path missed"

    # False positives the broadened matcher must still avoid — a future year in ordinary content is
    # not a calendar. This is the guard that lets the detector be aggressive safely.
    for ok in (f"https://x.org/products/{NEXT}-concept-car",
               f"https://x.org/blog/the-{NEXT}-problem",             # year in a slug, not a path seg
               "https://x.org/article/12345678",                    # 8-digit id, not a date
               f"https://x.org/events/{NOW_Y}-04/"):                 # THIS year is real content
        assert not ec.calendar_trap(ok), f"false positive on {ok}"

    # The Wayback Machine's 14-digit YYYYMMDDhhmmss stamp. The compact matcher ended in (?!\d), so
    # six more digits after the day made it refuse — and an archive replay dated year 3099, which is
    # an unbounded URL space, read as an ordinary page. Found by running the shipped detector over
    # the calendar corpus rather than by reading the regex.
    assert ec.calendar_trap(f"https://web.archive.org/web/{NEXT}0101000000/https://www.bbc.co.uk/"), \
        "14-digit Wayback timestamp still slips past the compact-date matcher"
    # Same shape, a date in the PAST: that is an ordinary archived page and must stay crawlable.
    assert not ec.calendar_trap("https://web.archive.org/web/20200101000000/https://www.bbc.co.uk/")
    # Widening to \d{6} must not degrade into "any long digit run starting with a plausible date".
    assert not ec.calendar_trap(f"https://x.org/product/{NEXT}1234567890")
    assert not ec.calendar_trap(f"https://x.org/order/{NEXT}12312359")   # 12 digits: neither 8 nor 14


def t_deep_pagination_past_a_sane_bound_is_a_trap() -> None:
    """Path-form deep pagination (forum.xda .../page-300) is a trap the query-variant cap misses,
    because the page number is in the PATH not the query. Benchmark-verified: page-100 and page-300
    return an identical link set. A crawler must still follow the early pages."""
    from crawler import errors
    assert errors.looks_like_trap("https://forum.xda-developers.com/f/apps.5124/page-300") == errors.TRAP
    assert errors.looks_like_trap("https://x.org/blog/page/500/") == errors.TRAP
    assert errors.looks_like_trap("https://x.org/f/apps/page-101") == errors.TRAP
    # …but the ordinary early pages a real listing needs must pass.
    assert errors.looks_like_trap("https://forum.xda-developers.com/f/apps.5124/page-2") is None
    assert errors.looks_like_trap("https://x.org/news/page/2/") is None
    assert errors.looks_like_trap("https://x.org/products?page=5") is None


def t_redirect_loop_is_measured_not_guessed() -> None:
    assert ec.diagnose("http://x/", redirects=10).case == ec.CASE_REDIRECT_LOOP
    assert ec.diagnose("http://x/", status=200, redirects=3, body=ARTICLE).case == ec.CASE_OK


def t_js_shell_escalates_to_a_render() -> None:
    shell = '<html><body><div id="root"></div><script src="/bundle.js"></script></body></html>'
    v = ec.diagnose("http://x/", status=200, body=shell)
    assert (v.case, v.action) == (ec.CASE_JS_RENDER, ec.ACT_RENDER_JS)
    assert ec.diagnose("http://x/", status=200, body=ARTICLE).case == ec.CASE_OK


def t_cloudflare_tiers_route_to_different_actions() -> None:
    """The managed tier self-clears for a coherent fingerprint, so a stealth render is worth
    paying for. The interactive tier provably cannot self-clear, so spending a 90s render on it
    is pure waste — it must be skipped, not escalated."""
    body = ("<html><head><title>Just a moment...</title></head><body>"
            "<script>window._cf_chl_opt={{cvId:'3',cType:'{}',cRay:'a20'}};</script>"
            "<script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'></script></body></html>")
    hdr = {"cf-mitigated": "challenge"}
    managed = ec.diagnose("http://x/", status=403, headers=hdr, body=body.format("managed"))
    inter = ec.diagnose("http://x/", status=403, headers=hdr, body=body.format("interactive"))
    assert managed.action == ec.ACT_ESCALATE_C3, f"managed -> {managed.action}"
    assert inter.action == ec.ACT_SKIP, f"interactive -> {inter.action}"
    assert managed.case == inter.case == ec.CASE_ANTIBOT


def t_an_unclearable_wall_is_not_reported_as_an_IP_problem() -> None:
    """needs_network_path told the operator "the IP is the blocker, not the browser" and to buy a
    residential proxy. On the most common wall reaching that bucket — a Cloudflare INTERACTIVE
    challenge — that is measurably false: zoopla.co.uk returns 469,746 chars from our own datacenter
    IP once the challenge itself is solved. The egress was never the problem, and no proxy changes
    the outcome, so the advice cost money and time for nothing.

    An unclearable wall must therefore be split out BEFORE the netpath bucket, and named."""
    from crawler import async_engine as ae
    from crawler import errors

    e = ae.AsyncEngine.__new__(ae.AsyncEngine)
    interstitial = ("<html><head><title>Just a moment...</title></head><body><script>"
                    "window._cf_chl_opt={{cvId:'3',cType:'{}',cRay:'a20'}};</script>"
                    "<script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'>"
                    "</script></body></html>")

    it = ae.WorkItem(url="https://www.zoopla.co.uk/to-rent", depth=0, ctx=None)
    it.last_body = interstitial.format("interactive")
    wall = e._unclearable_wall(it)
    assert wall is not None, "an interactive challenge must not fall through to needs_network_path"
    kind, remedy = wall
    assert kind == "cloudflare_interactive", kind
    assert "IP is NOT the blocker" in remedy, remedy
    assert "solve_challenge.py" in remedy, "the remedy must name something that actually works"

    # A MANAGED challenge self-clears once the fingerprint is coherent, so it must stay in the
    # normal ladder — calling it unclearable would abandon pages C3 can genuinely get.
    it.last_body = interstitial.format("managed")
    assert e._unclearable_wall(it) is None

    # No body at all → we genuinely do not know; do not invent a diagnosis.
    it.last_body = ""
    assert e._unclearable_wall(it) is None

    # Terminal, because no retry or re-route can change it.
    assert errors.bucket_reason("cloudflare_interactive") == "terminal"
    # …and the generic bucket must no longer state the IP claim as fact.
    assert "the IP is the blocker" not in errors.reason_detail(errors.NEEDS_NETWORK_PATH)


def t_walls_are_never_storable_and_carry_a_persistable_reason() -> None:
    """`blocking` is what gates the upsert — if a wall reported False, the login form or the
    "not found" body would land in the corpus as a document."""
    for case in (ec.CASE_AUTH_WALL, ec.CASE_SOFT_404, ec.CASE_ANTIBOT, ec.CASE_GEO_BLOCK,
                 ec.CASE_CALENDAR_TRAP, ec.CASE_SSL, ec.CASE_DNS):
        assert ec.Verdict(case).blocking is True, case
    assert ec.Verdict(ec.CASE_OK).blocking is False
    assert ec.Verdict(ec.CASE_OK).reason is None
    from crawler import errors
    assert ec.Verdict(ec.CASE_CALENDAR_TRAP).reason == errors.TRAP
    assert ec.Verdict(ec.CASE_DNS).reason == errors.DNS
    # A case with no pre-existing errors.* constant still yields a stable, queryable string.
    assert ec.Verdict(ec.CASE_PAYWALL).reason == "paywall"


def t_visible_text_ignores_script_and_style() -> None:
    html = "<html><style>.a{color:red}</style><script>var x=1;</script><p>Hello there</p></html>"
    assert ec.visible_text(html) == "Hello there"


def t_slow_trickle_body_is_cut_loose_not_allowed_to_hold_a_worker() -> None:
    """A server that drip-feeds bytes never trips httpx's read timeout, because that timeout
    measures the silence BETWEEN chunks, not the total. Measured before the fix: 26s elapsed,
    14 bytes received, no exception, with a 10s read timeout configured. With a bounded worker
    pool a handful of such hosts starve the entire crawl — and it looks like "the sites are slow"
    rather than like a bug. Nutch ships the same knob (http.time.limit) for the same reason.

    Asserts the reason is TIMEOUT and not TOO_LARGE: the body was slow, not big, and the two get
    opposite retry policies (transient requeue vs terminal)."""
    import os
    import socket
    import threading
    import time as _t

    from crawler import errors
    from crawler.fetcher import Fetcher

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def trickle():
        try:
            conn, _ = srv.accept()
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
            for _ in range(200):
                conn.sendall(b"x")
                _t.sleep(1)                      # never silent long enough to time out a read
        except Exception:
            pass

    threading.Thread(target=trickle, daemon=True).start()
    prev = os.environ.get("CRAWLER_BODY_BUDGET_S")
    os.environ["CRAWLER_BODY_BUDGET_S"] = "3"
    try:
        f = Fetcher("test-agent", timeout_s=30, delay_s=0, respect_robots=False,
                    prefer_fixtures=False, allow_network=True)
        t0 = _t.monotonic()
        r = f.fetch(f"http://127.0.0.1:{port}/")
        took = _t.monotonic() - t0
    finally:
        if prev is None:
            os.environ.pop("CRAWLER_BODY_BUDGET_S", None)
        else:
            os.environ["CRAWLER_BODY_BUDGET_S"] = prev
        srv.close()

    assert r.reason == errors.TIMEOUT, f"expected timeout, got reason={r.reason!r} error={r.error!r}"
    assert took < 15, f"budget was 3s but the fetch held the worker for {took:.1f}s"
    # And the taxonomy has to agree, or the retry policy diverges from the diagnosis.
    assert ec.diagnose("http://x/", error=r.error).case == ec.CASE_TIMEOUT


def t_soft_404_slug_title_and_redirect_route() -> None:
    """Two soft-404 shapes measured live on adanidefence.com (2026-07-27).

    The crawler stored `/DefenceMedia` as a real document. The site answers HTTP 200, the visible
    "404" is an IMAGE so no body text says it, and the title is the route SLUG `page-not-found` —
    which none of the phrase tables matched, because every phrase in them is spaced English.
    """
    H = {"content-type": "text/html"}
    body = ("<html><head><title>page-not-found</title></head><body>Back to Home Company About Us "
            "Media Our Capabilities Unmanned Systems Missiles Careers Contact Us</body></html>")

    v = ec.diagnose("https://www.adanidefence.com/DefenceMedia", status=200, headers=H, body=body)
    assert v.case == ec.CASE_SOFT_404, f"slug title read as {v.case} — a 404 page enters the corpus"
    assert v.action == ec.ACT_MARK_GONE

    # The redirect alone is enough, even with an innocent title and a full-looking page.
    v = ec.diagnose("https://www.adanidefence.com/DefenceMedia", status=200, headers=H,
                 body="<html><head><title>Adani Defence</title></head><body>" + "x" * 4000 + "</body></html>",
                 final_url="https://adanidefence.com/page-not-found?item=%2fdefencemedia")
    assert v.case == ec.CASE_SOFT_404, f"redirect to a not-found route read as {v.case}"

    # ── and the three ways this could over-reach ──────────────────────────────
    # De-slugging joins words that were never adjacent, so a real article must survive it.
    v = ec.diagnose("https://ex.com/blog/how-to-handle-404-not-found-errors", status=200, headers=H,
                 body="<html><head><title>How to handle four-oh-four errors in nginx</title></head>"
                      "<body>" + "word " * 900 + "</body></html>")
    assert v.case != ec.CASE_SOFT_404, "an article about 404s was discarded as one"

    # Asking FOR a site's 404 page (our own corpus does this) is not a soft 404 by redirect.
    v = ec.diagnose("https://ex.com/page-not-found", status=200, headers=H,
                 body="<html><head><title>Our custom error page</title></head><body>"
                      + "word " * 900 + "</body></html>",
                 final_url="https://ex.com/page-not-found?x=1")
    assert v.case != ec.CASE_SOFT_404, "the requested URL already said not-found; no verdict from that"

    # An ordinary redirect must stay ordinary.
    v = ec.diagnose("https://ex.com/a", status=200, headers=H,
                 body="<html><head><title>Products</title></head><body>" + "word " * 900 + "</body></html>",
                 final_url="https://ex.com/products")
    assert v.case != ec.CASE_SOFT_404


def t_a_redirect_to_a_generic_error_route_is_a_soft_404() -> None:
    """The other half of the CMS soft-404, and 7 of the 12 the benchmark caught us missing.

    These sites answer a dead URL with 200 and a redirect to a generic ERROR route — nothing in the
    path says "404", so the not-found list misses it, and the landing page is frequently the whole
    homepage (bharatpetroleum.in/errorpage: 8,416 chars of real text), so no thin-page rule catches
    it either. Every positive below is a redirect target measured live on 2026-07-27.
    """
    H = {"content-type": "text/html"}
    # A FULL page, because the point is that length cannot be the discriminator here.
    BIG = "<html><head><title>Home</title></head><body>" + "word " * 2000 + "</body></html>"
    for asked, landed in [
            ("https://www.bharatpetroleum.in/this-page-does-not-exist-9f3a2b",
             "https://www.bharatpetroleum.in/errorpage"),
            ("https://www.exideindustries.com/nope", "https://www.exideindustries.com/error.aspx"),
            ("https://www.unionbankofindia.co.in/nope",
             "https://www.unionbankofindia.bank.in/en/home?aspxerrorpath=/nope"),
            ("https://mazagondock.in/nope", "https://mazagondock.in/Errors/Exception"),
            ("https://up.gov.in/nope", "https://up.gov.in/frmHttpErrorPage.aspx"),
            ("https://www.posco.co.kr/nope", "http://www.posco.co.kr/infomsg/error.html")]:
        v = ec.diagnose(asked, status=200, headers=H, body=BIG, final_url=landed)
        assert v.case == ec.CASE_SOFT_404, f"{landed} read as {v.case} — a 404 page enters the corpus"

    # ── the over-reach this rule is one careless edit away from ───────────────
    # The guard is deliberately ASYMMETRIC: strict page-level names on the landing side, but the
    # bare word "error"/"404" anywhere in the REQUESTED path clears it. Someone who asked about
    # errors and was redirected somewhere else about errors got what they asked for.
    for asked, landed, why in [
            ("https://docs.ex.com/errors", "https://docs.ex.com/tutorial/error.html",
             "a docs page about errors, moved"),
            ("https://ex.com/support/error-codes", "https://ex.com/help/error-codes",
             "error on both sides"),
            ("https://ex.com/404-explained", "https://ex.com/guides/error.html",
             "an article about 404s, moved"),
            ("https://ex.com/old", "https://ex.com/new", "an ordinary redirect")]:
        v = ec.diagnose(asked, status=200, headers=H, body=BIG, final_url=landed)
        assert v.case != ec.CASE_SOFT_404, f"false positive ({why}): {asked} -> {landed}"

    # A bare /error segment is NOT in the list on purpose: it is as likely to be a transient backend
    # fault page, and this verdict marks a URL permanently gone. Keep it that way.
    v = ec.diagnose("https://ex.com/nope", status=200, headers=H, body=BIG,
                    final_url="https://ex.com/error")
    assert v.case != ec.CASE_SOFT_404, "a bare /error redirect now marks URLs permanently gone"


def t_site_wide_login_widget_is_not_an_auth_wall() -> None:
    """A password field in a header/footer/modal must not condemn the page it sits on.

    Measured on blendmest.in (2026-07-27): a WooCommerce site whose every page carries a login
    form. 14 of its 16 pages were recorded `auth_wall` and never stored — including a 2,399-char
    homepage full of product copy — so a whole-site crawl kept exactly ONE page.

    Length alone cannot separate the two: github.com/settings/profile is a genuine wall at 618
    chars, that site's /cart is not one at 600. What separates them is the redirect — every real
    wall in the corpus lands on /login, /signin or an identity provider.
    """
    H = {"content-type": "text/html"}
    shop = ("<html><head><title>Drone Shop</title></head><body>"
            "<div class='content'>" + "Indigenous drone manufacturer product listing. " * 30 +
            "</div>"
            "<div id='login-modal'><form><input type='text' name='user'>"
            "<input type=\"password\" name=\"pass\"></form></div></body></html>")
    v = ec.diagnose("https://blendmest.in/shop/", status=200, headers=H, body=shop,
                    final_url="https://blendmest.in/shop/")
    assert v.case != ec.CASE_AUTH_WALL, (
        f"a site-wide login widget condemned a real page ({v.case}: {v.detail}) — this is how a "
        "whole WooCommerce site collapses to one stored page")

    # A page that is NOTHING but a form is still a wall.
    bare = ("<html><head><title>Sign in</title></head><body>"
            "<form><input type=\"password\" name=\"pass\"></form></body></html>")
    v = ec.diagnose("https://example.com/members/", status=200, headers=H, body=bare,
                    final_url="https://example.com/members/")
    assert v.case == ec.CASE_AUTH_WALL, f"a bare login form read as {v.case}"

    # A redirect to a login path is definitive even on a wordy page.
    v = ec.diagnose("https://github.com/settings/profile", status=200, headers=H,
                    body="<html><head><title>GitHub</title></head><body>" + "word " * 400 +
                         "<input type=\"password\"></body></html>",
                    final_url="https://github.com/login?return_to=%2Fsettings%2Fprofile")
    assert v.case == ec.CASE_AUTH_WALL, "a redirect to /login is no longer recognised"

    # A HOST that is the sign-in service: secure.login.gov has no "/login" in its path, and was
    # the one real detection lost when the bare-password rule was tightened.
    v = ec.diagnose("https://secure.login.gov/", status=200, headers=H,
                    body="<html><head><title>Sign in</title></head><body>" + "word " * 200 +
                         "</body></html>",
                    final_url="https://secure.login.gov/")
    assert v.case == ec.CASE_AUTH_WALL, "a host named login.* is not recognised as a sign-in service"

    # ...but only on a LABEL, so an ordinary company keeps its pages.
    v = ec.diagnose("https://logindustries.com/about", status=200, headers=H,
                    body="<html><head><title>Log Industries</title></head><body>" + "word " * 200 +
                         "</body></html>",
                    final_url="https://logindustries.com/about")
    assert v.case != ec.CASE_AUTH_WALL, "substring match on the host swept up an ordinary domain"


def t_an_exhausted_quota_is_read_in_every_spelling_servers_use() -> None:
    """"Nothing left in this window" has three spellings in the wild and we read one of them.

    Measured the same day across the edge-case corpus's OWN hosts: api.nasa.gov and NCBI send
    ``X-RateLimit-Remaining``; api.agify.io, api.crossref.org and api.microlink.io send the
    hyphenated ``X-Rate-Limit-Remaining``; content.guardianapis.com and api.magicthegathering.io
    send RFC 9440's un-prefixed ``RateLimit-Remaining``; codeberg.org sends the combined
    ``RateLimit: "baseline";r=1991;t=600``. The identical zero was detected on one host and
    invisible on the next purely because of a hyphen.

    The negatives matter as much as the positives here: this branch pauses a host, so a rule that
    fired on `r=0.5` or on a policy header that merely NAMES a quota would cool down every polite
    API the crawler talks to.
    """
    body = "<html><body>" + "hello world " * 400 + "</body></html>"

    def case(hdrs, status=200):
        return ec.diagnose("https://api.example.com/v1/thing", status=status, headers=hdrs,
                           body=body, final_url="https://api.example.com/v1/thing").case

    for hdr in ("x-ratelimit-remaining", "ratelimit-remaining", "x-rate-limit-remaining"):
        assert case({hdr: "0"}) == ec.CASE_RATE_LIMIT, f"{hdr}: 0 was not read as an exhausted quota"
    assert case({"ratelimit": '"baseline";r=0;t=600'}) == ec.CASE_RATE_LIMIT, \
        "the RFC 9440 combined field was not read"

    # Quota left, or no quota claim at all — the host must NOT be slowed down.
    for hdrs in ({"ratelimit": '"baseline";r=1991;t=600'},   # codeberg: 1,991 of 2,000 left
                 {"ratelimit": '"win";r=0.5;t=60'},          # a fraction is not zero
                 {"x-rate-limit-remaining": "24"},
                 {"ratelimit-policy": "150;w=900"},          # names a quota, reports no usage
                 {}):
        assert case(hdrs) != ec.CASE_RATE_LIMIT, f"{hdrs} was wrongly read as an exhausted quota"


def t_the_client_redirect_cap_is_a_loop_however_it_is_reported() -> None:
    """httpx signals its cap by RAISING, so the one exception that IS a redirect loop was the only
    one the transport branch could never name.

    Measured: httpbin.io/redirect/30 and httpbingo.org/redirect/30 declare more hops than httpx's
    max_redirects=20, so every fetch ends in TooManyRedirects("Exceeded maximum allowed
    redirects."). classify_failure had no pattern for it, so diagnose returned http_error +
    retry_later — wrong twice: it retries a URL that will exhaust the cap again at 20 requests a
    go, and it never records that the URL SHAPE is a trap, so siblings keep being enqueued.

    Both call sites are covered because they report it differently: the harness sets redirects=99,
    while the production fetcher records only str(exc) and has no hop count at all."""
    msg = "TooManyRedirects: Exceeded maximum allowed redirects."
    for kwargs in ({"error": msg, "redirects": 99},        # harness
                   {"error": "Exceeded maximum allowed redirects."}):   # production fetcher
        v = ec.diagnose("https://httpbin.io/redirect/30", **kwargs)
        assert v.case == ec.CASE_REDIRECT_LOOP, f"{kwargs} -> {v.case}"
        assert v.action == ec.ACT_DROP_TRAP, v.action
    assert errors.classify_failure(None, msg) == errors.TRAP, "the reason vocabulary still has no name for it"

    # Narrow on purpose: an ordinary transport failure keeps its own verdict.
    assert ec.diagnose("https://x/", error="ReadTimeout: timed out").case == ec.CASE_TIMEOUT
    assert ec.diagnose("https://x/", error="ConnectError: refused").case == ec.CASE_CONN
    # ...and a chain that completed within the cap is still counted, not just the exception.
    assert ec.diagnose("https://x/", redirects=25).case == ec.CASE_REDIRECT_LOOP


def t_an_ip_reputation_block_is_retryable_not_terminal() -> None:
    """The IP-block idiom none of the CDN-shaped markers could see: a short sentence with no vendor
    name, no "access denied" and no reference id.

    The DIRECTION is the point. Unnamed, a 403 lands on CASE_HTTP_ERROR, which buckets TERMINAL —
    a live host we happen to be blocked from today is buried as unrecoverable, which is the exact
    mistake that buried 617 live bel-india URLs. Named, it is an anti-bot wall: retryable, and
    escalated to the tier that might actually clear it."""
    for body in ("<html><body>You&#39;ve been blocked by network security.</body></html>",
                 "<html><body>Access to this resource has been restricted due to unusual traffic "
                 "from your network.</body></html>"):
        v = ec.diagnose("https://www.reddit.com/r/all.json", status=403, headers=H, body=body)
        assert v.case == ec.CASE_ANTIBOT, f"{body[:40]!r} -> {v.case}"
        assert errors.bucket_reason(v.reason) == "retryable", \
            "a live host we are merely blocked from was filed as unrecoverable"

    # A real 403 with real content is still an ordinary HTTP error — the rule must not swallow it.
    v = ec.diagnose("https://x/", status=403, headers=H,
                    body="<html><body>" + "ordinary defence article text " * 300 + "</body></html>")
    assert v.case == ec.CASE_HTTP_ERROR, v.case


def t_a_server_enforced_consent_route_is_a_wall_however_long_its_own_page_is() -> None:
    """A CMP OVERLAY leaves the article in the response, which is why the harness has a
    `not_blocking` bucket. A server-side REDIRECT to a consent route is the other thing: the page
    was never sent. Measured — golem.de sends every section to /sonstiges/zustimmung/auswahl.html
    and serves 2,648 chars of pure consent copy, sailing past the 1,500-char gate; derstandard.at
    rewrites articles to /consent/tcf/<path> and missed that gate by 53 characters."""
    v = ec.diagnose("https://www.golem.de/news/", status=200, headers=H,
                    body="<html><body>" + "zustimmung datenschutz einwilligung " * 80 + "</body></html>",
                    final_url="https://www.golem.de/sonstiges/zustimmung/auswahl.html")
    assert v.case == ec.CASE_CONSENT_WALL, v.case

    # ...but only when we did NOT ask for it. A cookie policy someone linked to is a document.
    v = ec.diagnose("https://x.de/consent/tcf/", status=200, headers=H,
                    body="<html><body>" + "our cookie policy explains " * 400 + "</body></html>",
                    final_url="https://x.de/consent/tcf/")
    assert v.case != ec.CASE_CONSENT_WALL, "stored cookie policy was mistaken for a wall"

    # An age gate whose wording is painted by JS and never reaches the visible text at all.
    v = ec.diagnose("https://hpnotiq.example/", status=200, headers=H,
                    body='<html><body id="agegate">' + "<option>Country</option>" * 200 + "</body></html>")
    assert v.case == ec.CASE_CONSENT_WALL, v.case

    # Negative control: an ordinary article is never a consent wall.
    v = ec.diagnose("https://news.example/story", status=200, headers=H,
                    body="<html><body>" + "real defence article text " * 400 + "</body></html>")
    assert v.case == ec.CASE_OK, v.case


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("t_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} EDGE-CASE TESTS PASSED")


def test_edgecase_suite() -> None:
    main()


if __name__ == "__main__":
    main()

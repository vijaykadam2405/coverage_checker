"""Crawl-error taxonomy + per-status policy — single source of truth for
'what went wrong' (reason strings) and 'what to do about it' (policy verbs).

Reason strings are the keys of the per-job ``errors_by_reason`` summary and are
persisted in CrawlHistory. HTTP failures use ``"http_<status>"`` verbatim so the
per-status breakdown is free; non-HTTP categories are the constants below.

Nothing here imports the rest of the crawler package (no circular deps) — it is
pure classification + policy tables, consumed by fetcher/async_engine/harvest.
"""
from __future__ import annotations

import os
import re

# ── reason vocabulary (errors_by_reason keys; also stored in sqlite) ──────────
DNS = "dns"
SSL = "ssl"                         # cert/handshake — NEVER bypassed (see classify note)
CONN_REFUSED = "conn_refused"
TIMEOUT = "timeout"
RENDER_CRASH = "render_crash"
PARSE_ERROR = "parse_error"
TOO_LARGE = "too_large"
ROBOTS = "blocked_by_robots"
HOST_DOWN = "skipped_host_down"     # circuit breaker tripped this run
GONE_SKIP = "skipped_gone"          # known-404/410 from a prior run
OFF_PEAK = "skipped_off_peak"       # careful host, outside its allowed crawl window
BLOCKLISTED = "skipped_blocklisted"  # operator blocklist — this host is not part of the corpus
TRAP = "trap"                       # frontier heuristic drop (not a fetch error)
# The host's slots never freed inside the deflect budget, so the page was NEVER FETCHED — we never
# learned anything about it. These were emitted as bare strings by async_engine and so fell through
# bucket_reason's default to "terminal", telling the operator that 617 perfectly live bel-india.in
# product pages were unrecoverable. They are the most retryable failure there is.
HOST_BUSY = "host_busy"             # deflect budget expired waiting for a per-host slot
RENDER_TIMEOUT = "render_timeout"   # render never completed
RENDER_FAILED = "render_failed"     # render died for an unclassified reason
# A 4xx that a full human-like Playwright session STILL couldn't clear — needs a
# different network path (residential proxy / real headless session / source API),
# not another retry. Routed here so these are queryable instead of blending into http_403.
NEEDS_NETWORK_PATH = "needs_network_path"
# Cloudflare 525/526: the EDGE could not complete TLS to the ORIGIN. Our own connection to the edge
# was fine — this is somebody else's handshake, and it is transient far more often than not, so it
# is retryable and deliberately NOT errors.SSL (which is terminal AND a HARD_FAIL, i.e. mapping it
# there would circuit-break the host for a cert rotation we are not party to).
ORIGIN_SSL = "origin_ssl"
# The four content-wall verdicts edgecase.diagnose persists (edgecase.CONTENT_WALLS). They existed
# only as bare CASE strings with no entry in either bucket set, so bucket_reason's fail-closed
# default declared all four terminal — including consent_wall, whose own action is ACT_ESCALATE_C3,
# i.e. the crawler knew a stealth render clears it and the report said "unrecoverable" anyway.
# Exactly the bug class that buried HOST_BUSY, one layer up.
RATE_LIMITED = "rate_limited"  # throttled (429 / Retry-After / a throttle message on a 200)
NEEDS_RENDER = "needs_render"  # JS shell; the content exists, we just fetched the wrong tier
AUTH_WALL = "auth_wall"        # login/SSO gate — an account opens it, not a retry
PAYWALL = "paywall"            # subscriber gate — same
GEO_BLOCK = "geo_block"        # withheld by country / legal order (HTTP 451, or a geo body on a 200)
CONSENT_WALL = "consent_wall"  # CMP / age gate — C3 clears these, so it is RETRYABLE
# Live host was WAF-blocked but we served the content from the Wayback Machine's
# archived copy instead — a SUCCESS via a WAF-free path, tagged so the dashboard
# shows "routed around the block" distinctly from a raw fetch.
SERVED_FROM_ARCHIVE = "served_from_archive"
# Live host was WAF-blocked but the CamoFox stealth engine (C3) rendered it —
# a SUCCESS via engine-level fingerprint spoofing, distinct from a raw fetch.
SERVED_BY_CAMOFOX = "served_by_camofox"
# Live host was walled by a Cloudflare INTERACTIVE challenge that nothing in the container can clear;
# the host-side CDP solver (C4, scripts/host_solver.py on the real GPU) passed it and served the
# page. A SUCCESS via a tier the container physically cannot run, tagged so the dashboard shows it.
SERVED_BY_HOST_SOLVER = "served_by_host_solver"
# Live host TLS-fingerprint-blocked plain httpx, but a curl_cffi real-Chrome JA3 GET cleared it —
# a SUCCESS via a cheap impersonated fetch that skipped the browser/C3 escalation entirely.
SERVED_BY_IMPERSONATE = "served_by_impersonate"
# A page retrieved with TLS verification DISABLED by the per-host opt-in (Job.tls_insecure_hosts) —
# the honest equivalent of a human clicking "Advanced -> Proceed anyway". A SUCCESS, but an
# UNTRUSTED one: like the other SERVED_BY_* reasons it only appears in the recovered-by breakdown,
# never as a crawl_history error_category, so bucket_reason never sees it.
TLS_UNVERIFIED = "tls_unverified"
# The fallback reason: classify_failure() returns this when nothing else matched.
OTHER = "other"
# Captcha / solver reason codes (C3 solver integration)
CAPTCHA_SOLVED = "captcha_solved"
CAPTCHA_FAILED = "captcha_failed"
NEEDS_CAPTCHA_SOLVER = "needs_captcha_solver"


# A CDN IP-reputation block page served with HTTP 200 (Akamai "Access Denied" etc.). C3 forces
# status=200 on any render, so the BODY is the only signal that the stealth render actually got
# blocked at the IP layer rather than delivering real content.
_IP_BLOCK_MARKERS = ("access denied", "edgesuite.net",
                     "you don't have permission to access", "reference #")

# Phrases distinctive enough that ONE is proof — they don't occur in editorial prose. The weak
# markers above are Akamai-shaped and matched none of CloudFront's wall, which is why 709 CloudFront
# "403 ERROR / Request blocked" pages were stored as real documents (lockheedmartin, boeing, mbda,
# bel-india): CloudFront serves that body with HTTP **200**, so the status check never fired.
_BLOCK_PAGE_STRONG = (
    "the request could not be satisfied",            # CloudFront 403 wall
    "generated by cloudfront",
    "enable javascript and cookies to continue",      # Cloudflare interstitial
    "attention required! | cloudflare",
    "checking your browser before accessing",
    "verify you are human by completing the action",
    # The IP-reputation idiom, which none of the CDN-shaped markers above can see: the body is a
    # short sentence and carries no vendor name, no "access denied" and no reference id. Measured
    # 2026-07-27 — reddit.com/r/all.json and old.reddit.com/r/all/.json both answer 403 with
    # 189,906 bytes yielding 143 chars, "You've been blocked by network security."; zenodo.org
    # answers 403 with "Access to this resource has been restricted due to unusual traffic from
    # your network."
    #
    # Direction check, because this codebase has been burned getting it backwards: without these
    # the verdict is CASE_HTTP_ERROR on a 403, which buckets TERMINAL — a live host we are merely
    # blocked from today is buried as unrecoverable. With them it is CASE_ANTIBOT, which buckets
    # retryable and escalates to the stealth tier. A false positive costs one stealth render; a
    # false negative costs the host. Both phrases are wall copy that does not occur in prose.
    "blocked by network security",
    "unusual traffic from your network",
)


def is_ip_block_page(rendered: str | None) -> bool:
    """True if a rendered body is really a CDN IP-block page (Akamai 'Access Denied' etc.), not
    content. Title match, or >=2 markers — one marker alone would false-hit an article that merely
    mentions e.g. 'access denied'. Used to (a) stop counting a blocked C3 render as a success and
    (b) trigger a fresh-IP retry."""
    if not rendered:
        return False
    low = rendered.lower()
    return ("<title>access denied</title>" in low
            or any(m in low for m in _BLOCK_PAGE_STRONG)
            or sum(m in low for m in _IP_BLOCK_MARKERS) >= 2)


# A rendered anti-bot CHALLENGE / CAPTCHA interstitial — distinct from a plain IP block. These are
# walls a captcha solver or a better browser fingerprint might clear (so they flag as a captcha
# limitation, not needs_network_path). Distinctive enough that one is proof: none occur in prose.
# CRITICAL: markers must be ACTIVE-wall signals only — text/scripts that are gone once the challenge
# is passed. Widget SCRIPT sources (challenges.cloudflare.com/turnstile, hcaptcha.com/, recaptcha/)
# and the response FIELDS (cf-turnstile-response) stay in the DOM AFTER a pass, so keying on them made
# is_captcha_wall fire on the very page that proves success — measured on
# scrapingcourse.com/cloudflare-challenge, whose "You bypassed the Cloudflare challenge!" page still
# references challenges.cloudflare.com/turnstile and was therefore recorded as needs_captcha_solver.
# Keep only the interstitial text + the managed-challenge orchestrate path, which are absent post-pass.
_CAPTCHA_WALL_MARKERS = (
    # NOT the bare "/cdn-cgi/challenge-platform": Cloudflare's JS-Detection script
    # (/cdn-cgi/challenge-platform/scripts/jsd/main.js) is injected into EVERY page a Cloudflare site
    # serves, including fully-cleared ones — so keying on the bare prefix threw away real content.
    # Measured on dzone.com: C3 passed the challenge and captured 245KB of genuine article HTML,
    # which this marker then flagged as an uncleared wall (and reported as "needs_captcha_solver",
    # advising a solver key that provably cannot solve a managed challenge). The INTERSTITIAL is the
    # /h/<seed>/orchestrate/ path, which is gone once the challenge clears — same persistent-widget
    # trap as challenges.cloudflare.com/turnstile, which was removed here for the same reason.
    "just a moment", "/cdn-cgi/challenge-platform/h/", "cf-chl-",
    "checking your browser before accessing", "attention required! | cloudflare",
    "verify you are human", "verifying you are human", "enable javascript and cookies to continue",
    "px-captcha", "_incapsula_resource", "captcha-delivery.com/captcha",
    "please complete the security check", "pardon our interruption",
)


def is_captcha_wall(rendered: str | None) -> bool:
    """True if a rendered body is an anti-bot CHALLENGE / CAPTCHA interstitial (Cloudflare managed
    challenge, Turnstile, reCAPTCHA/hCaptcha, DataDome, Imperva…). Used so a wall C3 could not clear
    is flagged as a captcha LIMITATION (resolvable with a solver / better fingerprint) rather than a
    generic IP block — the operator asked for that distinction, with the URL kept in a log."""
    if not rendered:
        return False
    low = rendered.lower()
    return any(m in low for m in _CAPTCHA_WALL_MARKERS)


def http_reason(status: int) -> str:
    return f"http_{status}"


def _env_int(k: str, d: int) -> int:
    return int(os.environ.get(k, str(d)))


# ── policy verbs ──────────────────────────────────────────────────────────────
DROP = "drop"              # count, move on
GONE = "gone"              # persist as permanently gone (skip on future runs)
DISGUISE = "disguise"      # one retry with a browser UA (403 only — UA spoof is the ceiling)
RETRY_LATER = "retry_later"  # transient: one in-run requeue + next-run eligible
COOLDOWN = "cooldown"      # RETRY_LATER + put the whole host on cooldown (429/503)

_STATUS_POLICY = {
    400: DROP, 401: DROP, 403: DISGUISE, 404: GONE, 410: GONE,
    # 408 and 425 are the only 4xx codes that MEAN "ask again". policy_for_status falls through to
    # DROP for anything under 500, so both were terminal — the crawler threw away a page because
    # the server admitted IT ran out of time, and never went back. Same fail-closed default that
    # buried host_busy. Measured: diagnose(408) returned action=skip.
    408: RETRY_LATER,   # Request Timeout — the server timed us out, the page is fine
    425: RETRY_LATER,   # Too Early — "replay this later" is the entire semantics of the code
    429: COOLDOWN,
    # 451 is terminal by DECISION, not by falling through the else-branch. No retry, no proxy and
    # no browser changes a legal takedown; naming it here puts it in the terminal bucket on
    # purpose instead of by accident.
    451: GONE,
    500: RETRY_LATER, 502: RETRY_LATER, 503: COOLDOWN, 504: RETRY_LATER,
}


def policy_for_status(status: int) -> str:
    if status in _STATUS_POLICY:
        return _STATUS_POLICY[status]
    return RETRY_LATER if status >= 500 else DROP


# ── statuses whose REASON is not "http_<n>" ──────────────────────────────────
# A few statuses are a named transport failure wearing an HTTP code, and calling them http_<n>
# throws that away. The reason string drives more than reporting: errors.TIMEOUT is what arms the
# per-host bump_timeout() and the WEAK_UNDER_LOAD auto-careful throttle, and neither fires for a
# bare http_522. Measured on cliqset.com, a dead origin behind Cloudflare: edgecase.diagnose said
# `timeout`, errors said `http_522` — the two classifiers disagreed about the same response. This
# table is what keeps them in sync.
_STATUS_REASON = {
    408: TIMEOUT,       # Request Timeout — the server gave up waiting on US (RFC 7231 §6.5.7)
    451: GEO_BLOCK,     # withheld for legal reasons
    # Cloudflare origin-side errors: the EDGE answered, the ORIGIN did not.
    522: TIMEOUT,       # "Connection Timed Out"  — origin never completed the handshake
    523: TIMEOUT,       # "Origin Is Unreachable"
    524: TIMEOUT,       # "A Timeout Occurred"    — origin accepted, never answered
    525: ORIGIN_SSL,    # "SSL Handshake Failed"      } the EDGE->ORIGIN TLS leg, not ours
    526: ORIGIN_SSL,    # "Invalid SSL Certificate"   }
}


def status_reason(status: int) -> str:
    """Reason for an HTTP status: a named transport category where one applies, else http_<status>."""
    return _STATUS_REASON.get(status, http_reason(status))


# categories that feed the per-host circuit breaker (a dead HOST, not a bad page)
HARD_FAIL = {DNS, CONN_REFUSED, SSL}
# categories eligible for an in-run requeue (transient — the page itself may recover)
TRANSIENT = {TIMEOUT, RENDER_CRASH}
# Weakness-under-load signature (the SIPRI case): a host that refuses/resets/times-out when hit at
# high concurrency should be auto-slowed to careful (conc 1) BEFORE the dead-host breaker trips.
# Distinct from HARD_FAIL (which kills the host) — this only throttles it. conn-reset/empty-response
# already fold into CONN_REFUSED (_PATTERNS), timeouts into TIMEOUT.
WEAK_UNDER_LOAD = {CONN_REFUSED, TIMEOUT}

# ── batch-report roll-up: a persisted error_category → completion bucket ──────
# RESOLVABLE = will be retried (in-run / C3 / fresh-IP / next run). TERMINAL = unresolvable even
# after retries. Kept (error_category NULL) is handled by the caller. http_* statuses are bucketed
# via policy_for_status so 429/5xx→retryable and 404/410/400/401→terminal fall out automatically.
_TERMINAL_REASONS = {GONE_SKIP, ROBOTS, SSL, TRAP, TOO_LARGE, PARSE_ERROR, "no_main_text",
                     # Walls only an ACCOUNT or a different country opens. Terminal by decision.
                     AUTH_WALL, PAYWALL, GEO_BLOCK}
_RETRYABLE_REASONS = {TIMEOUT, RENDER_CRASH, CONN_REFUSED, NEEDS_NETWORK_PATH,
                      HOST_DOWN, OFF_PEAK, DNS,
                      HOST_BUSY, RENDER_TIMEOUT, RENDER_FAILED,
                      # 525/526 is the site's own cert, usually mid-rotation; CONSENT_WALL's own
                      # action is ACT_ESCALATE_C3, so calling it unrecoverable contradicted the
                      # crawler's own plan for it.
                      ORIGIN_SSL, CONSENT_WALL, RATE_LIMITED, NEEDS_RENDER,
                      # Both are retryable because something CAN change the outcome: a warmer
                      # session or better fingerprint for a wait-only wall, a configured solver key
                      # for a widget. They are kept distinct from RENDER_CRASH so the backlog says
                      # which — "the browser died" and "a captcha stopped us" need different fixes.
                      CAPTCHA_FAILED, NEEDS_CAPTCHA_SOLVER}


def bucket_reason(reason: str | None) -> str:
    """None/'' → 'kept' (a success upsert cleared error_category). Else 'retryable' | 'terminal'."""
    if not reason:
        return "kept"
    if reason.startswith("http_"):
        try:
            pol = policy_for_status(int(reason[5:]))
        except ValueError:
            return "terminal"
        return "retryable" if pol in (RETRY_LATER, COOLDOWN) else "terminal"
    return "retryable" if reason in _RETRYABLE_REASONS else "terminal"


# What was already tried, and what would actually change the outcome. The report used to show only
# the raw reason, so "retryable" read as "we gave up on the first try" even when the full
# C1 -> impersonate -> C3 ladder had run. Keyed by reason; falls back to the raw reason.
_REASON_DETAIL = {
    # Deliberately no longer asserts "the IP is the blocker". That claim was stated as fact and was
    # measurably wrong on the most common case reaching this bucket: a Cloudflare INTERACTIVE
    # challenge, which never self-clears no matter what egress you use — zoopla.co.uk returns
    # 469,746 chars from our own datacenter IP once the challenge itself is solved. Sending the
    # operator to buy a residential proxy for that is worse than saying nothing. Unclearable walls
    # are now split out into their own reason before reaching here, so this bucket is genuinely
    # "we don't know", and it says so.
    NEEDS_NETWORK_PATH: ("blocked after httpx + real-Chrome JA3 + C3 stealth render all failed, with "
                         "no vendor wall we could identify. A different egress (residential/US, "
                         "CRAWLER_C3_PROXY_RETRY=1 with a proxy wired) is the usual next thing to "
                         "try — but confirm the block is IP-based first: an unclearable challenge "
                         "looks identical here and no proxy fixes it."),
    "cloudflare_interactive": (
        "Cloudflare INTERACTIVE challenge (cType=interactive). It never self-clears and injects no "
        "Turnstile widget, so no render, retry or proxy passes it — the IP is NOT the blocker. Run "
        "scripts/solve_challenge.py <url> on the host (SeleniumBase CDP solve_captcha), ~28s/URL."),
    TIMEOUT:        "host did not answer in time; retried in-run and re-queued for the next crawl.",
    CONN_REFUSED:   "host refused the connection (usually per-IP concurrency); auto-careful drops it to 1 and retries.",
    RENDER_CRASH:   "the browser render crashed; the tab was recycled and the page re-queued.",
    DNS:            "hostname did not resolve — check the URL, then the resolver.",
    HOST_DOWN:      "host was circuit-broken after repeated hard failures; retried after cooldown.",
    HOST_BUSY:      ("never fetched — the host's concurrency slots stayed full for the whole deflect "
                     "budget, so the crawl moved on rather than spin. The page itself is untested and "
                     "is very likely fine; re-crawl it, ideally with a smaller batch or a higher "
                     "CRAWLER_HOST_CONCURRENCY_CEIL so this host is not sharing slots with a big one."),
    RENDER_TIMEOUT: "the render never completed in the budget; the page was re-queued.",
    RENDER_FAILED:  "the render failed for an unclassified reason; worth one re-crawl.",
    ORIGIN_SSL:     ("Cloudflare could not complete TLS to the ORIGIN (525/526). Our connection to "
                     "the edge was fine — this is the site's own cert/handshake, usually a rotation "
                     "in progress. Retried; if it persists, only the site owner can fix it."),
    RATE_LIMITED:   ("the host throttled us (429 / Retry-After / a throttle message). The page is "
                     "fine — the rate was not. Retried after the host cools down."),
    NEEDS_RENDER:   ("the HTML was a JS shell, so the content exists but this tier could not see "
                     "it. Re-crawl with render_js on, or let the auto-escalation reach it."),
    AUTH_WALL:      "a login/SSO gate, not the page. Only an account opens it — no retry will.",
    PAYWALL:        "a subscriber gate served instead of the article. No retry changes this.",
    GEO_BLOCK:      ("withheld by country or legal order (HTTP 451, or a region message on a 200). "
                     "Terminal from this network; only a different exit country changes it."),
    CONSENT_WALL:   ("a CMP/age gate hid the content from the plain HTML fetch. The C3 stealth "
                     "render accepts the dialog and gets the real page — retried there."),
    OFF_PEAK:       "careful .gov/.mil host outside its allowed crawl window; retried next window.",
    GONE_SKIP:      "404/410 seen twice — the page is gone. No retry will recover it.",
    ROBOTS:         "robots.txt disallows this path. Not retried by policy.",
    SSL:            "TLS trust failure. Never bypassed — a broken cert is a real trust failure.",
    TRAP:           "dropped by the frontier trap heuristic (calendar/facet explosion), never fetched.",
    TOO_LARGE:      "response exceeded the size cap; not retried.",
    PARSE_ERROR:    "fetched but could not be parsed into a document.",
    CAPTCHA_FAILED: ("a captcha was detected and the solve did not clear it. Check the recorded "
                     "captcha_type: a Cloudflare managed challenge needs a better fingerprint or a "
                     "warmer session (no token can solve it), a widget needs a working solver."),
    NEEDS_CAPTCHA_SOLVER: ("a captcha widget was detected but no solver could be used — either "
                           "CAPTCHA_PROVIDER/CAPTCHA_API_KEY is unset or the page exposed no "
                           "sitekey to build a task from. Nothing retries its way past this."),
}


def reason_detail(reason: str | None, status: int | None = None) -> str:
    """One line explaining what was already attempted and what would change the result."""
    if not reason:
        return "kept"
    if reason in _REASON_DETAIL:
        return _REASON_DETAIL[reason]
    if reason.startswith("http_"):
        kind = "retried" if bucket_reason(reason) == "retryable" else "not retried (terminal)"
        return f"server returned {reason[5:]}; {kind}."
    return reason


# ── exception / Chromium-net:: message → reason ──────────────────────────────
# Playwright exposes only error *messages* (net::ERR_*); httpx exposes exception
# text. Both are substring-matched here, first pattern wins. SSL is classified
# but the response is NEVER verify=False / ignore_https_errors — a broken cert is
# a real trust failure; we skip the page, we do not bypass it.
# ponytail: substring taxonomy; add a needle when a new failure string appears in logs.
_PATTERNS = [
    (DNS, ("getaddrinfo", "name or service not known", "nodename nor servname",
           "err_name_not_resolved", "temporary failure in name resolution")),
    (SSL, ("certificate", "ssl", "err_cert", "err_ssl", "handshake", "tlsv1", "sslv3")),
    # err_socket_not_connected / err_network_changed / err_address_unreachable were MISSING, so they
    # fell through classify_failure to an empty category: not retryable, not terminal, not
    # quarantined — the URL was simply dropped with no record of why. Found by the error_detail
    # column: five zf.com URLs carried "net::ERR_SOCKET_NOT_CONNECTED" under a NULL category.
    (CONN_REFUSED, ("connection refused", "err_connection_refused", "err_connection_closed",
                    "connection reset", "err_connection_reset", "err_empty_response",
                    "err_socket_not_connected", "err_network_changed", "err_connection_aborted",
                    "err_address_unreachable", "err_network_io_suspended", "err_socket_is_null",
                    # httpx/h11 wording for the same thing. These were MISSING, so an overloaded
                    # host that hangs up mid-request classified as OTHER — which is neither
                    # retryable nor terminal, and (worse) is not in WEAK_UNDER_LOAD, so the
                    # auto-careful throttle that exists precisely for "host refuses under load"
                    # never fired. Measured against httpstat.us: "RemoteProtocolError: Server
                    # disconnected without sending a response." fell straight through to OTHER.
                    "server disconnected", "peer closed connection", "remoteprotocolerror",
                    "incomplete chunked read", "connection broken", "protocolerror",
                    # The bare Python builtin exception NAMES, which carry no space and so matched
                    # none of the spaced needles above. Whenever a caller logs repr(exc) or
                    # type(exc).__name__ — which several do — this is the only form present.
                    "connectionrefused", "connectionreset", "connectionaborted", "brokenpipe",
                    # httpx raises a bare ConnectError (often with an EMPTY message) when the TCP
                    # connection cannot be established at all. Measured on cinchcast.com: the text
                    # was literally "ConnectError: ", which matched nothing and classified as OTHER
                    # — again invisible to the circuit breaker and to WEAK_UNDER_LOAD.
                    "connecterror", "readerror", "writeerror")),
    (TIMEOUT, ("timed out", "timeout", "err_timed_out", "err_connection_timed_out",
               "deadline exceeded")),
    # The client's own redirect cap. httpx raises TooManyRedirects("Exceeded maximum allowed
    # redirects.") after 20 hops and Chromium says ERR_TOO_MANY_REDIRECTS; neither matched any
    # needle above, so both landed in OTHER — unnamed, and invisible to anything that reasons about
    # traps. TRAP, not TIMEOUT or CONN_REFUSED: 20 hops were spent and the URL is no closer to a
    # page, and retrying spends 20 more. TRAP is already in _TERMINAL_REASONS, and it is the same
    # reason edgecase.CASE_REDIRECT_LOOP already maps to, so the two paths now agree.
    (TRAP, ("exceeded maximum allowed redirects", "too many redirects",
            "err_too_many_redirects", "toomanyredirects")),
]

# ERR_ABORTED is not a failure: Chromium aborts the navigation when the response turns out to be a
# download rather than a page. The URL is fine and the bytes are there — it is the browser that is
# the wrong tool. Kept out of _PATTERNS because the fix is to REFETCH over httpx, not to retry the
# render, and callers need to tell the two apart.
_NOT_A_PAGE = ("err_aborted", "net::err_aborted", "download is starting", "net::err_blocked_by")


def is_download_abort(error_text: str | None) -> bool:
    """True when the render failed only because the URL serves a file, not a page."""
    low = (error_text or "").lower()
    return any(n in low for n in _NOT_A_PAGE)


def classify_failure(status: int | None, error_text: str | None) -> str | None:
    """Reason for a FAILED fetch; None when the fetch succeeded (no status>=400, no error)."""
    if status is not None and status >= 400:
        return status_reason(status)
    if not error_text:
        return None
    low = error_text.lower()
    if low == ROBOTS:
        return ROBOTS
    if low == "offline_no_fixture":
        return OTHER
    for reason, needles in _PATTERNS:
        if any(n in low for n in needles):
            return reason
    if low.startswith(("render", "nav:", "content:")) or "render_failed" in low:
        return RENDER_CRASH
    return OTHER


# ── Retry-After parsing (429 / 503) ──────────────────────────────────────────
def parse_retry_after(value: str | None, cap_s: float = 300.0) -> float | None:
    """Retry-After header → seconds (delta-seconds int form OR HTTP-date). None if
    absent/unparseable. Clamped to cap_s so a hostile '31536000' can't freeze a host.

    Non-positive values return None, NOT 0 or a negative. Every caller combines this with
    ``or <default>``, so None yields the sane default cooldown — whereas a negative was TRUTHY and
    went straight through as the cooldown, putting the host's retry-time in the past and turning a
    hard block into a hot retry loop. Real blockers do emit this: harborfreight.com answers a
    permanent PerimeterX block with ``Retry-After: 0``. A server telling us to retry immediately
    while refusing us is not information worth obeying.
    """
    if not value:
        return None
    value = value.strip()
    try:
        secs = float(int(value))
        return min(secs, cap_s) if secs > 0 else None
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (dt - datetime.now(timezone.utc)).total_seconds()
        return min(max(secs, 0.0), cap_s)
    except Exception:
        return None


# ── careful hosts (.gov / .mil — "quiet quiet") ──────────────────────────────
def _careful_tlds() -> set[str]:
    raw = os.environ.get("CRAWLER_CAREFUL_HOSTS", ".gov,.mil")
    return {s.strip().lstrip(".").lower() for s in raw.split(",") if s.strip()}


def is_careful_host(host: str | None) -> bool:
    """Segment-exact suffix match: epa.gov ✓, ministry.gov.in ✓, defence.mod.gov.uk ✓,
    but govtrack.us ✗ and email.com ✗ (substring, not a domain segment)."""
    if not host:
        return False
    segments = host.lower().rstrip(".").split(".")
    tlds = _careful_tlds()
    return any(seg in tlds for seg in segments)


# ── off-peak crawl window for careful hosts ──────────────────────────────────
def _parse_hours(spec: str) -> list[tuple[int, int]] | None:
    """'0-6,20-23' → [(0,6),(20,23)]. 'off'/'' → None (always allowed)."""
    spec = (spec or "").strip().lower()
    if not spec or spec == "off":
        return None
    ranges: list[tuple[int, int]] = []
    for part in spec.split(","):
        if "-" not in part:
            continue
        a, b = part.split("-", 1)
        try:
            ranges.append((int(a) % 24, int(b) % 24))
        except ValueError:
            continue
    return ranges or None


def in_allowed_window(hour: int, spec: str) -> bool:
    """Is `hour` (0-23) inside the allowed spec? Ranges may wrap midnight (20-6)."""
    ranges = _parse_hours(spec)
    if ranges is None:
        return True
    for a, b in ranges:
        if a <= b:
            if a <= hour <= b:
                return True
        elif hour >= a or hour <= b:      # wraps midnight
            return True
    return False


def careful_off_peak_now(host: str | None) -> bool:
    """True when `host` is careful AND the current LOCAL hour is OUTSIDE
    CRAWLER_CAREFUL_HOURS — the URL should be skipped now and retried next run
    (polite: don't hammer a gov server during its business/peak hours)."""
    if not is_careful_host(host):
        return False
    spec = os.environ.get("CRAWLER_CAREFUL_HOURS", "off")
    from datetime import datetime
    return not in_allowed_window(datetime.now().hour, spec)


# ── URL trap heuristics (stateless, cheap) ───────────────────────────────────
# Parameter names that carry a PAGE INDEX. Shared with async_engine._looks_paginated so the trap
# guard and the follow-heuristic can never drift apart — they did: ?pagi= (pixabay) was followed as
# pagination but invisible to this trap, so a "jump to last page" link (?pagi=42187) was fetched.
PAGE_PARAM = r"pag(?:e|ed|i|ina|enum|e_num|e_no|enumber)?|pg|pn|p|start|offset|skip"
_DEEP_PAGE_RE = re.compile(rf"(?:[/?&](?:{PAGE_PARAM})[-=/])(\d{{3,}})", re.I)
# A page extension is decoration, not identity: /about-us and /about-us.html are the SAME segment
# for loop-detection purposes. Counting them as different is what let the cyientdlm.com trap
# through — see the measured numbers in _norm_seg's caller below.
_PAGE_EXT_RE = re.compile(r"\.(?:html?|php|aspx?|jsp|cfm|shtml)$", re.I)


def _norm_seg(s: str) -> str:
    return _PAGE_EXT_RE.sub("", s).lower()


# A path segment that is a language/locale tag: `de`, `en-GB`, `pt_BR`, `zh-Hans`. Matched against
# an explicit ISO-639-1 list rather than a bare `[a-z]{2}` — two-letter segments are everywhere in
# real paths (`hr`, `pr`, `qa`, `ai`) and treating all of them as locales would make the trap check
# below fire on ordinary sites. Used by looks_like_trap's language-switcher guard.
_LOCALE_SEG_RE = re.compile(
    r"(?:aa|ab|af|ak|am|ar|as|az|be|bg|bn|bo|br|bs|ca|cs|cy|da|de|el|en|eo|es|et|eu|fa|fi|fo|fr|"
    r"ga|gd|gl|gu|he|hi|hr|ht|hu|hy|id|ig|is|it|iw|ja|jv|ka|kk|km|kn|ko|ku|ky|la|lb|lo|lt|lv|mk|"
    r"ml|mn|mr|ms|mt|my|nb|ne|nl|nn|no|oc|pa|pl|ps|pt|ro|ru|rw|si|sk|sl|so|sq|sr|sv|sw|ta|te|tg|"
    r"th|tk|tl|tr|tt|uk|ur|uz|vi|xh|yi|yo|zh|zu)"
    r"(?:[-_][A-Za-z]{2,4})?", re.I)


def looks_like_trap(canon_url: str) -> str | None:
    """Cheap stateless URL-shape trap checks → TRAP or None. The STATEFUL
    query-explosion cap (calendar/facet traps) lives at the enqueue site — it
    needs per-job (host,path)->count state that doesn't belong in a pure function."""
    from urllib.parse import urlsplit

    if len(canon_url) > _env_int("CRAWLER_MAX_URL_LEN", 2000):
        return TRAP
    raw = [s for s in urlsplit(canon_url).path.split("/") if s]
    if len(raw) > 12:
        return TRAP
    # A scheme left sitting in the PATH is never a real resource — it is a malformed href the site
    # emitted (`href="https:/www.cyient.com/x"`) that we then resolved relative to the current page.
    # 639 of these were fetched from cyientdlm.com, each returning the same soft-404 shell.
    if "http:" in raw or "https:" in raw:
        return TRAP
    segs = [_norm_seg(s) for s in raw]
    counts: dict[str, int] = {}
    for s in segs:                       # /a/b/a/b/a/b … loop trap
        counts[s] = counts.get(s, 0) + 1
        if counts[s] >= 3:
            return TRAP
    # RELATIVE-LINK PATH ACCUMULATION. A site that serves `href="capabilities"` (no leading slash)
    # on /about-us/ sends the crawler to /about-us/capabilities, which serves the same menu, which
    # sends it to /about-us/capabilities/industries … forever, every hop a 200. No single segment
    # ever hits 3, so the loop check above never fires, and the paths stay under the 12-segment cap
    # for a long time. Measured on cyientdlm.com: 16,717 pages — 71% of an entire 23,664-page
    # batch — spent walking one site's phantom directory tree, 98% of those URLs carrying a
    # repeated segment, depth 8-9 dominating. The shipping guards caught exactly 0 of 16,601.
    #
    # The discriminator is TWO OR MORE segments each repeating: an accumulating menu revisits
    # several of its own entries, while a legitimate path repeats at most one (/download/download,
    # /news/news.html). Scored on the stopped batch — the three checks above together refuse
    # 15,572 of the 16,601 trap URLs (93.8%) and 0 of the other 8,887 real frontier URLs. The
    # survivors are all shallow (depth ≤ 6) and unreachable once their parents are refused.
    if sum(1 for v in counts.values() if v >= 2) >= 2:
        return TRAP
    # THE SAME TRAP WITH ONE REPEAT. The rule above needs TWO distinct segments repeating, which an
    # accumulating menu only trips once it happens to revisit two of its own entries. A menu that
    # revisits ONE while appending new segments grows unbounded straight past it: measured on
    # godrejenterprises.com, /contact-us/about-us/good-&-green/newsroom/about-us/locks-and-security/
    # legal/intralogistics/material-handling-equipment/... repeats only "about-us", so it sailed to
    # 11-12 segments and reached 24,244 pending URLs — 78% of that instance's whole frontier, which
    # sat at 0.0 pages/s because every one of them burned five renders and then gave up host_busy.
    #
    # DEPTH IS THE DISCRIMINATOR, not the repeat count. A real path can repeat a segment
    # (/download/download, /news/news.html) but is essentially never also eight deep. Scored across
    # four live frontiers before shipping: this refuses 24,251 trap URLs and 0 of the 36,439 real
    # ones beside them. The deepest legitimate survivor anywhere was SEVEN segments (wartsila.com
    # event pages, automation.honeywell.com support paths), so the bound sits directly above the
    # real data rather than at a guessed round number.
    if len(segs) >= _env_int("CRAWLER_TRAP_REPEAT_DEPTH", 8) and any(v >= 2 for v in counts.values()):
        return TRAP
    # LANGUAGE-SWITCHER ACCUMULATION. A switcher that links the CURRENT path under every locale
    # turns one page into ~20 children that are the same page one segment deeper, and each of those
    # does it again: /de/ -> /de/en/ -> /de/en/fr/ … Measured on hbc-radiomatic.com, where it grew
    # to 45,169 pending URLs — 54% of that instance's whole frontier — and 28,540 of them were nine
    # or more locales deep, with the crawl unable to finish because the frontier grew faster than it
    # drained.
    #
    # Every guard above misses it by a hair: the paths land on exactly 12 segments (the cap is
    # `> 12`), and only the leading locale ever repeats, so neither the 3x loop check nor the
    # two-segments-repeating check fires.
    #
    # THREE distinct locales, not two: plenty of ISO-639 codes are also ordinary English path words
    # — `it` (Italian / IT dept), `no`, `is`, `in`, `at`, `so`, `my`, `id`, `la` — so /support/it/
    # and /news/no/ must stay crawlable. Needing three DISTINCT ones makes a real path essentially
    # impossible while catching this trap at its second hop.
    locales = {s for s in segs if _LOCALE_SEG_RE.fullmatch(s)}
    if len(locales) >= 3:
        return TRAP
    # Deep pagination past a sane bound. A crawler legitimately follows page-2, page-3; page-800 of
    # an unbounded forum/archive is a trap that the query-variant cap misses when the page number
    # lives in the PATH (forum.xda .../page-300) rather than the query string. Bound at
    # CRAWLER_MAX_PAGE_DEPTH (default 100) — high enough that a real multi-page listing completes,
    # low enough that an infinite pager is cut off. Verified against the crawler-trap benchmark:
    # xda page-300 (link-identical to page-100) is refused; page-2 is not.
    m = _DEEP_PAGE_RE.search(canon_url)
    if m and int(m.group(1)) > _env_int("CRAWLER_MAX_PAGE_DEPTH", 100):
        return TRAP
    return None

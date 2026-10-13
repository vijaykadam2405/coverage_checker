"""Edge-case taxonomy — name the obstacle on a page, and say what to do about it.

``diagnose()`` is the single "problem identifier + remedy" entry point. It turns ONE observed
fetch (status, headers, body, redirect count, timing, exception text) into a :class:`Verdict`
carrying three things:

    case      what is wrong, from a fixed vocabulary (the CASE_* constants)
    action    what the crawler should do about it (the ACT_* constants)
    evidence  the concrete marker that fired, quoted

The evidence string is what the report quotes, so every branch must name the marker it matched —
never just restate the verdict. "soft_404: title 'Page Not Found', 412 chars of text" is usable;
"looks like a soft 404" is not.

WHY THIS EXISTS AS ONE MODULE
-----------------------------
The detectors already in the tree were each written at the site that needed them: status policy in
``errors``, anti-bot tiers in ``captcha``, URL-shape traps in ``errors.looks_like_trap``. That is
fine until you want to ask "what stopped us on this page?" in one place — for a report, a dashboard,
or a decision about which of three escalation ladders to spend money on. So this module COMPOSES
those rather than re-implementing them (``errors.classify_failure``, ``errors.is_ip_block_page``,
``errors.is_captcha_wall``, ``captcha.detect_http`` are all called, not duplicated), and adds only
the cases nobody had written a detector for yet: soft 404, auth wall, paywall, consent wall, geo
block, JS-shell pages, redirect loops, and far-future calendar traps.

Pure: stdlib + the two other pure modules. No engine, no I/O, no browser — so every branch is
unit-testable from a captured ``(status, headers, html)`` triple, which is exactly what the
edge-case harness records. See ``tests/test_edgecase.py``.

FALSE POSITIVES ARE THE REAL RISK
---------------------------------
Every detector here can misfire on a legitimate page, and a misfire is worse than a miss: it
discards content the crawl already paid for. Two rules keep that in check, and both are load-bearing
in the checks below:

  1. A body marker is only believed when the page is ALSO content-poor. Real articles mention
     "sign in" and "cookies" constantly; what they don't do is carry those words with 300 characters
     of visible text. Nearly every body check below is ``marker AND text_len < threshold``.
  2. Where a machine-readable declaration exists, prefer it to prose. ``isAccessibleForFree:false``
     in JSON-LD is the publisher's own statement that this is a paywall; "subscribe" in the footer
     is not.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit

from . import captcha, errors

# ── case vocabulary ──────────────────────────────────────────────────────────
CASE_OK = "ok"
CASE_JS_RENDER = "js_render"
CASE_DNS = "dns_failure"
CASE_TIMEOUT = "timeout"
CASE_SSL = "ssl_error"
CASE_CONN = "conn_error"
CASE_RATE_LIMIT = "rate_limit"
CASE_GEO_BLOCK = "geo_block"
CASE_ANTIBOT = "antibot"
CASE_CAPTCHA = "captcha"
CASE_AUTH_WALL = "auth_wall"
CASE_PAYWALL = "paywall"
CASE_CONSENT_WALL = "consent_wall"
CASE_SOFT_404 = "soft_404"
CASE_REDIRECT_LOOP = "redirect_loop"
CASE_CALENDAR_TRAP = "calendar_trap"
CASE_URL_SPACE_LOOP = "url_space_loop"
CASE_TOO_LARGE = "too_large"
CASE_GONE = "gone"
CASE_HTTP_ERROR = "http_error"

# ── action vocabulary ────────────────────────────────────────────────────────
ACT_STORE = "store"                     # nothing wrong; keep the page
ACT_RENDER_JS = "render_js"             # re-fetch in a real browser
ACT_ESCALATE_C3 = "escalate_c3"         # re-fetch in the stealth browser (CamoFox)
ACT_SOLVE_CAPTCHA = "solve_captcha"     # a solvable widget/gate is present
ACT_BACKOFF = "backoff"                 # honour Retry-After, slow this host down
ACT_RETRY_LATER = "retry_later"         # transient; requeue
ACT_SKIP = "skip"                       # record the reason, do not store, do not retry
ACT_MARK_GONE = "mark_gone"             # permanently dead; never fetch again
ACT_DROP_TRAP = "drop_trap"             # never enqueue URLs of this shape

# Cases whose page body must NEVER be stored as content — they are walls, not documents.
# ``harvest``/``async_engine`` gate the upsert on this so a login form or a "page not found"
# body can't land in the corpus and poison dedup.
NON_CONTENT = frozenset({
    CASE_DNS, CASE_TIMEOUT, CASE_SSL, CASE_CONN, CASE_RATE_LIMIT, CASE_GEO_BLOCK,
    CASE_ANTIBOT, CASE_CAPTCHA, CASE_AUTH_WALL, CASE_CONSENT_WALL, CASE_SOFT_404,
    CASE_REDIRECT_LOOP, CASE_CALENDAR_TRAP, CASE_URL_SPACE_LOOP, CASE_TOO_LARGE,
    CASE_GONE, CASE_HTTP_ERROR,
})

# Walls that arrive as a perfectly ordinary HTTP 200 with a real HTML body. Nothing in the fetch
# ladder catches these — the status is fine, the content type is fine, the markup parses — so
# without an explicit check the WALL becomes the stored document: a login form, a "page not found"
# body, or a paywall teaser lands in the corpus, and because every such page on a site is nearly
# identical they also poison near-duplicate detection. Anti-bot and captcha walls are deliberately
# NOT in this set; those are caught earlier by the C1 -> C2 -> C3 escalation ladder, and listing
# them here would double-handle them.
CONTENT_WALLS = frozenset({CASE_SOFT_404, CASE_AUTH_WALL, CASE_PAYWALL,
                           CASE_CONSENT_WALL, CASE_GEO_BLOCK})

# case → the ``errors`` reason string persisted in CrawlHistory, so the existing per-reason
# reporting keeps working and these verdicts show up in the same breakdown as everything else.
# Cases with no natural existing reason keep their own name (the vocabulary is the reason).
CASE_REASON = {
    CASE_DNS: errors.DNS, CASE_TIMEOUT: errors.TIMEOUT, CASE_SSL: errors.SSL,
    CASE_CONN: errors.CONN_REFUSED, CASE_GONE: errors.GONE_SKIP,
    CASE_TOO_LARGE: errors.TOO_LARGE,
    CASE_CALENDAR_TRAP: errors.TRAP, CASE_URL_SPACE_LOOP: errors.TRAP,
    CASE_ANTIBOT: errors.NEEDS_NETWORK_PATH, CASE_CAPTCHA: errors.NEEDS_CAPTCHA_SOLVER,
    # "Cases with no natural existing reason keep their own name" was fine as a naming rule and
    # wrong as a BUCKETING one: an unknown reason hits bucket_reason's fail-closed default and is
    # reported as unrecoverable. These two have exact existing equivalents.
    CASE_SOFT_404: errors.GONE_SKIP,      # ACT_MARK_GONE — identical meaning to a real 404/410
    CASE_REDIRECT_LOOP: errors.TRAP,      # ACT_DROP_TRAP — never enqueue this shape again
    # The four content walls keep their own names, which are now real constants in errors and
    # really are bucketed (auth/paywall/geo terminal, consent retryable because C3 clears it).
    CASE_AUTH_WALL: errors.AUTH_WALL, CASE_PAYWALL: errors.PAYWALL,
    CASE_GEO_BLOCK: errors.GEO_BLOCK, CASE_CONSENT_WALL: errors.CONSENT_WALL,
    CASE_RATE_LIMIT: errors.RATE_LIMITED, CASE_JS_RENDER: errors.NEEDS_RENDER,
    # CASE_HTTP_ERROR is deliberately absent and must NEVER be persisted: it spans 400 (skip) and
    # 500 (retry), so any single bucket for it would be wrong half the time. Every code path that
    # records a status uses errors.status_reason(), which yields the specific http_<n>.
}


@dataclass(frozen=True)
class Verdict:
    case: str = CASE_OK
    action: str = ACT_STORE
    evidence: str = ""
    detail: dict = field(default_factory=dict)   # vendor, variant, sitekey, retry_after…

    @property
    def blocking(self) -> bool:
        """True when the page body is a wall and must not be stored as content."""
        return self.case in NON_CONTENT

    @property
    def reason(self) -> str | None:
        """The ``errors`` reason string to persist, or None when nothing went wrong."""
        if self.case == CASE_OK:
            return None
        return CASE_REASON.get(self.case, self.case)

    def as_row(self, url: str) -> dict:
        return {"url": url, "case": self.case, "action": self.action,
                "evidence": self.evidence, **self.detail}


# ── cheap text extraction (no bs4 on the hot path) ───────────────────────────
_DROP_EL = re.compile(r"(?is)<(script|style|noscript|template|svg)\b[^>]*>.*?</\1\s*>")
_ANY_TAG = re.compile(r"(?s)<[^>]+>")


def visible_text(html: str | None) -> str:
    """Rough visible text: drop script/style/svg wholesale, strip remaining tags, squeeze space.
    Deliberately crude — this feeds LENGTH THRESHOLDS, not extraction, and it must stay cheap
    enough to run on every fetched page."""
    if not html:
        return ""
    return " ".join(_ANY_TAG.sub(" ", _DROP_EL.sub(" ", html)).split())


def _title(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    return " ".join(m.group(1).split()).lower() if m else ""


# Hyphens/underscores/plus → spaces. A CMS that routes to a slug often uses the slug AS the title:
# adanidefence.com answers a dead URL with `<title>page-not-found</title>`, which no phrase in the
# tables below matches, so a 404 page was stored as a real document (measured live, 2026-07-27).
# Applied only to the title and the URL path, never to body prose, where joining hyphenated words
# would invent phrases that were not written.
_SEP_RE = re.compile(r"[-_+./]+")


def _deslug(s: str) -> str:
    return _SEP_RE.sub(" ", s or "").strip()


def _path_of(u: str) -> str:
    """Path + query, lowercased. The query matters: the redirect target that prompted this carried
    the dead route in `?item=`, and a host-only compare would have missed it."""
    from urllib.parse import urlsplit
    try:
        p = urlsplit(u or "")
        return f"{p.path}?{p.query}".lower()
    except Exception:                                  # noqa: BLE001 — a URL we cannot parse is not evidence
        return ""


# Route names a site redirects to when a URL does not exist. Matched on the de-slugged PATH only,
# never the host or the body — "404" appearing in prose is a number, not a verdict.
_NOT_FOUND_PATHS = ("page not found", "not found", "404", "pagenotfound", "notfound",
                    "error 404", "page missing")

# The other half of the same behaviour: a CMS that redirects a dead URL to a generic ERROR route and
# answers 200. Nothing in the path says "404", so the list above misses it entirely, and the landing
# page is often the full homepage — bharatpetroleum.in/errorpage returns 8,416 chars of real text,
# so no thin-page rule catches it either. Measured on 7 of the 12 soft-404s the benchmark missed:
# /errorpage, /error.aspx, ?aspxerrorpath=, /Errors/Exception, /frmHttpErrorPage.aspx,
# /infomsg/error.html.
#
# Deliberately PAGE-LEVEL names, not a bare "error" path segment: a bare /error is as likely to be a
# transient backend fault page, and this verdict marks a URL permanently gone. Every needle here is
# a form actually observed, and the caller additionally requires that the REQUESTED path carried no
# error word — so a documentation page about error handling, reached by its own redirect, is safe.
_ERROR_ROUTES = ("errorpage", "error page", "error aspx", "error html", "error htm",
                 "error php", "error jsp", "error cshtml", "errors exception",
                 "aspxerrorpath", "httperrorpage", "customerror", "custom error")


# A body whose ENTIRE visible text is a CDN error reference and nothing else. Some Akamai
# properties answer a denied request with HTTP **200** and a 42-byte body reading
# "Reference #18.321cc517.1785142523.1d14bf3" - no markup, no title, no vendor cookie in the body.
# Measured live on rakuten.co.jp (2026-07-27, Server: AkamaiNetStorage): a crawler keying on status
# alone stores that as a successful fetch of the Rakuten homepage.
#
# Anchored ^...$ against the WHOLE visible text, so it cannot fire on a page carrying any content
# at all - "Reference #" in a footnote is not a match, only a body that is nothing BUT the
# reference. That anchoring is what makes a rule this short safe.
_CDN_REF_ONLY = re.compile(
    r"^\s*reference\s*#\s*[0-9a-f]{1,4}\.[0-9a-f]{6,12}\.\d{6,14}\.[0-9a-f]{4,16}\s*$", re.I)

# The page's own headings. A heading is a headline, not incidental prose, so it carries the same
# weight as <title> - and a CMS that leaves the title as the site name puts the only statement that
# the page is missing in the H1. Measured on rites.com: <h1>Page Not Found</h1> under
# <title>RITES || THE INFRASTRUCTURE PEOPLE</title>, where the 2,667 chars of visible text are
# entirely the site-wide navigation menu, so the title rule and the 1,500-char body rule both miss.
# H2 as well as H1, because a template that comments its H1 out still headlines the H2: iitj.ac.in
# serves <!--<h1>Sorry!</h1>--> <h2>Page/File Not Found</h2>.
_HEADING_RE = re.compile(r"(?is)<h[12][^>]*>(.*?)</h[12]>")


def _headings(html: str, limit: int = 4) -> list[str]:
    """Visible text of the first few H1/H2 headings, lowercased."""
    return [" ".join(_ANY_TAG.sub(" ", h).split()).lower()
            for h in _HEADING_RE.findall(html or "")[:limit]]


def _any(hay: str, needles) -> str | None:
    """First needle present in `hay`, or None — returned so the caller can quote it as evidence."""
    for n in needles:
        if n in hay:
            return n
    return None


# ── marker tables ────────────────────────────────────────────────────────────
# SPA shells: the page is a mount point, the content arrives from JS.
_SPA_MARKERS = ('id="root"', "id='root'", 'id="app"', "id='app'", 'id="__next"', 'id="__nuxt"',
                "__next_data__", "window.__nuxt__", "window.__initial_state__", "data-reactroot",
                "ng-version", "<app-root", "data-server-rendered")

# STRONG: unambiguous 404 boilerplate. Safe to act on from the <title> alone, with no length gate.
_NOT_FOUND = ("page not found", "404 not found", "page cannot be found", "page you requested",
              "page doesn't exist", "page does not exist", "page you are looking for",
              "sorry, we couldn't find", "sorry, we can't find", "oops! that page",
              "nothing found", "no longer exists", "error 404")
# WEAK: sites name the missing ENTITY rather than "page" — meetup.com serves
# "<title>Meetup | Group not found</title>" plus "the group you're looking for doesn't exist",
# which none of the strong phrases match, so its 404 was claimed by the consent check and the dead
# URL stayed in the frontier. But these phrases are ordinary English — "weapons not found in Iraq"
# is a headline, not a 404 — so they are only believed on a page that is ALSO nearly empty.
_NOT_FOUND_WEAK = ("not found", "doesn't exist", "does not exist", "no longer available",
                   "couldn't be found", "could not be found", "we can't find")

_LOGIN_URL_HINTS = ("/login", "/signin", "/sign-in", "/log-in", "/auth/", "/session/new",
                    "/account/login", "/users/sign_in", "/oauth/authorize", "/sso/")
# Landing on one of these means an identity provider took over the request. Kept separate from the
# path hints above because it needs no corroboration: nothing but an auth wall sends you here.
_SSO_HOSTS = ("accounts.google.com", "login.microsoftonline.com", "login.live.com",
              "signin.aws.amazon.com", "login.yahoo.com", "appleid.apple.com",
              "id.atlassian.com", ".okta.com", ".auth0.com", ".onelogin.com",
              "login.salesforce.com", "sso.")
_LOGIN_TEXT = ("sign in to continue", "please log in", "please sign in", "you must be logged in",
               "members only", "registration required", "login required", "create a free account",
               "sign up to continue", "log in to view", "sign in to view")

# The publisher's own machine-readable declaration beats any prose guess.
_PAYWALL_JSONLD = re.compile(r'(?i)"isaccessibleforfree"\s*:\s*(?:"?false"?|0)')
_PAYWALL_TEXT = ("subscribe to continue", "subscribers only", "to continue reading",
                 "this article is for subscribers", "already a subscriber",
                 "subscribe to read", "premium article", "you've reached your limit",
                 "free articles remaining", "become a member to read")

# Structural paywall hooks — markup a page only carries if it HAS a paywall. Deliberately not
# prose: "subscribe" appears in every newsletter footer on the web.
_PAYWALL_STRUCT = ('data-paywall', 'class="paywall', "class='paywall", "id=\"paywall",
                   "paywall-container", "meteredcontent", "data-metered", "subscriber-only",
                   'data-testid="paywall', "js-paywall", "piano-paywall", "tp-modal")
# Auth failures expressed as a JSON payload rather than a login page.
_JSON_AUTH_ERR = ('"not_authed"', '"unauthorized"', '"invalid_token"', '"invalid_auth"',
                  '"authentication_required"', '"oauthexception"', '"login_required"',
                  '"token_expired"', '"missing_token"', '"requires authentication"',
                  '"access_denied"', '"invalid_grant"')

_CMP_VENDORS = {"onetrust": "onetrust", "cookielaw.org": "onetrust", "cookiebot": "cookiebot",
                "usercentrics": "usercentrics", "didomi": "didomi", "sourcepoint": "sourcepoint",
                "sp-prod.net": "sourcepoint", "quantcast": "quantcast", "trustarc": "trustarc",
                "consent.youtube.com": "google", "consent.google.com": "google",
                "cmp.osano": "osano", "klaro": "klaro",
                # Vendors the benchmark actually met, plus the IAB TCF stub all of them define.
                # `__tcfapi` is the WEAKEST needle in this table - a publisher that ships a CMP
                # defines it on every page, gated or not - so it can only ever convict through the
                # length and consent-route gates below. Measured 2026-07-27: golem.de's gate page
                # and de.yahoo.com's real homepage both define it, and only the first is a wall.
                #
                # Every needle here was replayed over the whole 1,277-row corpus before being kept,
                # and two candidates were REMOVED for failing that: "termly.io" claimed four
                # webscraper.io test pages (673-1,265 chars of real product listing behind an
                # ordinary banner) and "cookiecontrol" claimed instagram.com's JS shell. A short
                # page that merely LOADS a CMP is not a wall, and the length gate alone cannot tell
                # the two apart - so the vendor table only carries needles that survived that check.
                "consentmanager.net": "consentmanager", "iubenda": "iubenda", "axeptio": "axeptio",
                "borlabs-cookie": "borlabs", "cookieyes": "cookieyes",
                "fundingchoicesmessages": "google-funding-choices", "__tcfapi": "iab-tcf"}

# A consent gate the SERVER enforces: it did not send the page, it sent us to a consent ROUTE and
# will keep doing so until a choice is recorded. That is a different mechanism from the usual
# overlay, and the only one that actually withholds content from an HTML crawler. Matched on the
# landing PATH and with no length gate, because the text being measured is the GATE's own copy.
# Measured 2026-07-27: golem.de/news/ -> /sonstiges/zustimmung/auswahl.html serves 2,648 chars of
# consent copy and zero article content, which sailed straight past the 1,500-char gate below, and
# derstandard.at's articles -> /consent/tcf/<original path> missed that gate by 53 chars.
_CONSENT_ROUTES = ("/consent/", "/consent?", "/consent.htm", "/cookie-consent", "/cookieconsent",
                   "/cookie-wall", "/cookiewall", "/zustimmung", "/collectconsent",
                   "/privacy-consent", "/gdpr-consent", "/cmp/")
# An age gate is a consent wall asking a different question, and sites give it its own route for
# the same reason. Measured: curaleaf.com -> /age-gate?returnurl=%2F (497 chars, <title>Age
# Gate</title>) and heineken.com -> /in/en/agegateway/?returnUrl= (3,043 chars, all country list).
_AGE_GATE_ROUTES = ("/age-gate", "/agegate", "/age_gate", "/agegateway", "/age-gateway",
                    "/age-verification", "/ageverification", "/age-check", "/agecheck",
                    "/verify-age", "/age-restriction")
_AGE_GATE = ("are you over 18", "are you 21 or older", "enter your date of birth",
             "verify your age", "must be of legal drinking age",
             # Quoted from live gates in the benchmark that none of the five above matched, because
             # each of those assumes the site asks in one particular form. Measured: curaleaf.com
             # "for use only by adults 21 years of age and older", hpnotiq.com "Let's see some ID.
             # Country of Residence", smirnoff.com "Please tell us your date of birth.",
             # cinzano.com "Hai l'eta legale per bere?".
             "years of age or older", "years of age and older", "of legal drinking age",
             "legal purchase age", "tell us your date of birth", "please enter your birth",
             "confirm your age", "see some id", "legale per bere")
# Structural age-gate hooks, in the same spirit as _PAYWALL_STRUCT above: markup a page only carries
# when it HAS a gate. Needed because the wording is increasingly painted by JS out of a JSON config
# and never reaches the visible text at all. Measured: hpnotiq.com id="agegate", curaleaf.com
# class="age-gate", smirnoff.com "dobFormat":"DD-MM-YYYY".
_AGE_GATE_MARKUP = ('id="age-gate', "id='age-gate", 'class="age-gate', "class='age-gate",
                    'id="agegate', "id='agegate", 'class="agegate', "class='agegate",
                    'data-age-gate', '"dobformat"', 'id="age-verification',
                    'class="age-verification')

_GEO_TEXT = ("not available in your country", "not available in your region",
             "not available in your location", "unavailable in your country",
             "isn't available in your country", "content is not available in your",
             "due to legal reasons", "for legal reasons", "outside of the united states",
             "only available in the", "access from your country", "geo-restricted",
             "geoblock", "this content is not available where you are",
             # Measured on the benchmark's licensed-operator rows, which are the sites legally
             # obliged to say WHY and therefore the most reliable geo evidence there is. Every
             # phrasing below is quoted from a live block page and none was matched by the list
             # above: the earlier entries all assume "not available IN your …", and these say
             # "unavailable FROM" or "cannot be accessed FROM" instead.
             "unavailable from your location", "unavailable from your country",
             "unavailable in your location", "unavailable in your region",
             "cannot be accessed from your location", "cannot be accessed from your country",
             "not accessible from your country", "not accessible in your country",
             "blocked in your country", "restricted in your country",
             "from a country we", "in your jurisdiction")

_RATE_TEXT = ("too many requests", "rate limit exceeded", "rate-limited", "slow down",
              "you are making too many", "request throttled", "quota exceeded")

# Calendar/date-navigation hints — one must be present before a far-future YEAR is called a trap,
# otherwise a product page for a "2035 model" would be dropped.
_CAL_HINTS = ("calendar", "/events", "event/", "agenda", "schedule", "archive", "date=",
              "month=", "year=", "ym=", "day=", "week=", "tribe-bar-date", "eventdate",
              "mini-calendar", "/cal/", "when=", "/day/", "/history/", "moon_calendar",
              "bdate=", "edate=", "/date/")
# A 4-digit year anywhere in the URL. NOT anchored to 19/20/21: a calendar trap accepts arbitrary
# far-future years, and the benchmark's proof URLs use 3099 — anchoring to the 2000s let every one
# of them through. 19xx-29xx is enough range to avoid matching ordinary 4-digit ids (a bare year
# still needs a calendar hint below, so this is not sufficient on its own).
_DATE_IN_URL = re.compile(r"(?<!\d)(1\d|2\d|30)(\d{2})(?!\d)")
# A full date with a separator: YYYY-MM, YYYY/MM, YYYY_MM.
_FULL_DATE = re.compile(r"(?<!\d)((?:1\d|2\d|30)\d{2})[-/_](0?[1-9]|1[0-2])(?![\d])")
# A compact 8-digit date, YYYYMMDD — Localist /day/date/29990101, Blogspot 3099_01_01, etc. The
# month/day ranges are checked so a random 8-digit id (an object key, a phone number) doesn't match.
#
# The optional trailing `\d{6}` is the 14-digit YYYYMMDDhhmmss timestamp the Wayback Machine puts in
# every URL (/web/30990101000000/https://...). Without it the closing (?!\d) saw six more digits and
# refused the match, so an archive replay dated year 3099 — an infinitely deep URL space — read as
# an ordinary page. Found by probing the shipped detector against the calendar corpus. Deliberately
# 6-or-nothing rather than \d*: that keeps the range checks meaningful instead of matching any long
# digit run that happens to open with a plausible date.
_COMPACT_DATE = re.compile(
    r"(?<!\d)((?:1\d|2\d|30)\d{2})[-_/]?(0[1-9]|1[0-2])[-_/]?(0[1-9]|[12]\d|3[01])(?:\d{6})?(?!\d)")


def _far_future_year(url: str, horizon_years: int = 1) -> int | None:
    """Largest 4-digit year in the URL that is further out than `horizon_years`, else None."""
    now = datetime.now(timezone.utc).year
    years = [int(a + b) for a, b in _DATE_IN_URL.findall(url)]
    future = [y for y in years if y > now + horizon_years]
    return max(future) if future else None


# Hostnames that resolve to a cloud provider's instance-metadata service. Fetching these from
# inside a container hands back IAM credentials in plain JSON.
_METADATA_HOSTS = {"metadata.google.internal", "metadata.goog", "instance-data",
                   "169.254.169.254", "fd00:ec2::254", "100.100.100.200"}


def private_target(url: str) -> str | None:
    """Evidence string if `url` points inside our own network, else None.

    A crawler follows links written by the pages it reads, so any page it fetches can steer it —
    that is server-side request forgery, and a crawler is the ideal confused deputy for it. The
    scheme filter in ``parse.extract_links`` already drops ``file://``/``gopher://``/``javascript:``,
    but an ordinary ``http://`` URL pointed at a private address sails through: the prize target is
    ``http://169.254.169.254/latest/meta-data/iam/security-credentials/``, which returns live cloud
    credentials as JSON — and this crawler would have stored them in the corpus as a document.

    ``same_domain_only`` masks this for most jobs, but it is not the control: jobs run with it off,
    and it is scope policy rather than a security boundary.

    LIMIT, stated plainly: this checks the address written in the URL. It does NOT stop DNS
    rebinding, where a public hostname resolves to 127.0.0.1 at connect time. Closing that needs a
    resolve-then-pin socket hook; the network-level egress block is the real defence. Set
    CRAWLER_ALLOW_PRIVATE_HOSTS=1 to crawl an intranet deliberately."""
    import ipaddress
    import os as _os
    if _os.environ.get("CRAWLER_ALLOW_PRIVATE_HOSTS") == "1":
        return None
    try:
        host = (urlsplit(url).hostname or "").lower().strip(".")
    except ValueError:
        return "unparseable host"
    if not host:
        return None
    if host in _METADATA_HOSTS or host == "localhost" or host.endswith(".localhost"):
        return f"cloud/loopback metadata host {host!r}"
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None                      # a normal hostname; DNS rebinding is out of scope, see above
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
        return f"private/link-local address {host!r}"
    return None


def calendar_trap(url: str, horizon_years: int = 1) -> str | None:
    """Evidence string if `url` walks into an unbounded calendar, else None.

    The frontier's existing query-variant cap keys on ``host+path``, so a calendar that varies a
    QUERY parameter (``/events/?date=2035-01``) is already capped at CRAWLER_MAX_QUERY_VARIANTS.
    A calendar that varies the PATH (``/events/2035-01/``) gets a fresh key on every hop, so the
    cap never fires and ``looks_like_trap`` sees nothing wrong either — two short segments, no
    repetition. That is the hole this closes.

    A full date (``YYYY-MM`` or the compact ``YYYYMMDD``) far in the future is self-evidently a
    calendar and needs no hint; a bare year does, or "/products/2030-concept" would be dropped.

    Verified against the 64-URL crawler-trap benchmark: the compact and year-only path forms
    (Localist ``/day/date/29990101``, ``/calendar/January/3099/``, Blogspot ``3099_01_01_archive``)
    were all missed by the original YYYY-MM-only matcher."""
    low = url.lower()
    now = datetime.now(timezone.utc).year
    # Compact 8-digit date first — most specific, needs no hint.
    m = _COMPACT_DATE.search(low)
    if m and int(m.group(1)) > now + horizon_years:
        return f"far-future compact date {m.group(0)!r} in URL (now {now})"
    m = _FULL_DATE.search(low)
    if m and int(m.group(1)) > now + horizon_years:
        return f"far-future date {m.group(0)!r} in URL (now {now})"
    year = _far_future_year(low, horizon_years)
    if year and _any(low, _CAL_HINTS):
        return f"far-future year {year} in a calendar-shaped URL (hint {_any(low, _CAL_HINTS)!r})"
    # A far-future year as its OWN path segment (/3099/, /2099/january/) is a calendar even without a
    # keyword hint — a standalone future year in the path is not something a normal content URL does.
    if year and re.search(rf"/{year}(/|$)", low):
        return f"far-future year {year} as a path segment (calendar/archive space)"
    return None


# The spellings servers actually use to say "your quota is spent". Only ``x-ratelimit-remaining`` was
# read, which is one convention out of three — measured live on this corpus's OWN hosts, same day:
# api.agify.io / api.genderize.io / api.microlink.io / api.crossref.org send the hyphenated
# ``X-Rate-Limit-Remaining``; content.guardianapis.com, crunchbase.com and api.magicthegathering.io
# send RFC 9440's un-prefixed ``RateLimit-Remaining``; codeberg.org sends the combined structured
# field ``RateLimit: "baseline";r=1991;t=600``. So the very same "0 left" that is detected on
# api.nasa.gov was invisible on api.agify.io purely because of one hyphen.
#
# Safe in both directions by construction: reading more spellings can only reach the conclusion this
# branch already draws from the x- form, it fires only when a server explicitly states ZERO, and
# CASE_RATE_LIMIT maps to errors.RATE_LIMITED, which bucket_reason calls RETRYABLE — so a misfire
# costs a cooldown, never a URL buried as unrecoverable.
_RL_REMAINING = ("x-ratelimit-remaining", "ratelimit-remaining", "x-rate-limit-remaining")
# `r=0` inside the RFC 9440 combined field. Bounded on the right so `r=0.5` and `r=01` do not match.
_RL_STRUCT_R0 = re.compile(r"(?:^|[;,\s])r\s*=\s*0(?![0-9.])")


def _quota_exhausted(h: dict) -> str | None:
    """Evidence string when the server says zero requests are left in the window, else None."""
    for name in _RL_REMAINING:
        if (h.get(name) or "").strip() == "0":
            return f"{name}: 0"
    if _RL_STRUCT_R0.search(h.get("ratelimit") or ""):
        return f"RateLimit: {h['ratelimit']!r} (r=0)"
    return None


# ── the detector ─────────────────────────────────────────────────────────────
def diagnose(url: str, *, status: int | None = None, headers: dict | None = None,
             body: str | None = None, final_url: str | None = None, redirects: int = 0,
             elapsed_s: float = 0.0, error: str | None = None,
             max_redirects: int = 10, content_length: int | None = None) -> Verdict:
    """Classify ONE observed fetch. First match wins, checks ordered most-specific first.

    `error` is an exception/net:: message when the fetch never produced a response; when it is set
    the transport classification is authoritative and no body check runs."""
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    html = body or ""
    low = html.lower()
    final = (final_url or url).lower()

    # 1. Redirect loop — measured, not guessed: the client hit its own cap. This runs BEFORE the
    #    transport branch, because httpx signals the cap by RAISING, so the one exception that IS
    #    a redirect loop was the only one the transport branch could never name. Measured:
    #    httpbin.io/redirect/30 and httpbingo.org/redirect/30 both declare more hops than httpx's
    #    own max_redirects=20, so every single fetch of them ends in
    #    httpx.TooManyRedirects("Exceeded maximum allowed redirects.") — for which
    #    errors.classify_failure has no pattern, so it fell through to OTHER and this returned
    #    CASE_HTTP_ERROR + ACT_RETRY_LATER. That is the wrong answer twice over: it retries a URL
    #    that will exhaust the cap again at 20 requests a go, and it never records that the URL
    #    SHAPE is a trap, so sibling URLs keep being enqueued.
    #
    #    The MESSAGE is tested as well as the count, because the count is not always present. The
    #    harness sets redirects=99 on this exception, but fetcher.py records only str(exc) and has
    #    no redirect-count field at all, so on the production path the message is the only evidence
    #    that survives. Deliberately narrow — it matches httpx's exact wording and nothing else, so
    #    a genuine read timeout or a dropped connection is still a timeout / conn_error.
    if redirects >= max_redirects or (error and
                                      "exceeded maximum allowed redirects" in error.lower()):
        return Verdict(CASE_REDIRECT_LOOP, ACT_DROP_TRAP,
                       f"{redirects} redirects (cap {max_redirects}), last {final[:90]!r}"
                       if redirects else
                       f"client redirect cap hit: {error.strip()[:90]}",
                       {"redirects": redirects})

    # 2. Transport failure — no response at all. errors.classify_failure owns this vocabulary.
    if error:
        reason = errors.classify_failure(None, error)
        snippet = error.strip().splitlines()[0][:120] if error.strip() else error
        if reason == errors.DNS:
            return Verdict(CASE_DNS, ACT_MARK_GONE, f"resolver: {snippet}")
        if reason == errors.SSL:
            # Never bypassed. A broken chain is a real trust failure, not an inconvenience —
            # verify=False here would silently accept a MITM for the whole crawl.
            return Verdict(CASE_SSL, ACT_SKIP, f"TLS: {snippet}")
        if reason == errors.TIMEOUT:
            return Verdict(CASE_TIMEOUT, ACT_RETRY_LATER, f"timeout after {elapsed_s:.1f}s: {snippet}")
        if reason == errors.CONN_REFUSED:
            return Verdict(CASE_CONN, ACT_RETRY_LATER, f"connection: {snippet}")
        return Verdict(CASE_HTTP_ERROR, ACT_RETRY_LATER, snippet)

    # 3. Calendar / URL-space traps — a URL-shape judgement, independent of the response.
    cal = calendar_trap(url)
    if cal:
        return Verdict(CASE_CALENDAR_TRAP, ACT_DROP_TRAP, cal)
    if errors.looks_like_trap(url):
        segs = [s for s in urlsplit(url).path.split("/") if s]
        return Verdict(CASE_URL_SPACE_LOOP, ACT_DROP_TRAP,
                       f"URL shape: {len(segs)} path segments, {len(url)} chars")

    # 4. Anti-bot / captcha. This MUST run before the generic status rules below, because several
    #    vendors express their block as a status those rules would claim first. Kasada answers a
    #    normal GET with HTTP 429 — measured on realestate.com.au, realtor.com and canadagoose.com,
    #    all three carrying x-kpsdk-ct. Read as a rate limit, that earns ACT_BACKOFF, and the
    #    crawler then waits and retries a wall that will 429 forever; read as Kasada, it escalates
    #    or is skipped. A genuine 429 from an ordinary server matches no vendor signature, so the
    #    rate-limit rule below still fires for it.
    det = captcha.detect_http(status, h, html)
    if det.found and det.blocking:
        ev = f"{det.kind}"
        if det.sitekey:
            ev += f", sitekey={det.sitekey[:24]}"
        if h.get("cf-mitigated"):
            ev += f", cf-mitigated={h['cf-mitigated']!r}"
        detail = {"vendor": det.kind, "solvability": det.solvability, "sitekey": det.sitekey}
        if det.solvability in ("token_injection", "vendor_task", "local_ocr"):
            return Verdict(CASE_CAPTCHA, ACT_SOLVE_CAPTCHA, ev, detail)
        if det.solvability == "wait_only":
            # A managed Cloudflare challenge self-clears for a coherent fingerprint — that is
            # exactly what C3 provides, so this is the one wall worth spending a stealth render on.
            return Verdict(CASE_ANTIBOT, ACT_ESCALATE_C3, ev, detail)
        return Verdict(CASE_ANTIBOT, ACT_SKIP, ev + " (not injectable — cannot self-clear)", detail)
    if errors.is_ip_block_page(html):
        return Verdict(CASE_ANTIBOT, ACT_ESCALATE_C3, "CDN block page served with HTTP 200",
                       {"vendor": "cdn_ip_block"})
    # ...but only while the page is not ITSELF saying this URL does not exist. Imperva injects
    # /_Incapsula_Resource into EVERY response it fronts - the cleared ones as well as the challenge
    # stubs - and stamps x-iinfo on all of them, so both the interstitial marker and the soft-block
    # heuristic below fire on an ordinary 404 page. Measured on iitj.ac.in: <title>Error...</title>,
    # <h2>Page/File Not Found</h2>, 78 chars, served through Incapsula. Read as a wall, a
    # permanently dead URL is escalated to the stealth tier on every crawl instead of being marked
    # gone. The comparison case still works: coles.com.au is Imperva's real challenge stub - 212
    # bytes, an EMPTY <body>, no title and no heading - so nothing here excuses it.
    #
    # Deliberately the TITLE and HEADINGS only, never the body text: a wall's own headline is
    # "Just a moment", "Access Denied" or "Attention Required", never "not found", whereas a stray
    # "not found" somewhere in a wall's markup would otherwise turn it into a permanent mark_gone.
    _own_words = " | ".join([_title(html)] + _headings(html))
    _says_gone = bool(_any(_own_words, _NOT_FOUND) or _any(_own_words, _NOT_FOUND_WEAK))
    if errors.is_captcha_wall(html) and not _says_gone:
        return Verdict(CASE_ANTIBOT, ACT_ESCALATE_C3, "anti-bot interstitial body",
                       {"vendor": "unknown"})
    # WAF soft-block: HTTP 200, valid HTML, vendor fingerprint present — and no content. Both
    # ScraperAPI's and ScrapingBee's docs concede they detect blocks by status code and leave this
    # case to the caller. Measured on delta.com: 8,164 bytes of Akamai sensor JS yielding 57 chars
    # of text, served as 200. Without this it reads as a JS-render miss and the crawler pays for a
    # browser render of a wall. The text floor is what keeps it off ordinary SPA shells — sephora
    # carries the same Akamai cookies on a real 808KB page and is left alone.
    if det.found and len(html) > 1000 and len(visible_text(html)) < 200 and not _says_gone:
        return Verdict(CASE_ANTIBOT, ACT_ESCALATE_C3,
                       f"{det.kind} soft-block: HTTP {status} with {len(html)} bytes of HTML "
                       f"yielding {len(visible_text(html))} chars of text",
                       {"vendor": det.kind, "soft_block": True})

    # The same wall with the sensor JS stripped out: some Akamai properties answer a denied request
    # with HTTP 200 and a body that is ONLY the error reference. It is far too small for the byte
    # floor above (42 bytes, not 1,000) and carries no body marker at all, so nothing else here can
    # see it - measured on rakuten.co.jp, stored as a successful homepage fetch.
    if _CDN_REF_ONLY.match(visible_text(html)):
        return Verdict(CASE_ANTIBOT, ACT_ESCALATE_C3,
                       f"HTTP {status} whose entire body is a CDN error reference "
                       f"({visible_text(html)[:60]!r}) - the edge refused, the origin never ran",
                       {"vendor": det.kind or "cdn_reference"})

    # 5. Status-driven cases that need no body.
    ra = errors.parse_retry_after(h.get("retry-after"))
    spent = _quota_exhausted(h)
    if status == 429 or (status in (503, 403) and ra is not None) or spent:
        ev = f"HTTP {status}"
        if ra is not None:
            ev += f", Retry-After={h['retry-after']!r} ({ra:.0f}s)"
        if spent:
            ev += f", {spent}"
        return Verdict(CASE_RATE_LIMIT, ACT_BACKOFF, ev, {"retry_after_s": ra})
    if status == 451:
        return Verdict(CASE_GEO_BLOCK, ACT_SKIP, "HTTP 451 Unavailable For Legal Reasons")
    if status in (404, 410):
        return Verdict(CASE_GONE, ACT_MARK_GONE, f"HTTP {status}")
    if status in (522, 524, 523):
        # Cloudflare's origin-side errors: 522 "Connection timed out", 524 "A timeout occurred",
        # 523 "Origin is unreachable". The edge answered, the ORIGIN did not — which is a timeout
        # wearing a 5xx, and calling it one gets the right retry behaviour instead of a generic
        # server error. Measured on cliqset.com, a dead host still fronted by Cloudflare.
        return Verdict(CASE_TIMEOUT, ACT_RETRY_LATER,
                       f"HTTP {status} — Cloudflare edge up, origin did not answer")
    if status == 413 or (content_length and content_length > 50_000_000):
        return Verdict(CASE_TOO_LARGE, ACT_SKIP, f"HTTP {status}, content-length={content_length}")

    # 6. Remaining 4xx/5xx with nothing more specific to say.
    if status is not None and status >= 400:
        # ...unless the BODY says why. A geo block is routinely dressed as a plain 403 (bwin,
        # betfred, stoiximan) or even a 500 (tab.com.au) rather than the 451 the standard provides,
        # and the generic branch below files those as transient server faults — so the crawler
        # retries a permanent, legally-mandated refusal forever. The page states the reason in
        # words; believe the words over the status line.
        #
        # Only ahead of the generic branch, never ahead of the specific ones above: a 429 with a
        # Retry-After is still a rate limit even if the page also mentions your country.
        if html:
            phrase = _any(visible_text(html).lower(), _GEO_TEXT)
            if phrase:
                return Verdict(CASE_GEO_BLOCK, ACT_SKIP,
                               f"HTTP {status}, and the body says {phrase!r} — a geo block wearing "
                               f"a server error, not a fault to retry")
        pol = errors.policy_for_status(status)
        act = ACT_BACKOFF if pol == errors.COOLDOWN else (
            ACT_RETRY_LATER if pol == errors.RETRY_LATER else ACT_SKIP)
        if status in (401, 403):
            hint = _any(_path_of(final), _LOGIN_URL_HINTS)   # path, not the whole URL — see below
            if status == 401 or hint:
                return Verdict(CASE_AUTH_WALL, ACT_SKIP,
                               f"HTTP {status}" + (f", redirected to {hint!r}" if hint else ""))
        return Verdict(CASE_HTTP_ERROR, act, f"HTTP {status}")

    # 7. Body-driven cases on a 200. Ordered by how strong the evidence is, and every one of them
    #    is gated on the page ALSO being content-poor — see the module docstring.
    text = visible_text(html)
    tlen = len(text)
    tl = text.lower()
    title = _title(html)

    # Soft 404 by <title> comes FIRST among the body checks. A title saying the page does not
    # exist is the least ambiguous signal available here, and it must not lose a race to a weaker
    # one: measured on slideshare.net and meetup.com, whose 404 pages carry a OneTrust script, so
    # the consent check claimed them and the crawler would have kept re-fetching a dead URL
    # instead of marking it gone.
    phrase = _any(title, _NOT_FOUND)
    if phrase:
        return Verdict(CASE_SOFT_404, ACT_MARK_GONE, f"HTTP 200 but <title> says {phrase!r}")
    # The same phrases against a de-slugged title, so `page-not-found` reads as "page not found" —
    # a CMS that titles the page with its own route slug otherwise walks past every phrase above.
    # Gated on a SHORT title, because de-slugging joins words that were never adjacent: an article
    # headlined "how-to-handle-404-not-found-errors" would otherwise be marked gone.
    deslugged = _deslug(title)
    phrase = _any(deslugged, _NOT_FOUND)
    if phrase and len(deslugged) <= 40:
        return Verdict(CASE_SOFT_404, ACT_MARK_GONE,
                       f"HTTP 200 but <title> is the slug {title!r}")

    # Redirected to a not-found route. The strongest signal on this whole page: the site itself
    # decided the URL does not exist and sent us somewhere that says so in the path. Requires that
    # the REQUESTED url did not already look like that, or a genuine page about 404s (a help
    # article, our own edge-case corpus) would be discarded.
    if final_url and final_url != url:
        landed, asked = _deslug(_path_of(final_url)), _deslug(_path_of(url))
        hit = _any(landed, _NOT_FOUND_PATHS)
        if hit and not _any(asked, _NOT_FOUND_PATHS):
            return Verdict(CASE_SOFT_404, ACT_MARK_GONE,
                           f"HTTP 200 after redirect to {hit!r} — the site says this URL is gone")
        # Same signal, generic CMS wording. The two sides are deliberately asymmetric: the landing
        # path must match one of the specific page-level names above, while the REQUESTED path is
        # cleared by the mere word "error" or "404". Anyone who asked for something about errors and
        # was redirected somewhere else about errors got what they asked for — /errors landing on
        # /tutorial/error.html is a documentation move, not a dead URL. Being loose here costs a
        # detection on a genuinely dead /errors page; being strict cost a live docs page.
        hit = _any(landed, _ERROR_ROUTES)
        if hit and "error" not in asked and "404" not in asked:
            return Verdict(CASE_SOFT_404, ACT_MARK_GONE,
                           f"HTTP 200 after redirect to the error route {hit!r} — "
                           f"the site had no such page")
    # Body wording, still ahead of the consent/paywall checks for the same reason: measured on
    # meetup.com, whose 404 page carries a CMP script and was claimed by the consent check.
    # Weak phrases are admitted here — but only here, where the page must also be nearly empty.
    phrase = _any(tl[:2000], _NOT_FOUND) or _any(tl[:2000], _NOT_FOUND_WEAK)
    if phrase and tlen < 1500:
        return Verdict(CASE_SOFT_404, ACT_MARK_GONE,
                       f"HTTP 200 but body says {phrase!r} in {tlen} chars")
    # A weak phrase in the TITLE is believed only on a thin page, for the same reason.
    phrase = _any(title, _NOT_FOUND_WEAK)
    if phrase and tlen < 2500:
        return Verdict(CASE_SOFT_404, ACT_MARK_GONE,
                       f"HTTP 200, <title> says {phrase!r}, only {tlen} chars of text")

    # The same statement in the page's own heading. STRONG phrases only - never the weak list,
    # whose phrases are ordinary English and would convict a headline like "Weapons not found in
    # Iraq" - and gated on a page too thin to be an article ABOUT 404s: a developer post explaining
    # "404 Not Found" carries thousands of characters of body copy, while a 404 page carries the
    # site navigation and nothing else. Measured on rites.com, whose 2,667 chars are entirely its
    # navigation menu, so neither the 1,500-char body rule nor its 2,000-char window can see the
    # <h1>Page Not Found</h1> sitting underneath.
    for _h in _headings(html):
        phrase = _any(_h, _NOT_FOUND)
        if phrase and tlen < 3000:
            return Verdict(CASE_SOFT_404, ACT_MARK_GONE,
                           f"HTTP 200 but a heading says {phrase!r}, and the page carries only "
                           f"{tlen} chars of text (site navigation, no article)")

    # A JSON API refusing an unauthenticated caller. These never look like a login page — the body
    # is a few dozen bytes of JSON — so every length- and form-based check below misses them.
    # Measured: slack.com/api/auth.test returns HTTP 200 with {"ok":false,"error":"not_authed"},
    # i.e. a 200 that is really a 401, and graph.facebook.com/me returns 400 with an OAuthException.
    if tlen < 4000 and ("json" in str(h.get("content-type", "")).lower() or low.lstrip()[:1] in "{["):
        marker = _any(low, _JSON_AUTH_ERR)
        if marker:
            return Verdict(CASE_AUTH_WALL, ACT_SKIP,
                           f"JSON auth error {marker!r} (HTTP {status})")

    # An SSO redirect is definitive regardless of page size. The identity provider's sign-in page
    # is itself a big, wordy document — measured on drive.google.com, which lands on
    # accounts.google.com with 38,658 chars of text — so gating this on a short page (as the
    # generic login-URL check below does) meant the clearest auth wall in the corpus read as OK.
    # Matched against the HOST, not the whole URL: "sso." and ".okta.com" are host needles, and an
    # article at /notes/sso.html is not an identity provider.
    sso = _any((urlsplit(final).hostname or "").lower(), _SSO_HOSTS)
    if sso:
        return Verdict(CASE_AUTH_WALL, ACT_SKIP, f"redirected to identity provider {sso!r}")
    # Against the PATH, not the whole URL. Matching the whole string meant "//logindustries.com"
    # contained "/login" and every page of an ordinary manufacturer read as a login wall.
    hint = _any(_path_of(final), _LOGIN_URL_HINTS)
    if hint and tlen < 6000:
        return Verdict(CASE_AUTH_WALL, ACT_SKIP, f"landed on a login URL ({hint!r}), {tlen} chars")
    # The hints above are PATH hints, so a host that is itself the sign-in service slips past them:
    # secure.login.gov has no "/login" in its path. A hostname LABEL of login/signin/sso/auth is
    # the service announcing what it is — matched on labels, not a substring, so "logindustries.com"
    # is not swept up.
    host_labels = set((urlsplit(final).hostname or "").lower().split("."))
    named = host_labels & {"login", "signin", "sso", "auth", "accounts"}
    if named and tlen < 6000:
        return Verdict(CASE_AUTH_WALL, ACT_SKIP,
                       f"host is a sign-in service ({sorted(named)[0]!r}), {tlen} chars")
    phrase = _any(tl, _LOGIN_TEXT)
    if phrase and tlen < 2500:
        return Verdict(CASE_AUTH_WALL, ACT_SKIP,
                       f"login wall: {phrase!r}, only {tlen} chars of text")
    # A password FIELD with nothing else to back it up is the weakest signal in this function, and
    # it was quietly gutting ordinary sites. WordPress/WooCommerce — and most CMSs — put a login
    # form in a site-wide header, footer or modal, so EVERY page carries `<input type=password>`.
    # Measured on blendmest.in: 14 of its 16 pages were discarded as auth walls, including a
    # 2,399-char homepage full of product copy, and the crawl stored ONE page from the whole site.
    #
    # Length cannot separate the two — github.com/settings/profile is a real wall at 618 chars
    # while that site's /cart is not one at 600. What does separate them is the redirect: every
    # genuine wall in the corpus lands on /login, /signin or an identity provider, and all of those
    # are caught above. So a bare password field now only convicts a page too thin to be anything
    # but a form.
    has_pw = 'type="password"' in low or "type='password'" in low
    if has_pw and tlen < 400:
        return Verdict(CASE_AUTH_WALL, ACT_SKIP,
                       f"login wall: <input type=password> and only {tlen} chars of text")

    if _PAYWALL_JSONLD.search(html):
        # The publisher declared it. Hard vs soft decides whether the text is salvageable:
        # a metered paywall ships the full article and hides it with CSS, so it is still readable.
        soft = tlen > 4000
        return Verdict(CASE_PAYWALL, ACT_STORE if soft else ACT_SKIP,
                       f'JSON-LD isAccessibleForFree:false, {tlen} chars '
                       f'({"soft — full text present" if soft else "hard — teaser only"})',
                       {"variant": "soft" if soft else "hard"})
    phrase = _any(tl, _PAYWALL_TEXT)
    if phrase and tlen < 3000:
        return Verdict(CASE_PAYWALL, ACT_SKIP, f"paywall: {phrase!r}, only {tlen} chars",
                       {"variant": "hard"})
    # Soft/metered paywall on a page that still carries its full text. Keyed on STRUCTURAL markup
    # (a paywall CSS hook or a metering attribute), never on prose, because an article that merely
    # mentions subscriptions must not be labelled — the length gate that protects the other checks
    # is unavailable here by definition. The action is STORE: the text is present and readable, so
    # the only thing at stake is labelling it correctly. Measured on wired.com (12,574 chars),
    # newyorker.com (11,208) and technologyreview.com (11,603).
    marker = _any(low, _PAYWALL_STRUCT)
    if marker and tlen > 3000:
        return Verdict(CASE_PAYWALL, ACT_STORE,
                       f"soft paywall markup {marker!r}; full text present ({tlen} chars)",
                       {"variant": "soft"})

    # Geo and rate messages come BEFORE the consent check. Both are specific statements about why
    # this page is withheld; a CMP script is merely present. Measured on peacocktv.com and
    # paramountplus.com, whose region pages also load a consent manager — read as consent walls,
    # they would be escalated to a stealth render, which cannot change what country we are in.
    phrase = _any(tl, _GEO_TEXT)
    if phrase and tlen < 4000:
        return Verdict(CASE_GEO_BLOCK, ACT_SKIP, f"geo message: {phrase!r}, {tlen} chars")

    phrase = _any(tl, _RATE_TEXT)
    if phrase and tlen < 2000:
        return Verdict(CASE_RATE_LIMIT, ACT_BACKOFF, f"HTTP 200 but body says {phrase!r}")

    # A consent or age gate the site enforces by REDIRECT, checked ahead of the vendor scan and with
    # no length gate. The site took away the page we asked for and put a door in front of it, so
    # whatever text arrived belongs to the door. Requires that we did not ASK for the consent route,
    # exactly like the soft-404 redirect rule above, so a cookie policy someone linked to is still
    # stored as the document it is.
    if final_url and final_url != url:
        landed, asked = _path_of(final_url), _path_of(url)
        route = _any(landed, _CONSENT_ROUTES)
        if route and not _any(asked, _CONSENT_ROUTES):
            return Verdict(CASE_CONSENT_WALL, ACT_ESCALATE_C3,
                           f"redirected to the consent route {route!r}: {tlen} chars of consent "
                           f"copy and no page content", {"vendor": "server_side"})
        route = _any(landed, _AGE_GATE_ROUTES)
        if route and not _any(asked, _AGE_GATE_ROUTES):
            return Verdict(CASE_CONSENT_WALL, ACT_ESCALATE_C3,
                           f"redirected to the age gate {route!r}, {tlen} chars",
                           {"vendor": "age_gate"})
    # The same wall reached by HOST instead of by path - the CMP runs on its own subdomain and the
    # path says nothing. Measured on Yahoo's EU properties: uk.yahoo.com -> consent.yahoo.com.
    if (urlsplit(final).hostname or "").startswith("consent."):
        return Verdict(CASE_CONSENT_WALL, ACT_ESCALATE_C3,
                       f"landed on a consent host ({urlsplit(final).hostname}), {tlen} chars",
                       {"vendor": "server_side"})

    vendor = None
    for needle, name in _CMP_VENDORS.items():
        if needle in low or needle in final:
            vendor = name
            break
    # The consent ROUTE counts alongside the length gate and the consent HOST: a gate we were sent
    # to is a gate however thick its own page is. Measured on derstandard.at, 1,553 chars.
    if vendor and (tlen < 1500 or "consent." in final or _any(_path_of(final), _CONSENT_ROUTES)):
        return Verdict(CASE_CONSENT_WALL, ACT_ESCALATE_C3,
                       f"{vendor} consent gate, only {tlen} chars behind it", {"vendor": vendor})
    # An age-gate route serving in place at the URL we asked for, with no redirect to give it away.
    route = _any(_path_of(final), _AGE_GATE_ROUTES)
    if route:
        return Verdict(CASE_CONSENT_WALL, ACT_ESCALATE_C3,
                       f"age-gate route {route!r}, {tlen} chars", {"vendor": "age_gate"})
    # Structural gate markup, with a cap generous enough to survive the gate's own controls: an age
    # gate's visible text is mostly its dropdowns. Measured - hpnotiq.com yields 2,024 chars of
    # which the country <select> is 220 options, and hendricksgin.com 2,939 chars with zero <p>
    # over 150 - so the flat 2,000-char cap below was measuring the length of a list of countries
    # rather than the amount of content being withheld.
    marker = _any(low, _AGE_GATE_MARKUP)
    if marker and tlen < 4000:
        return Verdict(CASE_CONSENT_WALL, ACT_ESCALATE_C3,
                       f"age-gate markup {marker!r}, only {tlen} chars behind it",
                       {"vendor": "age_gate"})
    # The same allowance for the wording, granted only to a page that actually carries a big
    # <select>, so an ordinary page that mentions a date of birth keeps the strict cap.
    phrase = _any(tl, _AGE_GATE)
    if phrase and tlen < (3200 if low.count("<option") >= 20 else 2000):
        return Verdict(CASE_CONSENT_WALL, ACT_ESCALATE_C3, f"age gate: {phrase!r}, {tlen} chars",
                       {"vendor": "age_gate"})

    # JS shell: a mount point, not a document. Escalating to a render is the whole remedy.
    marker = _any(low, _SPA_MARKERS)
    if marker and tlen < 500:
        return Verdict(CASE_JS_RENDER, ACT_RENDER_JS,
                       f"SPA shell {marker!r} with only {tlen} chars of text", {"marker": marker})
    if tlen < 200 and len(html) > 500:
        return Verdict(CASE_JS_RENDER, ACT_RENDER_JS,
                       f"{len(html)} bytes of HTML yielding {tlen} chars of text")

    return Verdict(CASE_OK, ACT_STORE, f"{status or 200}, {tlen} chars of text")


def demo() -> None:
    """Runnable self-check: python -m crawler.edgecase"""
    now_y = datetime.now(timezone.utc).year
    assert diagnose("http://x/", error="getaddrinfo failed").case == CASE_DNS
    assert diagnose("http://x/", error="certificate verify failed").action == ACT_SKIP
    assert diagnose("http://x/", status=429, headers={"Retry-After": "60"}).case == CASE_RATE_LIMIT
    assert diagnose("http://x/", status=451).case == CASE_GEO_BLOCK
    assert diagnose("http://x/", redirects=10).case == CASE_REDIRECT_LOOP
    assert diagnose(f"http://x/events/{now_y + 9}-03/").case == CASE_CALENDAR_TRAP
    assert diagnose(f"http://x/products/{now_y + 9}-model").case != CASE_CALENDAR_TRAP
    assert diagnose("http://x/", status=200,
                    body="<title>Page Not Found</title><body>gone</body>").case == CASE_SOFT_404
    assert diagnose("http://x/", status=200,
                    body='<div id="root"></div>' + "<script>x</script>" * 40).case == CASE_JS_RENDER
    assert diagnose("http://x/", status=200,
                    body="<body>" + "real article text. " * 200 + "</body>").case == CASE_OK
    print("edgecase demo OK")


if __name__ == "__main__":
    demo()

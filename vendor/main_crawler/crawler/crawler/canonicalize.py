"""URL canonicalization + hashing.

Equivalent URLs must collapse to one identity so the frontier and self-dedup
(§7A) don't re-crawl the same content under cosmetically different URLs. The
canonical URL is the dedup key on every ``document``.

(Adapted from the reference crawler's canonicalize module.)
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

# Query params that never change page content — stripped during canonicalization.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "mc_cid",
    "mc_eid", "_ga", "_gl", "ref", "ref_src", "ref_url", "igshid", "yclid",
    "spm", "scm",
}
DEFAULT_PORTS = {"http": "80", "https": "443"}
# WordPress-style pager segment whose trailing slash is load-bearing — see canonicalize_url.
_PAGER_PATH_RE = re.compile(r"/pag(?:e|ed|ina)/\d+/$", re.I)

# RFC 3986 §6.2.2.1/§6.2.2.2 percent-encoding normalization, applied to the PATH only.
# An escape of an unreserved character (ALPHA / DIGIT / - . _ ~) is equivalent to the character
# itself, and the hex digits are case-insensitive — so %2E, %2e and `.` are three spellings of one
# URL. Nothing normalized them, so al-monitor.com queued /index%2Ephp/originals/... and
# /index%2ephp/originals/... as two distinct URLs on top of the plain path: 31,805 of one instance's
# 122,440 pending URLs were the same articles under a percent-encoded prefix.
# Path only, deliberately: the query already goes through the parse_qsl/urlencode round trip below,
# which has its own long-standing decode/re-encode behaviour — normalizing it twice, by two
# different rules, is how a separator a server depends on gets rewritten.
_PCT_RE = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _normalize_pct(path: str) -> str:
    def sub(m: "re.Match[str]") -> str:
        ch = chr(int(m.group(1), 16))
        return ch if ch in _UNRESERVED else "%" + m.group(1).upper()
    return _PCT_RE.sub(sub, path)


def canonicalize_url(url: str, base: str | None = None) -> str:
    """Canonical form: resolve against base, lowercase scheme/host, drop default
    port, strip tracking params, sort remaining params, drop fragment, normalize
    trailing slash, collapse a leading ``www.``.

    NEVER raises: a malformed href (bad port ``:80800``/``:abc``, bad IPv6 ``[:::1]``)
    makes ``parts.hostname``/``parts.port`` throw ValueError — one such link would
    otherwise abort a whole page's link enqueue. On any failure we fall back to the
    raw netloc (or the input url) so the caller can filter/skip it, not crash."""
    try:
        if base:
            url = urljoin(base, url)
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        # .hostname/.port can raise ValueError on a malformed authority — fall back to
        # a string-only netloc (strip credentials, lowercase) that never throws.
        try:
            host = parts.hostname.lower() if parts.hostname else ""
            port = parts.port
        except (ValueError, UnicodeError):
            netloc_raw = parts.netloc.rsplit("@", 1)[-1].lower()
            host = netloc_raw.split(":", 1)[0]
            port = None
        # ponytail: keep the host verbatim — do NOT strip www. Collapsing www→apex in
        # the FETCH url breaks sites whose apex path-redirects to the www root
        # (lntpes.com/our-offerings/x  307→  www.lntpes.com/), which silently turns every
        # deep URL into the homepage. same_site()/registered_domain() still normalize www
        # for same-domain matching, and content-hash dedup catches any true www/apex dup.
        netloc = host
        if port is not None and str(port) != DEFAULT_PORTS.get(scheme, ""):
            netloc = f"{host}:{port}"

        pairs = [(k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
                 if k.lower() not in TRACKING_PARAMS]
        pairs.sort()
        query = urlencode(pairs)

        path = _normalize_pct(parts.path or "/")
        # Strip a trailing slash ONLY on a bare path. `/foo/` and `/foo` are the same resource
        # almost everywhere (servers 301 between them), so collapsing them is a real dedup win.
        # `/search/?q=x` is not the same: with a query string the path is usually a script route,
        # and frameworks that append a slash treat the two as different URLs. Measured on
        # gutenberg.org, which serves /ebooks/search/?query=history (19,652 bytes) and answers
        # /ebooks/search?query=history with a 404 — so the canonical form we fetched was a page
        # that does not exist, and every result page of that search was lost.
        # A /page/N/ PAGER SEGMENT is the same trap without a query string: state.gov answers
        # /press-releases/page/2 with a 301 to /press-releases/ — page 1 — while
        # /press-releases/page/2/ is the real page 2, so stripping that one slash turns every link
        # in a 214-page pager into the listing root and the frontier dedups the whole chain away.
        # Cost of the carve-out: /page/2 and /page/2/ no longer collapse to one identity, so a site
        # that links both forms can fetch a pager page twice (seen on krebsonsecurity.com). The
        # content-hash dedup catches the duplicate document; a chain silently collapsed onto page 1
        # is not catchable at all.
        if (len(path) > 1 and path.endswith("/") and not query
                and not _PAGER_PATH_RE.search(path)):
            path = path.rstrip("/")
        return urlunsplit((scheme, netloc, path, query, ""))
    except Exception:
        return url                       # worst case: hand back the raw input, never raise


def registered_domain(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except (ValueError, UnicodeError):
        return ""                        # malformed authority → no registered domain
    return host[4:] if host.startswith("www.") else host


def same_site(a: str, b: str) -> bool:
    return registered_domain(a) == registered_domain(b)


# Domain brokers and parking services. An EXPIRED seed domain does not fail the way a dead site
# does — it answers 301 and lands on whichever broker now owns it, and that broker is a real,
# healthy, deeply linked marketplace. Treated as a legitimate cross-domain move, one dead seed
# (acmat.com, from the 802-source catalogue) put the whole of hugedomains.com in scope and the
# crawl walked 70 pages / 164 requests of for-sale listings for unrelated domains —
# CeliacMatters.com, TheZodiacMatch.com — into a defence corpus.
#
# Registrars sit in this list alongside the pure brokers. They are real companies, but this set is
# consulted in exactly ONE place: whether a SEED's cross-domain redirect may widen crawl scope. A
# defence manufacturer whose domain now redirects to godaddy.com is an expired registration every
# time, so there is no case to preserve.
# ponytail: a host list, because the failure is a host. If a parking service not listed here shows
# up, the general form is the content signature ("this domain is for sale" / "buy this domain"),
# which edgecase.py is the right home for.
_PARKING_DOMAINS = frozenset({
    "hugedomains.com", "sedo.com", "sedoparking.com", "afternic.com", "dan.com",
    "bodis.com", "parkingcrew.net", "undeveloped.com", "domainmarket.com",
    "buydomains.com", "squadhelp.com", "brandbucket.com", "namebright.com",
    "above.com", "voodoo.com", "epik.com", "cashparking.com", "godaddy.com",
    "namecheap.com", "porkbun.com", "uniregistry.com", "domainnamesales.com",
    "smartname.com", "fabulous.com", "internettraffic.com", "sav.com",
})


def is_parking_host(url: str) -> bool:
    """True when `url` lands on a domain broker or parking service rather than a real site.

    Matched as a domain SUFFIX, not equality: `registered_domain` only strips `www.`, and brokers
    serve parking pages from every label they own (shop.sedo.com, ns1.parkingcrew.net). The
    boundary dot matters — without it `notsedo.com` would match `sedo.com`.
    """
    host = registered_domain(url)
    return any(host == d or host.endswith("." + d) for d in _PARKING_DOMAINS)

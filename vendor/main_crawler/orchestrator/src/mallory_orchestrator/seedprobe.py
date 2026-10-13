"""Find the URL a site actually answers on, before it becomes a crawl seed.

WHY THIS EXISTS. Every seed in this catalogue was built as `https://<apex>/`, and that one shape is
wrong for a large minority of real sites. Measured on the 92 catalogued sources holding zero pages,
by probing all four shapes (https/http x apex/www):

    28 recoverable — the site was alive and the seed was simply the wrong URL
     0 with no DNS at all
    63 that resolve and still serve nothing (404, 503, 405, refused, timeout)

The 28 were not obscure. They were MBDA, NORINCO, CASIC, CSSC, CSIC, Almaz-Antey, Rosoboronexport,
Navantia, Renk, Hyundai Rotem — the most significant manufacturers in the catalogue, invisible for
months because of a URL scheme:

    mbda-systems.com  https://apex refused  ->  https://www.mbda-systems.com/   569 KB
    norinco.com                             ->  http://www.norinco.com/
    casic.com.cn                            ->  http://www.casic.com.cn/
    almaz-antey.ru                          ->  http://almaz-antey.ru/

Chinese and Russian defence sites in particular commonly serve **plain HTTP on www only**. A
hardcoded `https://<apex>` can never reach them, and the crawl reports `fetched=0` for a site that
is perfectly healthy.

So the check belongs at ENROLMENT, not as a rescue: a site should never enter the catalogue with a
seed nobody has confirmed answers.

WHAT COUNTS AS AN ANSWER. 200-399 and a body worth parsing. A 404/503/405 is a server talking, not
a site to crawl, and accepting one would hand the crawler a seed that yields exactly one error page.

DELIBERATELY CONSERVATIVE ON FAILURE. If nothing answers, this returns None and the caller keeps the
conventional `https://<apex>/`. Never guess a URL into the catalogue — a probe that runs while the
network is having a bad minute must not permanently rewrite a good seed, which is the failure that
made [[source-liveness-audit]] necessary.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import httpx

log = logging.getLogger("mallory.seedprobe")

# A browser UA, because a 403 to a bot UA is not a dead site — it is a WAF, and the crawler has
# tiers for that. Probing as a bot would write off sites the crawler can actually reach.
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_TIMEOUT = 20.0
_MIN_BYTES = 500          # a redirect stub or an empty 200 is not a page

# A DOMAIN-FOR-SALE PAGE IS NOT A SITE. When a company folds, its domain lands on a parking service
# that serves a perfectly real 3-44 KB page — comfortably over _MIN_BYTES — so every size-based test
# passes and the probe would happily re-point the catalogue at it. Measured on the one-page sites:
# nine catalogued "manufacturers" now resolve to one of these (acmat, avontech, condorsystems,
# nexaero, shumerlya, sigmaas, spectrumastro, intelligentholding, projectq).
#
# Re-pointing at one is worse than leaving the seed broken: the crawl would then succeed and fill the
# corpus with sales copy under a defence company's name.
_PARKED_HOSTS = frozenset({
    "hugedomains.com", "domainmart.com", "atom.com", "sedo.com", "sedoparking.com",
    "afternic.com", "dan.com", "buydomains.com", "undeveloped.com", "squadhelp.com",
    "brandbucket.com", "namecheap.com", "godaddy.com", "bodis.com", "parkingcrew.net",
    # Added after the first live pass caught these serving real-looking pages for dead companies:
    # sparta.com -> domaineasy, roxel.com -> a GoDaddy site-builder shell, rheintech.de -> an
    # ad-tracking redirect. The list will never be complete, which is exactly why a cross-domain
    # move is proposed for review rather than applied (see api.seeds_recheck).
    "domaineasy.com", "godaddysites.com", "afp24.net", "wixsite.com", "site123.me",
    # Found by reading what one-page sites actually stored: starkdefence.com serves
    # "Parked at Loopia", nexaero.com an Atom sales page. Each addition here came from looking at a
    # real row, which is why the list will always be behind reality -- and why a cross-domain
    # re-point is reviewed rather than trusted to a blocklist.
    "loopia.com", "loopia.se", "one.com", "hostinger.com", "namebright.com", "epik.com",
})


def _is_parked(url: str) -> bool:
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    # Suffix match, so `cc.hugedomains.com` is caught too — parking services use many subdomains.
    return any(host == p or host.endswith("." + p) for p in _PARKED_HOSTS)


def variants(domain: str) -> list[str]:
    """The four shapes, in preference order: HTTPS before HTTP, apex before www.

    Preference matters — a site reachable both ways should be crawled over TLS, and the apex is what
    the catalogue is keyed on.
    """
    d = (domain or "").strip().lower().removeprefix("www.").rstrip("/")
    if not d:
        return []
    return [f"https://{d}/", f"https://www.{d}/", f"http://{d}/", f"http://www.{d}/"]


def resolve(domain: str, timeout: float = _TIMEOUT) -> str | None:
    """The first variant that returns a real page, or None if none does.

    Redirects are followed, so the returned URL is where the site actually lives — several of the 28
    land somewhere else entirely (aerodata.de -> https://www.aerodata.de/en/).
    """
    for url in variants(domain):
        try:
            # verify=False: an expired or mismatched certificate is a fact about the site, not a
            # reason to call it dead. The crawler decides per-host whether to proceed (see
            # Job.tls_insecure_hosts); this probe only answers "is anything there".
            with httpx.Client(timeout=timeout, follow_redirects=True, verify=False) as c:
                r = c.get(url, headers=_UA)
            if 200 <= r.status_code < 400 and len(r.content) >= _MIN_BYTES:
                if _is_parked(str(r.url)):
                    return None         # the company is gone; a sales page is not a recovery
                return str(r.url)
        except Exception:                       # noqa: BLE001 — every failure is just "not this one"
            continue
    return None


def resolve_many(domains: list[str], workers: int = 6,
                 timeout: float = _TIMEOUT) -> dict[str, str | None]:
    """Probe a batch. SIX workers, not sixty.

    A previous audit of this same catalogue ran 24 concurrent probes, swamped the resolver, and
    reported 174 sites dead — 61 of which were live. The import path is not worth that risk, and at
    four shapes per host this is bounded work anyway.

    `timeout` is per shape, so the worst case for one domain is four times it. The recurring recheck
    passes a shorter one than the import does: a homepage that needs twenty seconds to answer is not
    the case that recheck exists to recover, and four shapes x twenty seconds x a batch of domains is
    how a health action turns into a long-running job.
    """
    if not domains:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return dict(zip(domains, ex.map(lambda d: resolve(d, timeout=timeout), domains)))

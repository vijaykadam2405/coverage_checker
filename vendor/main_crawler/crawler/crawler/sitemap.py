"""Fetch a site's sitemap URLs — used ONLY to cross-verify crawl coverage (crawled vs sitemap).

This is a diagnostic. It is NEVER used to seed the crawl frontier — the operator rule is "use the
sitemap to cross-check, don't depend on it for links." Best-effort and absent-safe: many gov sites
have no usable sitemap, and that just yields an empty set (coverage shows "no sitemap").
"""
from __future__ import annotations

import re

import httpx

_UA = "Mozilla/5.0 (compatible; MalloryCoverage/1.0)"
_TIMEOUT = 15.0
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
_SITEMAP_DECL = re.compile(r"(?im)^\s*sitemap:\s*(\S+)")


def norm_url(u: str) -> str:
    """Normalize for set comparison: drop scheme, leading www., fragment + query, trailing slash;
    lowercase the HOST only (hostnames are case-insensitive; paths may be case-sensitive)."""
    u = (u or "").strip().split("#", 1)[0].split("?", 1)[0]
    u = u.split("://", 1)[-1]
    if u.startswith("www."):
        u = u[4:]
    if len(u) > 1 and u.endswith("/"):
        u = u[:-1]
    if "/" in u:
        host, path = u.split("/", 1)
        return host.lower() + "/" + path
    return u.lower()


def _same_reg(url_host: str, reg: str) -> bool:
    return url_host == reg or url_host.endswith("." + reg)


def _get(client: httpx.Client, url: str) -> str:
    try:
        r = client.get(url, timeout=_TIMEOUT, headers={"User-Agent": _UA}, follow_redirects=True)
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


def _candidates(client: httpx.Client, host: str) -> list[str]:
    """robots.txt Sitemap: declarations first, then the conventional /sitemap.xml (deduped)."""
    cands = _SITEMAP_DECL.findall(_get(client, f"https://{host}/robots.txt"))
    cands.append(f"https://{host}/sitemap.xml")
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _host_variants(host: str) -> list[str]:
    """apex + www. Many sites only publish the sitemap on www, and an apex that path-redirects to
    www (lntpes.com/sitemap.xml 307→ www homepage) returns HTML, not the map — so try both."""
    h = host[4:] if host.startswith("www.") else host
    return [h, "www." + h]


def fetch_sitemap_urls(host: str, max_urls: int = 50000) -> set[str]:
    """Best-effort set of same-registered-domain page URLs from the site's sitemap(s), normalized
    (see ``norm_url``). Tries apex + www, recurses a ``<sitemapindex>`` ONE level. Empty on
    no-sitemap / any error."""
    reg = norm_url(host)                       # host without scheme/www
    urls: set[str] = set()

    def _collect(locs: list[str]) -> None:
        for u in locs:
            n = norm_url(u)
            if _same_reg(n.split("/", 1)[0], reg):
                urls.add(n)
                if len(urls) >= max_urls:
                    return

    with httpx.Client() as client:
        for h in _host_variants(host):             # apex first, then www
            for sm in _candidates(client, h):
                xml = _get(client, sm)
                if not xml or "<loc" not in xml.lower():   # a redirected homepage has no <loc>
                    continue
                locs = _LOC.findall(xml)
                if "<sitemapindex" in xml.lower():
                    for child in locs:             # index → fetch each child sitemap (one level)
                        if len(urls) >= max_urls:
                            break
                        _collect(_LOC.findall(_get(client, child)))
                else:
                    _collect(locs)
                if urls:                           # first sitemap that yielded same-host URLs wins
                    return urls
    return urls


def declared_sitemaps(host: str) -> list[str]:
    """Every sitemap the site declares, so the operator can see WHICH one the check compared against.

    This matters more than it looks. lockheedmartin.com declares 27, one per locale — en-us, en-au,
    en-gb, en-il, he-il and so on — and fetch_sitemap_urls stops at the first that yields URLs.
    That is the RIGHT default (comparing against all 27 would report tens of thousands of "missing"
    pages that are the same content in other languages, which is exactly the corpus bloat we spent a
    day deduplicating). But a check that silently compares against one of 27 without saying so is a
    check that misleads, so this makes the choice visible.
    """
    with httpx.Client() as client:
        for h in _host_variants(host):
            found = _SITEMAP_DECL.findall(_get(client, f"https://{h}/robots.txt"))
            if found:
                return found
    return []


def coverage(host: str, crawled_urls: set[str], max_urls: int = 50000) -> dict:
    """Cross-verify: compare normalized crawled URLs against the sitemap set for ``host``.

    A BARE PERCENTAGE IS NOT AN ANSWER HERE, and reporting one is how this check misleads. Measured
    across the 60 largest sites, the same "36% covered" shape came out of four unrelated situations:

      * lockheedmartin.com — 2,263 listed, 1,457 never fetched, and the missing ones are real
        capability pages. A genuine gap.
      * rtx.com — the sitemap lists 683 URLs and we hold 2,018. We have THREE TIMES the site's own
        index; "32% covered" is an indictment of the sitemap, not of the crawl.
      * leonardo.com — 3,723 listed, 2,869 held, 563 in common. Neither set is missing: the site
        serves the same pages under two URL spaces (/en/about/... and /EN/focus-detail/-/detail/...)
        and we walked the other one.
      * a site with no sitemap at all, which must never read as 0% covered.

    So `verdict` is the answer and `coverage_pct` is supporting evidence. `extra` — what we hold
    that the sitemap does not list — is what separates the second and third cases from the first,
    and leaving it out is precisely what made this number untrustworthy.
    """
    sm = fetch_sitemap_urls(host, max_urls=max_urls)
    crawled = {norm_url(u) for u in crawled_urls}
    covered = sm & crawled
    missed = sorted(sm - crawled)
    extra = len(crawled - sm)
    pct = round(100 * len(covered) / len(sm)) if sm else None

    if not sm:
        verdict = "no_sitemap"          # NOT 0% — nothing was measured
    elif pct >= 90:
        verdict = "covered"
    elif extra > len(sm):
        # We hold more than the site publishes. Its index is partial (or stale), so the shortfall
        # says nothing about the crawl.
        verdict = "sitemap_partial"
    elif extra > len(covered):
        # Both sides are large and they barely intersect: two URL spaces for one site. Needs a human
        # to say which is canonical — it is NOT a number of missing pages.
        verdict = "different_urls"
    else:
        verdict = "gaps"                # the real one: they list it, we do not have it
    return {"host": host, "sitemap_total": len(sm), "crawled_total": len(crawled),
            "covered": len(covered), "coverage_pct": pct, "extra": extra,
            "verdict": verdict, "missed": missed}


if __name__ == "__main__":   # ponytail: tiny self-check for the parse/normalize/coverage logic
    assert norm_url("https://www.example.com/a/") == "example.com/a"
    assert norm_url("http://example.com/a?b=1#c") == "example.com/a"
    assert _same_reg("blog.x.com", "x.com") and not _same_reg("notx.com", "x.com")
    cov = coverage("x.invalid", {"https://www.x.invalid/a"}, max_urls=10)  # no sitemap → empty
    # coverage_pct is None, NOT 0: nothing was measured, and a dashboard that renders those the same
    # way reports a site with no sitemap as a site we failed to crawl.
    assert cov["sitemap_total"] == 0 and cov["coverage_pct"] is None
    assert cov["verdict"] == "no_sitemap"
    print("sitemap self-check OK")

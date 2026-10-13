"""Source identity + trust classification — solved entirely at the orchestrator, no human review.

Design invariants (per product decisions):
  * The ONLY human inputs are {url, frequency, category}. Everything else is automatic.
  * TIER != RELEVANCE. Tier is a confidence weight for Layer 2 only. It NEVER gates whether a
    source is crawled (frequency does) or whether its records are ingested/ranked (L2 relevance
    does). A brand-new tier-3 blog can carry the most important signal and rank #1 on merit.
  * FAIL TO LOW TRUST. An unknown domain resolves to tier 3 (source_known=false) — never tier 1.

Precedence when resolving a URL's source:
  1. human-declared category on the catalog row (url+frequency+category the user added)
  2. built-in heuristics (gov/mil TLD, curated trade-press / think-tank / defence-org / tender lists)
  3. fallback: aggregator, tier 3, source_known=false
Dynamic re-tiering (S-28) later nudges tiers from accept-rate + dedup originality — also automatic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# ── Taxonomy: category → default trust tier (1 = authoritative … 3 = corroborate) ──
CATEGORY_TIER: dict[str, int] = {
    "gov_primary": 1,
    "manufacturer_ir": 1,
    "defence_org": 1,
    "tender_portal": 1,
    "trade_press": 2,
    "think_tank": 2,
    "business_press": 2,
    "aggregator": 3,
    # Mainstream press (BBC, Reuters, AP). Tier 2, the same as the defence trade press: reliable
    # reporting, but a secondary source - it reports what a primary said rather than saying it.
    "general": 2,
    "blog_forum_social": 3,
    "unknown": 3,
}
VALID_CATEGORIES = set(CATEGORY_TIER)

# Multi-part public suffixes we care about (extend as needed; use `tldextract` in production).
_MULTI_SUFFIXES = {
    "gov.in", "nic.in", "co.in", "org.in", "ac.in", "res.in",
    "gov.uk", "co.uk", "org.uk", "ac.uk", "mod.uk",
    "com.au", "gov.au", "org.au", "co.kr", "or.kr", "go.kr",
    "com.tr", "gov.tr", "co.il", "org.il", "gov.il", "com.sa", "gov.sa",
    # Without these, every ministry on a 3-part suffix collapses into ONE catalog row: mod.gov.my
    # and mof.gov.my both resolve to "gov.my", so the second import silently overwrites the first
    # and the whole country's procurement crawls as a single source.
    "gov.my", "com.my", "org.my", "gov.sg", "com.sg", "gov.ph", "gov.vn",
    "gov.pk", "gov.bd", "gov.lk", "go.id", "or.id", "co.id",
    "gov.eg", "gov.za", "co.za", "org.za", "gov.ae", "gov.qa", "gov.kw",
    "com.br", "gov.br", "com.mx", "gob.mx", "gov.ar", "com.ar",
    "gov.tw", "com.tw", "co.jp", "or.jp", "go.jp", "ac.jp",
    "com.cn", "gov.cn", "org.cn", "net.cn", "com.hk", "gov.hk",
    "co.nz", "govt.nz", "gov.ua", "gov.pl", "gov.gr", "gov.it",
}

# Built-in heuristic hints (NOT a human review list — compiled defaults the auto-classifier uses).
_GOV_TLDS = (".gov", ".mil", ".gov.in", ".nic.in", ".gov.uk", ".gov.au", ".go.kr", ".gov.il", ".gov.sa")
_TRADE_PRESS = {
    "janes.com", "defensenews.com", "breakingdefense.com", "shephardmedia.com",
    "defenseworld.net", "armyrecognition.com", "navalnews.com", "flightglobal.com",
}
_THINK_TANK = {"rusi.org", "iiss.org", "orfonline.org", "idsa.in", "csis.org", "sipri.org"}
_DEFENCE_ORG = {"nato.int", "nspa.nato.int", "occar.int", "eda.europa.eu", "ted.europa.eu"}
_TENDER_PORTAL = {"eprocure.gov.in", "sam.gov", "defproc.gov.in", "gem.gov.in"}
_BUSINESS_PRESS = {
    "economictimes.indiatimes.com", "business-standard.com", "livemint.com",
    "reuters.com", "bloomberg.com", "ft.com",
}
# Aggregators/blogs are the default; a few named ones help stable ids.
_AGGREGATOR = {"idrw.org", "livefistdefence.com", "raksha-anirveda.com", "defencenews.in"}


@dataclass
class SourceResolution:
    domain: str          # registrable domain (eTLD+1)
    source_id: str       # stable id, e.g. RAKSHAANIRVEDA
    category: str        # from the taxonomy
    tier: int            # 1..3, derived from category
    source_known: bool   # True if human-declared or a built-in curated match
    resolved_by: str     # human | heuristic | fallback


def registrable_domain(url: str) -> str:
    """Return eTLD+1 (drops scheme, www, and extra subdomains). Best-effort, stdlib-only."""
    host = urlsplit(url if "//" in url else f"//{url}").hostname or url
    host = host.lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last2 = ".".join(parts[-2:])
    last3 = ".".join(parts[-3:])
    if last2 in _MULTI_SUFFIXES:
        return ".".join(parts[-3:]) if len(parts) >= 3 else host
    if last3 in _MULTI_SUFFIXES:  # rare 3-part suffix
        return ".".join(parts[-4:]) if len(parts) >= 4 else host
    return last2


def subdomain_parent(url: str) -> str | None:
    """The registrable domain this URL sits UNDER, or None when the URL owns that domain.

    The catalogue is keyed by registrable domain, so asc.army.mil, atec.army.mil and army.mil are
    one row, not three. Callers have to be able to tell those apart BEFORE writing: handing
    asc.army.mil to the catalogue is not adding a source, it is aiming at a row that already
    exists. www is the apex's canonical form, not a subdomain, so it returns None.
    """
    host = (urlsplit(url if "//" in url else f"//{url}").hostname or url).lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    domain = registrable_domain(url)
    return domain if host and host != domain else None


def source_id_from_domain(domain: str) -> str:
    """Stable uppercase id from the registrable domain: raksha-anirveda.com -> RAKSHAANIRVEDA."""
    core = domain.split(".")[0]
    return re.sub(r"[^A-Z0-9]", "", core.upper()) or domain.upper()


def _heuristic_category(domain: str, host: str, watched_domains: set[str]) -> tuple[str, str]:
    """Return (category, resolved_by) for an unknown domain — fully automatic."""
    if host.endswith(_GOV_TLDS) or domain.endswith(_GOV_TLDS):
        return "gov_primary", "heuristic"
    if domain in watched_domains:
        return "manufacturer_ir", "heuristic"
    if domain in _TENDER_PORTAL:
        return "tender_portal", "heuristic"
    if domain in _DEFENCE_ORG:
        return "defence_org", "heuristic"
    if domain in _TRADE_PRESS:
        return "trade_press", "heuristic"
    if domain in _THINK_TANK:
        return "think_tank", "heuristic"
    if domain in _BUSINESS_PRESS:
        return "business_press", "heuristic"
    if domain in _AGGREGATOR:
        return "aggregator", "heuristic"
    return "unknown", "fallback"  # fail to low trust (tier 3), never tier 1


def resolve_source(
    url: str,
    *,
    human_catalog: dict[str, str] | None = None,
    watched_domains: set[str] | None = None,
) -> SourceResolution:
    """Resolve a URL to {source_id, category, tier, ...}.

    human_catalog: {registrable_domain: category} the user added (url+frequency+category).
    watched_domains: official domains of watched entities (→ manufacturer_ir).
    """
    human_catalog = human_catalog or {}
    watched_domains = watched_domains or set()
    host = (urlsplit(url if "//" in url else f"//{url}").hostname or url).lower()
    domain = registrable_domain(url)

    if domain in human_catalog and human_catalog[domain] in VALID_CATEGORIES:
        category, resolved_by, known = human_catalog[domain], "human", True
    else:
        category, resolved_by = _heuristic_category(domain, host, watched_domains)
        known = resolved_by == "heuristic"  # curated match = known; pure fallback = unknown

    return SourceResolution(
        domain=domain,
        source_id=source_id_from_domain(domain),
        category=category,
        tier=CATEGORY_TIER[category],
        source_known=known,
        resolved_by=resolved_by,
    )


if __name__ == "__main__":  # quick self-check: python sources.py
    samples = [
        ("https://raksha-anirveda.com/x/largest-ever-k9-vajra-acquisition/", {}),
        ("https://www.janes.com/defence/india/lt-k9", {}),
        ("https://mod.gov.in/tenders/mgs-155mm", {}),
        ("https://some-new-defence-blog.xyz/scoop", {}),
        ("https://www.kalyanistrategic.com/press", {"kalyanistrategic.com": "manufacturer_ir"}),
    ]
    for u, cat in samples:
        r = resolve_source(u, human_catalog=cat)
        print(f"{r.source_id:16} tier={r.tier} {r.category:16} known={r.source_known} by={r.resolved_by}  <- {u[:50]}")

    # Two ministries on the same 3-part public suffix must stay TWO sources. When they collapse the
    # failure is silent — the second import overwrites the first and a whole country crawls as one site.
    for a, b in (("https://www.mod.gov.my/en/", "https://www.mof.gov.my/"),
                 ("https://www.mindef.gov.sg/", "https://www.mha.gov.sg/"),
                 ("https://www.mod.go.jp/", "https://www.mofa.go.jp/")):
        da, db = registrable_domain(a), registrable_domain(b)
        assert da != db, f"distinct ministries collapsed onto one domain: {a} and {b} -> {da}"
        assert da.count(".") == 2, f"{a} lost its ministry label -> {da}"
    # ...while a genuine two-part domain must NOT gain a phantom label.
    assert registrable_domain("https://www.rtx.com/news") == "rtx.com"
    assert registrable_domain("https://sub.deep.example.com/") == "example.com"
    print("multi-suffix self-checks passed")

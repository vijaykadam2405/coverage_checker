"""Defence-only scoping for tender-portal crawls — auto-applied to every ``job_type="tender"``.

Aggregator portals (tender18, tenderdetail, bidassist, tenderdekho) list EVERY public tender in
India: road works, catering, furniture, pest control, reflective vests. The defence slice is well
under 1% of the listing, so a plain crawl spends its entire budget on civil works. Two levers, at
two different stages, because neither is sufficient alone:

  * ``link_relevance_keywords`` (crawl budget) — on all four portals a tender's TITLE *is* its
    anchor text, so "Tender For High Visability Reflective Vest" and "Supply Of Ammunition To
    Ordnance Factory Khamaria" are separable BEFORE a fetch is spent on the detail page. This is
    what makes a whole-portal crawl affordable.
  * ``require_keywords`` (the keep gate) — the fetched page must actually mention a defence term,
    so a vague or mis-titled listing that slipped through link relevance still cannot enter the
    corpus, and neither can the listing/pagination pages we must walk to reach the details.

**Precision over recall, deliberately.** These portals are dominated by civil works, and
``harvest._link_is_relevant`` matches SUBSTRINGS, not word boundaries — so a term also has to be
unambiguous *inside* a longer word. That rules out several obvious candidates:
``tank`` (water tank, septic tank), ``gun`` (grease gun), ``arms`` (pharmaceuticals),
``mortar`` (cement mortar), ``explosive`` (mining), ``mod``/``hal``/``bel``/``ord`` (halted,
rebellion, record). Their unambiguous long forms are here instead. A term that costs one missed
tender is cheaper than one that costs ten thousand wasted fetches.
"""
from __future__ import annotations

from urllib.parse import urlsplit

# The list is the whole feature — keep it curated, not generated. Grouped for review, flattened
# below. Every entry must survive being matched as a bare substring of a longer word.
_ARMS_OF_SERVICE = [
    "defence", "defense", "military", "paramilitary", "indian army", "indian navy",
    "indian air force", "air force", "airforce", "naval", "navy", "coast guard",
    "army ", "armed forces", "soldier", "troops", "infantry", "regiment", "battalion",
    "brigade", "squadron", "garrison", "cantonment", "sainik", "assam rifles",
    "border security force", "central reserve police", "sashastra", "crpf", "cisf", "itbp",
    "nsg ", "special forces",
]
_ORGS = [
    "ministry of defence", "ministry of defense", "ordnance", "ordinance factory",
    "drdo", "defence research", "hindustan aeronautics", "bharat electronics",
    "bharat dynamics", "bharat earth movers", "beml", "mazagon dock", "garden reach",
    "goa shipyard", "hindustan shipyard", "cochin shipyard", "mishra dhatu", "midhani",
    "dgqa", "dgaqa", "cemilac", "aeronautical development", "military engineer service",
    "naval dockyard", "military hospital", "military station", "air base", "airbase",
    "military farm", "defence estate", "defence accounts", "canteen stores department",
]
_MATERIEL = [
    "ammunition", "missile", "radar", "artillery", "howitzer", "armoured", "armored",
    "warship", "submarine", "torpedo", "frigate", "corvette", "destroyer", "aircraft carrier",
    "avionics", "aeronautic", "aerospace", "helicopter", "unmanned aerial", "uav ", "drone",
    "night vision", "ballistic", "bulletproof", "bullet proof", "camouflage", "combat",
    "weapon", "firearm", "rifle", "grenade", "warhead", "arsenal", "battle tank",
    "sonar", "electronic warfare", "surveillance system", "simulator training",
]

#: Every defence surface, deduped, order preserved. Used verbatim as both
#: ``link_relevance_keywords`` (substring match on anchor text) and ``require_keywords``
#: (FlashText word-boundary match on the fetched page).
TERMS: list[str] = list(dict.fromkeys(_ARMS_OF_SERVICE + _ORGS + _MATERIEL))

#: Tender aggregators that gate their detail pages behind a free account. ``job_type="tender"``
#: on one of these auto-enables the register/login flow (see ``crawler.siteauth``); any other
#: host is crawled anonymously, because most public procurement portals need no account.
GATED_PORTALS: frozenset[str] = frozenset({
    "tenderdetail.com", "bidassist.com", "tenderdekho.com", "tender18.com",
})


def host_of(url: str) -> str:
    """Registrable-ish host for a URL: lowercased, ``www.`` stripped. Not a PSL parse — these are
    all plain second-level domains, and a PSL dependency for four hostnames is not worth it.

    Accepts a bare hostname too ("tender18.com"), because every caller that looks up a stored
    session has one of the two and urlsplit returns hostname=None for the scheme-less form —
    which silently keyed every session under "".
    """
    s = (url or "").strip()
    if not s:
        return ""
    if "//" not in s:
        s = "//" + s
    try:
        h = (urlsplit(s).hostname or "").lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def is_gated_portal(url: str) -> bool:
    """True when this URL is on a portal whose data needs an account. Matches subdomains too
    (``www.``, ``m.``), since these portals move their listings between them."""
    h = host_of(url)
    return any(h == p or h.endswith("." + p) for p in GATED_PORTALS)

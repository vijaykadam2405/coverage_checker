"""Seed loader + alias index (§6, §11).

The crawler watches exactly the universe defined by ``docs/seed/*.json``:
  * watchlist_entities.json  — 32 competitors (+ KSSL anchor, partner nodes)
  * watchlist_products.json  — KSSL products + tracked competitors' products
  * watchlist_tech_domains.json — 8 tech domains + keyword sets
  * watchlist_tenders.json   — tender keywords + target countries + portals
  * source_registry.json     — approved sources + trust tiers + capture defaults

This module loads them once and builds the lowercase alias indexes the resolver
uses. It is pure data — no judgments, no scoring.
"""
from __future__ import annotations

import functools
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import config


# --- typed seed rows -----------------------------------------------------
@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    aliases: tuple[str, ...]
    hq: str | None = None
    dir: str | None = None        # threat | watch | fav (L2 owns the meaning)
    priority: str | None = None
    kind: str = "competitor"      # competitor | anchor | partner


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    category: str
    aliases: tuple[str, ...]
    owner_id: str | None = None   # competitor id that makes it (None = KSSL)


@dataclass(frozen=True)
class TechDomain:
    id: str
    name: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    domain: str
    tier: int
    type: str
    region: str | None = None
    # Un-gated RSS/Atom/JSON feed for the same content. When the site's HTML is
    # WAF-blocked (needs_network_path), the engine crawls this feed's item links
    # instead — feeds are rarely bot-challenged. Optional.
    feed_url: str | None = None
    # Official JSON API for the same content (e.g. DVIDS for DoD sites). Tried
    # BEFORE Wayback/feed on a WAF block — it's the freshest & fullest source.
    # DVIDS pattern: https://api.dvidshub.net/search (key via DVIDS_API_KEY env). Optional.
    api_url: str | None = None


@dataclass
class Seed:
    entities: dict[str, Entity]
    products: dict[str, Product]
    tech_domains: dict[str, TechDomain]
    sources: dict[str, Source]
    tender_keywords: tuple[str, ...]
    tender_countries: tuple[str, ...]
    tender_sources: list[dict]
    capture_defaults: dict
    # lowercase alias -> id indexes (longest-alias-first matching done in resolver)
    entity_alias_index: dict[str, str] = field(default_factory=dict)
    product_alias_index: dict[str, str] = field(default_factory=dict)

    def source_for_url(self, url: str) -> Source | None:
        """Map a URL's host to a curated registry source (longest domain suffix
        wins). Returns None when no real registry domain matches — the wildcard
        ``COMPANY_IR`` catch-all is intentionally NOT used (unknown domains are
        classified by crawler.sources, never dumped into a tier-1 bucket)."""
        host = (urlsplit(url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        best: Source | None = None
        for src in self.sources.values():
            dom = src.domain.lower()
            if dom == "*":
                continue
            if host == dom or host.endswith("." + dom):
                if best is None or len(dom) > len(best.domain):
                    best = src
        return best


def _read(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@functools.lru_cache(maxsize=1)
def load_seed(seed_dir: str | None = None) -> Seed:
    """Load and index the seed. Cached — the seed is static within a run."""
    base = Path(seed_dir) if seed_dir else config.SEED_DIR

    ents_raw = _read(base / "watchlist_entities.json")
    prods_raw = _read(base / "watchlist_products.json")
    tech_raw = _read(base / "watchlist_tech_domains.json")
    tenders_raw = _read(base / "watchlist_tenders.json")
    reg_raw = _read(base / "source_registry.json")

    entities: dict[str, Entity] = {}

    # Anchor (KSSL) — watched, but never a "competitor". L2 sends KSSL as a
    # company_event with competitor_id=KSSL; the crawler still resolves it.
    a = ents_raw["anchor"]
    entities[a["id"]] = Entity(
        id=a["id"], name=a["name"], aliases=tuple(a.get("aliases", [])),
        hq=a.get("hq"), kind="anchor",
    )
    for c in ents_raw["competitors"]:
        entities[c["id"]] = Entity(
            id=c["id"], name=c["name"], aliases=tuple(c.get("aliases", [])),
            hq=c.get("hq"), dir=c.get("dir"), priority=c.get("priority"),
            kind="competitor",
        )
    for p in ents_raw.get("partners_to_watch", []):
        # Partner nodes have no aliases in the seed; name doubles as alias.
        entities[p["id"]] = Entity(
            id=p["id"], name=p["name"], aliases=(p["name"],),
            hq=p.get("country"), kind="partner",
        )

    products: dict[str, Product] = {}
    for kp in prods_raw.get("kssl_products", []):
        products[kp["id"]] = Product(
            id=kp["id"], name=kp["name"], category=kp["category"],
            aliases=tuple(kp.get("aliases", [])), owner_id="KSSL",
        )
    # Competitor products are keyed by owner; they have no stable id in the
    # seed, so we mint a deterministic one from name.
    for owner, plist in prods_raw.get("competitor_products", {}).items():
        for cp in plist:
            pid = _mint_product_id(cp["name"])
            products[pid] = Product(
                id=pid, name=cp["name"], category=cp["category"],
                aliases=tuple(cp.get("aliases", [])), owner_id=owner,
            )

    tech_domains = {
        t["id"]: TechDomain(id=t["id"], name=t["name"], keywords=tuple(t["keywords"]))
        for t in tech_raw["domains"]
    }

    sources = {
        s["id"]: Source(
            id=s["id"], name=s["name"], domain=s["domain"], tier=s["tier"],
            type=s["type"], region=s.get("region"), feed_url=s.get("feed_url"),
            api_url=s.get("api_url"),
        )
        for s in reg_raw["sources"]
    }

    seed = Seed(
        entities=entities,
        products=products,
        tech_domains=tech_domains,
        sources=sources,
        tender_keywords=tuple(tenders_raw["keywords"]),
        tender_countries=tuple(tenders_raw["target_countries"]),
        tender_sources=list(tenders_raw["sources"]),
        # env > registry json > hard fallback. CRAWLER_USER_AGENT is the final say so an
        # operator can set an honest identifying UA without editing the seed.
        capture_defaults={**config.FALLBACK_CAPTURE_DEFAULTS,
                          **reg_raw.get("global_capture_defaults", {}),
                          **({"user_agent": os.environ["CRAWLER_USER_AGENT"]}
                             if os.environ.get("CRAWLER_USER_AGENT") else {})},
    )

    # Build lowercase alias -> id indexes. Name is always an alias of itself.
    for e in entities.values():
        for surface in (e.name, *e.aliases):
            seed.entity_alias_index.setdefault(surface.lower(), e.id)
    for p in products.values():
        for surface in (p.name, *p.aliases):
            seed.product_alias_index.setdefault(surface.lower(), p.id)

    return seed


def _mint_product_id(name: str) -> str:
    """Deterministic id for a competitor product that has no seed id."""
    import re
    base = re.sub(r"[^A-Za-z0-9]+", "", name).upper()
    return ("P_" + base)[:24] if base else "P_UNKNOWN"

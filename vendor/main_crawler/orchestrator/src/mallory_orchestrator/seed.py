"""Load the static seed (the "keywords") the job matrix is built from."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import get_settings


@dataclass
class Entity:
    id: str
    name: str
    aliases: list[str]
    priority: str  # P1|P2|P3 (anchor = P0)


@dataclass
class Seed:
    entities: dict[str, Entity] = field(default_factory=dict)  # competitors only (excl. anchor)
    products_by_owner: dict[str, list[str]] = field(default_factory=dict)
    tender_keywords: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    tech_keywords: list[str] = field(default_factory=list)


def _read(seed_dir: Path, name: str) -> dict:
    p = seed_dir / name
    return json.loads(p.read_text()) if p.exists() else {}


def load_seed(seed_dir: str | Path | None = None) -> Seed:
    d = Path(seed_dir or get_settings().seed_dir)
    ent = _read(d, "watchlist_entities.json")
    prod = _read(d, "watchlist_products.json")
    tenders = _read(d, "watchlist_tenders.json")
    tech = _read(d, "watchlist_tech_domains.json")

    entities = {
        c["id"]: Entity(c["id"], c["name"], c.get("aliases", []), c.get("priority", "P2"))
        for c in ent.get("competitors", [])
    }
    products_by_owner: dict[str, list[str]] = {}
    for owner, items in prod.get("competitor_products", {}).items():
        products_by_owner[owner] = [i["name"] for i in items]

    tech_keywords: list[str] = []
    for dom in tech.get("domains", []):
        tech_keywords += dom.get("keywords", [])[:3]

    return Seed(
        entities=entities,
        products_by_owner=products_by_owner,
        tender_keywords=tenders.get("keywords", []),
        countries=tenders.get("target_countries", []),
        tech_keywords=sorted(set(tech_keywords)),
    )


def load_source_registry(seed_dir: str | Path | None = None) -> list[dict]:
    d = Path(seed_dir or get_settings().seed_dir)
    reg = _read(d, "source_registry.json")
    return reg.get("sources", [])

"""Seed the Source Catalog from the curated registry + tender portals (a starting catalog).

Humans grow it later via the admin UI (url + frequency + category). Discovered domains auto-add.
"""

from __future__ import annotations

from ..db import SessionLocal
from ..orchestrate import upsert_source
from ..seed import _read, load_source_registry
from ..config import get_settings
from pathlib import Path

# registry `type` → our taxonomy category
_TYPE_MAP = {
    "gov_primary": "gov_primary",
    "trade_press": "trade_press",
    "business_press": "business_press",
    "news": "business_press",
    "aggregator": "aggregator",
    "blog": "blog_forum_social",
}


def main() -> None:
    with SessionLocal() as db:
        n = 0
        for s in load_source_registry():
            domain = s.get("domain", "")
            if not domain or domain == "*":  # skip the old wildcard catch-all
                continue
            category = _TYPE_MAP.get(s.get("type", ""), "aggregator")
            upsert_source(
                db, url=f"https://{domain}", frequency="daily", category=category,
                search_template=f"https://{domain}/?s={{q}}", region=s.get("region"),
                added_by="human",
            )
            n += 1

        # tender portals from the tenders watchlist
        tenders = _read(Path(get_settings().seed_dir), "watchlist_tenders.json")
        for src in tenders.get("sources", []):
            url = src.get("url")
            if not url:
                continue
            upsert_source(db, url=url, frequency="6h", category="tender_portal",
                          seed_urls=[url], added_by="human")
            n += 1
    print(f"Seeded {n} sources into the catalog.")


if __name__ == "__main__":
    main()

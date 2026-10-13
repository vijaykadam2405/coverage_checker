"""Job generator — turns the seed into crawl jobs (§7).

This mirrors *their* side of the contract (we normally receive fully-formed
jobs), but the build asked us to "create jobs from the seed to test", so this
emits a realistic ~100-target job set:

  * news  : each tracked competitor × several registry news sources
  * tender: each tender portal × the tender keyword set
  * profile: each Indian competitor's IR/press search (partnerships, geo)
  * spec  : a sample of competitor products (OEM/product pages)
  * market/tech: a few market- and technology-stream news probes

Each job is pre-scoped to ONE source + ONE entity + its keywords — precision
comes from scoping the job, not sifting a giant pile afterward (§7A).
"""
from __future__ import annotations

import json
from urllib.parse import quote_plus

from .models import Job
from .seed import Seed, load_seed

# Per-source search-URL templates ({q} = url-encoded query). Best-effort real
# search endpoints; in production the job generator owns these.
SEARCH_TEMPLATES: dict[str, str] = {
    "IDRW": "https://idrw.org/?s={q}",
    "JANES": "https://www.janes.com/search?q={q}",
    "DEFNEWS": "https://www.defensenews.com/search/?q={q}",
    "BREAKDEF": "https://breakingdefense.com/?s={q}",
    "SHEPHARD": "https://www.shephardmedia.com/search/?q={q}",
    "DEFWORLD": "https://www.defenseworld.net/?s={q}",
    "ECOTIMES": "https://economictimes.indiatimes.com/topic/{q}",
    "BSTANDARD": "https://www.business-standard.com/search?q={q}",
    "THEPRINT": "https://theprint.in/?s={q}",
    "LIVEFIST": "https://www.livefistdefence.com/?s={q}",
    "ARMYRECOG": "https://www.armyrecognition.com/?s={q}",
}
# Sources used for general competitor news sweeps (mix of tiers).
NEWS_SOURCES = ["IDRW", "DEFNEWS", "ECOTIMES", "THEPRINT", "SHEPHARD", "ARMYRECOG"]
# Guessed IR/press search endpoints for profile jobs (company sites vary; the
# crawler degrades gracefully when one 404s).
IR_GUESS = {
    "NIBE": "https://www.nibe.co.in/?s=defence",
    "SOLAR": "https://www.solargroup.com/?s=defence",
    "TATA": "https://www.tataadvancedsystems.com/?s=news",
    "ADANI": "https://www.adanidefence.com/?s=news",
    "MAHINDRA": "https://www.mahindradefence.com/?s=news",
    "BDL": "https://bdl-india.in/?s=news",
}


def _q(*aliases: str) -> str:
    return quote_plus(" ".join(aliases[:2]))


def _news_keywords(seed: Seed, entity_id: str) -> list[str]:
    e = seed.entities[entity_id]
    kws = list(dict.fromkeys([e.name, *e.aliases]))[:4]
    # add this competitor's product names (helps the gate + resolver)
    for p in seed.products.values():
        if p.owner_id == entity_id:
            kws.append(p.name)
    return kws[:8]


def _candidate_pool(seed: Seed, entity_id: str | None,
                    job_type: str = "news") -> list[str]:
    """Broad, deduped keyword pool for probe-adaptive selection: entity
    name+aliases + owned product names+aliases + the tech-domain keyword sets
    those products fall under (Product.category == TechDomain.id). The pool is
    intentionally recall-heavy — the discovery probe (scripts.check_keywords.
    discover_keywords) prunes it to just the terms present on the seed URL.

    Reuses the seed's entity/product/tech-domain alias data, so a selected
    keyword is always something the system already understands.
    tender jobs draw the tender keyword set (already broad by design)."""
    pool: list[str] = []
    if job_type == "tender":
        pool.extend(seed.tender_keywords)
    if entity_id and entity_id in seed.entities:
        e = seed.entities[entity_id]
        pool.extend([e.name, *e.aliases])
        domains: set[str] = set()
        for p in seed.products.values():
            if p.owner_id == entity_id:
                pool.extend([p.name, *p.aliases])
                if p.category:
                    domains.add(p.category)          # category slug == tech-domain id
        for dom in domains:
            td = seed.tech_domains.get(dom)
            if td:
                pool.extend(td.keywords)
    # dedupe, keep order, drop blanks
    return list(dict.fromkeys(k for k in pool if k and k.strip()))


def generate(seed: Seed | None = None, max_pages_news: int = 30) -> list[Job]:
    seed = seed or load_seed()
    jobs: list[Job] = []
    n = 0

    # --- news jobs: tracked competitor × news sources -----------------
    tracked = [eid for eid, e in seed.entities.items()
               if e.kind == "competitor" and e.priority in ("P1", "P2")]
    for eid in tracked:
        e = seed.entities[eid]
        for sid in NEWS_SOURCES:
            tpl = SEARCH_TEMPLATES.get(sid)
            if not tpl:
                continue
            n += 1
            jobs.append(Job(
                job_id=f"job_news_{eid}_{sid}_{n:03d}",
                job_type="news",
                seed_urls=[tpl.format(q=_q(e.aliases[0] if e.aliases else e.name, "defence"))],
                keywords=_news_keywords(seed, eid),
                target_entity=eid,
                max_pages=max_pages_news, max_depth=2,
                freshness_days=120,
                capture=["html", "text", "images", "screenshot"],
            ))

    # --- tender jobs: each portal × tender keywords -------------------
    # Tender portals hide the listing behind a nav dropdown (Offerings→Tenders→Archives) and list
    # every RFP as a "Download" PDF across paginated tables. So for SCRAPE sources: render + click
    # the JS nav to reach the listing, crawl deeper (depth 4), DON'T keyword-gate (a thin table page
    # must survive to the PDF-download step), and lift the per-page PDF cap so EVERY Download saves.
    for src in seed.tender_sources:
        n += 1
        is_scrape = src.get("method") == "scrape"
        jobs.append(Job(
            job_id=f"job_tender_{src['id']}_{n:03d}",
            job_type="tender",
            seed_urls=[src["url"]],
            keywords=list(seed.tender_keywords),
            keyword=not is_scrape,                 # scrape portals: capture the whole tenders section
            target_entity=None,
            max_pages=120 if is_scrape else 60,
            max_depth=4 if is_scrape else 2,       # reach the listing behind the nav
            render_js=is_scrape,
            click_discovery=is_scrape,             # click JS-onclick menu items to reach Archives
            max_pdfs_per_page=0,                   # 0 = UNLIMITED: download every RFP, no cap
            freshness_days=180,
            capture=["html", "text", "pdf", "screenshot"],
        ))

    # --- profile jobs: Indian competitor IR/press --------------------
    for eid, url in IR_GUESS.items():
        if eid not in seed.entities:
            continue
        n += 1
        jobs.append(Job(
            job_id=f"job_profile_{eid}_{n:03d}",
            job_type="profile",
            seed_urls=[url],
            keywords=_news_keywords(seed, eid) + ["partnership", "MoU", "joint venture", "export"],
            target_entity=eid,
            max_pages=25, max_depth=2,
            capture=["html", "text"],
        ))

    # --- spec jobs: a sample of competitor products ------------------
    spec_products = [(pid, p) for pid, p in seed.products.items()
                     if p.owner_id and p.owner_id != "KSSL"][:12]
    for pid, p in spec_products:
        n += 1
        jobs.append(Job(
            job_id=f"job_spec_{pid}_{n:03d}",
            job_type="spec",
            seed_urls=[SEARCH_TEMPLATES["ARMYRECOG"].format(q=_q(p.name))],
            keywords=[p.name, *p.aliases][:5],
            target_entity=p.owner_id,
            max_pages=10, max_depth=1,
            capture=["html", "text", "images", "pdf", "screenshot"],
        ))

    # --- market + technology stream probes ---------------------------
    market_probes = [
        ("Armenia artillery tender", ["Armenia", "artillery", "howitzer", "tender"]),
        ("India defence budget", ["India", "defence budget", "capital outlay", "procurement"]),
        ("Saudi Arabia howitzer", ["Saudi Arabia", "howitzer", "155mm"]),
    ]
    for label, kws in market_probes:
        n += 1
        jobs.append(Job(
            job_id=f"job_market_{n:03d}",
            job_type="news",
            seed_urls=[SEARCH_TEMPLATES["DEFNEWS"].format(q=_q(label))],
            keywords=kws, target_entity=None,
            max_pages=30, max_depth=2, freshness_days=120,
            capture=["html", "text", "screenshot"],
        ))
    tech_probes = [
        ("ramjet 155mm", ["ramjet", "155mm", "artillery", "range"], "artillery"),
        ("loitering munition", ["loitering munition", "kamikaze drone", "UAV"], "uav"),
    ]
    for label, kws, _dom in tech_probes:
        n += 1
        jobs.append(Job(
            job_id=f"job_tech_{n:03d}",
            job_type="news",
            seed_urls=[SEARCH_TEMPLATES["SHEPHARD"].format(q=_q(label))],
            keywords=kws, target_entity=None,
            max_pages=30, max_depth=2, freshness_days=180,
            capture=["html", "text", "images", "screenshot"],
        ))

    return jobs


def write_jobs(jobs: list[Job], path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([j.model_dump() for j in jobs], fh, indent=2)


def distinct_sites(jobs: list[Job]) -> int:
    from urllib.parse import urlsplit
    return len({urlsplit(u).hostname for j in jobs for u in j.seed_urls})

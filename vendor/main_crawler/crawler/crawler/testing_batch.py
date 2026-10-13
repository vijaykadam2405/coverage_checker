"""The fixed §8 testing job set — a hand-made ~12-job batch, run ONCE, to
exercise harvest + gate + asset capture end-to-end. Seed URLs point at the
shipped fixtures so the run is reproducible offline (production cadence does
NOT apply here).

Mapping (contract §8.1):
  1–3  news    L&T / Adani / KNDS(FR)  -> keyword+entity gate pass (+ non-English)
  4–5  tender  MoD India / SAM.gov     -> tender keyword gate + PDF extraction + screenshot
  6–7  profile NIBE / Solar            -> keyword+entity gate pass
  8    spec    KNDS CAESAR             -> spec table + product image
  9–10 news    Armenia / India budget  -> market-stream keyword gate pass
  11–12 news   ramjet / loitering      -> technology-stream keyword gate pass
"""
from __future__ import annotations

from .models import Job


def build() -> list[Job]:
    return [
        # 1 — news L&T
        Job(job_id="job_2026-06-29_LT_news_01", job_type="news",
            seed_urls=["https://idrw.org/lt-k9-vajra-followon/"],
            keywords=["L&T", "Larsen & Toubro", "K9 Vajra", "artillery", "howitzer"],
            target_entity="LT", max_pages=10, max_depth=1, freshness_days=120,
            capture=["html", "text", "images", "screenshot"]),
        # 2 — news Adani
        Job(job_id="job_2026-06-29_ADANI_news_02", job_type="news",
            seed_urls=["https://economictimes.indiatimes.com/news/defence/adani-acquires-general-aeronautics/"],
            keywords=["Adani", "Adani Defence", "acquisition", "UAV", "drone", "Drishti"],
            target_entity="ADANI", max_pages=10, max_depth=1, freshness_days=120,
            capture=["html", "text", "screenshot"]),
        # 3 — news KNDS (French — proves non-English handling, criterion 7)
        Job(job_id="job_2026-06-29_KNDS_news_03", job_type="news",
            seed_urls=["https://www.defensenews.com/global/2026/06/27/knds-caesar-nigeria/"],
            keywords=["KNDS", "Nexter", "CAESAR", "155", "artillery", "Nigeria"],
            target_entity="KNDS", max_pages=10, max_depth=1, freshness_days=120,
            capture=["html", "text", "screenshot"]),
        # 4 — tender MoD India (+ RFP PDF)
        Job(job_id="job_2026-06-29_MOD_tender_04", job_type="tender",
            seed_urls=["https://mod.gov.in/tenders/mgs-155mm"],
            keywords=["155mm", "mounted gun system", "RFP", "tender", "artillery", "52 calibre"],
            target_entity=None, max_pages=15, max_depth=1, freshness_days=180,
            capture=["html", "text", "pdf", "screenshot"]),
        # 5 — tender SAM.gov
        Job(job_id="job_2026-06-29_SAMGOV_tender_05", job_type="tender",
            seed_urls=["https://sam.gov/opp/155mm-towed-howitzer/view"],
            keywords=["155mm", "towed howitzer", "light field gun", "tender", "solicitation"],
            target_entity=None, max_pages=15, max_depth=1, freshness_days=180,
            capture=["html", "text", "pdf", "screenshot"]),
        # 6 — profile NIBE (partnership)
        Job(job_id="job_2026-06-29_NIBE_profile_06", job_type="profile",
            seed_urls=["https://www.nibe.co.in/news/nibe-sig-sauer-license/"],
            keywords=["NIBE", "Sig Sauer", "licen", "agreement", "rifle", "small arms", "partnership"],
            target_entity="NIBE", max_pages=10, max_depth=1,
            capture=["html", "text"]),
        # 7 — profile Solar (partnership + geo_footprint)
        Job(job_id="job_2026-06-29_SOLAR_profile_07", job_type="profile",
            seed_urls=["https://www.solargroup.com/news/nagastra-armenia-export/"],
            keywords=["Solar Industries", "Nagastra", "loitering munition", "Armenia",
                      "export", "MoU", "EDGE"],
            target_entity="SOLAR", max_pages=10, max_depth=1,
            capture=["html", "text"]),
        # 8 — spec KNDS CAESAR (table + image)
        Job(job_id="job_2026-06-29_CAESAR_spec_08", job_type="spec",
            seed_urls=["https://www.armyrecognition.com/caesar-6x6-specs"],
            keywords=["CAESAR", "155mm", "52 calibre", "howitzer", "self-propelled"],
            target_entity="KNDS", max_pages=5, max_depth=1,
            capture=["html", "text", "images", "pdf", "screenshot", "media"]),
        # 9 — market: Armenia artillery tender
        Job(job_id="job_2026-06-29_market_09", job_type="news",
            seed_urls=["https://www.defensenews.com/global/2026/06/19/armenia-artillery-tender/"],
            keywords=["Armenia", "artillery", "155mm", "howitzer", "tender"],
            target_entity=None, max_pages=10, max_depth=1, freshness_days=120,
            capture=["html", "text", "screenshot"]),
        # 10 — market: India defence budget
        Job(job_id="job_2026-06-29_market_10", job_type="news",
            seed_urls=["https://www.defensenews.com/asia-pacific/2026/06/17/india-defence-budget/"],
            keywords=["India", "defence budget", "capital outlay", "procurement", "artillery"],
            target_entity=None, max_pages=10, max_depth=1, freshness_days=120,
            capture=["html", "text", "screenshot"]),
        # 11 — tech: ramjet 155mm
        Job(job_id="job_2026-06-29_tech_11", job_type="news",
            seed_urls=["https://www.shephardmedia.com/news/ramjet-155mm-rheinmetall/"],
            keywords=["ramjet", "155mm", "Rheinmetall", "artillery", "range", "projectile"],
            target_entity=None, max_pages=10, max_depth=1, freshness_days=180,
            capture=["html", "text", "images", "screenshot"]),
        # 12 — tech: loitering munition
        Job(job_id="job_2026-06-29_tech_12", job_type="news",
            seed_urls=["https://www.shephardmedia.com/news/loitering-munition-trends/"],
            keywords=["loitering munition", "kamikaze drone", "UAV", "one-way attack", "SkyStriker", "Nagastra"],
            target_entity=None, max_pages=10, max_depth=1, freshness_days=180,
            capture=["html", "text", "images", "screenshot"]),
    ]

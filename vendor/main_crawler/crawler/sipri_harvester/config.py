"""SIPRI harvester — endpoints, dataset registry, and the storage contract.

SIPRI is NOT a crawl target. Its databases are either a JSON API or published spreadsheets, so the
page-crawler is the wrong tool: /databases/milex is Drupal prose, and the arms-transfer app is a
Svelte SPA with an empty <body>. Reverse-engineered from armstransfers.sipri.org/build/bundle.js:
the SPA reads a PUBLIC, unauthenticated API at ${API_BASE}${PUBLIC_PREFIX}. The site's own JS sends
an EMPTY Authorization header when logged out, i.e. anonymous read is the designed path, not a hole.

Transport: C3 (CamoFox) first, direct httpx as the fallback — SIPRI_USE_C3=0 to invert. The API is
POST-only, so "through C3" means running the SPA's own fetch() inside the browser via /evaluate,
not rendering a page. See fetcher.py for the corrected C3 finding (an earlier read of "SIPRI blocks
CamoFox" was wrong; a dead VPN proxy had broken CamoFox for every site).
"""
from __future__ import annotations

import os

# ── Arms Transfers: the public JSON API behind armstransfers.sipri.org ────────
API_BASE = os.environ.get("SIPRI_API_BASE", "https://atbackend.sipri.org")
PUBLIC_PREFIX = "/api/p"          # the anonymous prefix; /api/v1 is the authenticated (staff) one

TRADES_SEARCH = f"{PUBLIC_PREFIX}/trades/search"
TRADES_COUNT = f"{PUBLIC_PREFIX}/trades/search/count"
TRADES_TOTAL = f"{PUBLIC_PREFIX}/trades/getTotal"
TRADES_MAX_YEAR = f"{PUBLIC_PREFIX}/trades/getMaxYear"
COUNTRIES_ALL = f"{PUBLIC_PREFIX}/countries/getAllCountriesTrimmed"
# /armaments/search/ and /armaments/getTotal are 404 on the public prefix — staff-only (/api/v1).
# These two ARE public and carry the same catalogue content, as GET:
ARMAMENT_DESIGNATIONS = f"{PUBLIC_PREFIX}/armaments/getAllDesignations"
ARMAMENT_CATEGORIES = f"{PUBLIC_PREFIX}/typelists/getAllSubArmamentCategories"

# The API is Spring Boot: `sorts` MUST be a JSON object (Map<String,String>), not an array — an
# array returns a 500 with a Jackson deserialisation error. `filters` is a list.
def search_body(page_no: int, page_size: int) -> dict:
    return {"filters": [], "logic": "and", "pageNo": page_no, "pageSize": page_size, "sorts": {}}


# Their server is slow and we are a guest on it: big pages mean FEWER round trips, which is gentler
# than many small ones. 29,916 trades / 2000 = 15 requests.
PAGE_SIZE = int(os.environ.get("SIPRI_PAGE_SIZE", "2000"))
REQUEST_DELAY_S = float(os.environ.get("SIPRI_REQUEST_DELAY_S", "8"))
TIMEOUT_S = float(os.environ.get("SIPRI_TIMEOUT_S", "90"))
MAX_RETRIES = int(os.environ.get("SIPRI_MAX_RETRIES", "3"))

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# The API is CORS-guarded; without an Origin matching the SPA some deployments 403.
API_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Content-Type": "application/json",
    "Origin": "https://armstransfers.sipri.org",
    "Referer": "https://armstransfers.sipri.org/",
    "Authorization": "",          # exactly what the site's own JS sends when logged out
}

WWW = "https://www.sipri.org"
# The arms-transfer SPA. C3 opens a tab here so an in-page fetch() to the API carries the
# same Origin/Referer the site's own UI sends.
SPA_ORIGIN = "https://armstransfers.sipri.org/ArmsTransfer/TransferData"

# ── Published spreadsheet datasets ───────────────────────────────────────────
# Filenames carry a year/version and WILL change on the annual update, so each entry records the
# landing page it was discovered on; `discover.py` re-resolves the current href from that page
# rather than trusting these paths forever.
FILE_DATASETS = [
    {
        "key": "milex",
        "title": "SIPRI Military Expenditure Database",
        "landing": f"{WWW}/databases/milex",
        "url": f"{WWW}/sites/default/files/SIPRI-Milex-data-1949-2025_v1.2.xlsx",
        "kind": "xlsx",
    },
    {
        "key": "armsindustry_top100",
        "title": "SIPRI Arms Industry Database — Top 100",
        "landing": f"{WWW}/databases/armsindustry",
        "url": f"{WWW}/sites/default/files/SIPRI-Top-100-2002-2024%20%282%29.xlsx",
        "kind": "xlsx",
    },
    {
        "key": "armsindustry_revenues",
        "title": "SIPRI Arms Industry — Total arms revenues, Top 100",
        "landing": f"{WWW}/databases/armsindustry",
        "url": f"{WWW}/sites/default/files/Total-arms-revenues-SIPRI-Top-100-2002-2024.xlsx",
        "kind": "xlsx",
    },
    {
        "key": "financial_value_arms_exports",
        "title": "SIPRI Financial Value of Global Arms Trade",
        "landing": f"{WWW}/databases/financial-value-global-arms-trade",
        "url": f"{WWW}/sites/default/files/Financial%20Values%20of%20Arms%20Exports%201994%E2%80%932022.xlsx",
        "kind": "xlsx",
    },
]

# Landing pages whose data is browsable HTML rather than a file or API. Crawled, not parsed into
# typed tables — the raw page lands in MinIO and a row in sipri.documents.
PAGE_DATASETS = [
    {"key": "embargoes", "landing": f"{WWW}/databases/embargoes"},
    {"key": "national_reports", "landing": f"{WWW}/databases/national-reports"},
    {"key": "national_budget_archive", "landing": f"{WWW}/databases/national-budget-archive"},
    {"key": "pko", "landing": f"{WWW}/databases/pko"},
]

# ── Storage: deliberately SEPARATE from the crawler's documents table and bucket ──
# SIPRI is reference data with its own shape and refresh cadence; mixing it into `documents`
# (one row per crawled page) would force it into a schema it does not fit.
PG_SCHEMA = os.environ.get("SIPRI_PG_SCHEMA", "sipri")
MINIO_BUCKET = os.environ.get("SIPRI_MINIO_BUCKET", "mallory-sipri")

"""SIPRI harvester — pulls SIPRI's published databases into their own Postgres schema and bucket.

Not a crawler. SIPRI's databases are a public JSON API (arms transfers) plus published spreadsheets
(military expenditure, arms industry, financial value); the pages at www.sipri.org/databases are
Drupal prose about the data, not the data. Crawling them yields descriptions, not records.

Run:  python -m sipri_harvester            # everything
      python -m sipri_harvester transfers  # one section
"""

from . import config, datasets, discover, fetcher, store, transfers  # noqa: F401

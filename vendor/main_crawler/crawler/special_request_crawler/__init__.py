"""Special-request crawler — C3 (CamoFox) only.

For sites the normal C1(httpx) → C2(Wayback) → C3(CamoFox) ladder handles badly: rather than
escalating per page, every request goes straight through the stealth browser. Slower per page and
deliberately low-concurrency, so it is a separate service run on demand, not part of the main pool.
"""

from .crawl import CrawlStats, crawl_target, load_targets  # noqa: F401

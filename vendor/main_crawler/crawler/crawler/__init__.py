"""Layer 1 — KSSL defence competitive-intelligence crawler (acquisition engine).

A job-driven engine: it receives a *crawl job* (URL + keywords + budget),
harvests raw web assets, mechanically filters them down, and returns structured
records to the Ingest API. It acquires and normalizes only — it never scores,
ranks, or judges relevance-to-strategy (that is Layer 2).

See docs/01_CRAWLER_CONTRACT.md for the full contract.
"""

__version__ = "0.1.0"

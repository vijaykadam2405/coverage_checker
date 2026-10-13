"""Crawler HTTP API — the public surface Layer 2 (or a job generator) calls.

POST a crawl job (§2 input) → get back the filtered output: one document per kept
URL plus the typed records (§3, §5). Optionally also push each record to the
Ingest API (the production flow). This wraps the same pipeline the CLI uses.
"""

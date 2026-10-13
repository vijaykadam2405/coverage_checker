"""Mallory Layer 1.5 — Acquisition Orchestrator.

Owns the seed→job matrix, the Source Catalog (source_id + trust tier, fully automatic), the
scheduler (cadence = per-source frequency), and the coverage ledger (no-miss guarantee). Reads the
static seed, generates crawl jobs, dispatches them to the Layer 1 crawler API, and forwards the
returned records to the Layer 2 ingest API.
"""

__version__ = "0.1.0"

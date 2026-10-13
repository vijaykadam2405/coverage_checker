"""Stub Ingest API (Layer 2's edge) — enforces the §9 acceptance rules.

This is a *stub* standing in for the real Layer 2 ingest. It validates that the
crawler emits contract-compliant records and rejects malformed ones with
``422 {failing_rule}`` so the test harness can prove acceptance end-to-end.
"""

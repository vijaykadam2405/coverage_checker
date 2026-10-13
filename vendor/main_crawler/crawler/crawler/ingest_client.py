"""Client that POSTs one raw page bundle per kept document to the Ingest API.

Two transports:
  * ``HttpIngestClient`` — real ``POST /ingest/v1/page`` over HTTP (production).
  * ``InProcessIngestClient`` — calls the FastAPI app via Starlette's TestClient,
    so the test harness proves the full POST + acceptance path without
    binding a port.

Both send the bundle ``{"document": ...}`` (no separate "record" — L1 sends
exactly one raw page bundle per kept page) and return
``IngestOutcome(accepted, failing_rule)``.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config
from .models import Document


@dataclass
class IngestOutcome:
    accepted: bool
    failing_rule: str | None
    document_id: str | None


def _bundle(doc: Document) -> dict:
    return {"document": doc.model_dump(exclude_none=False)}


# Shared pooled httpx client for ingest POSTs — keepalive reuses the connection to the ingest API
# instead of a fresh TCP+TLS handshake per document (one POST per kept page adds up on a big crawl).
import os as _os
import random as _random
import threading as _threading
import time as _time
_INGEST_CLIENT = None
_INGEST_LOCK = _threading.Lock()

# Attempts per document for retryable failures (transport / 5xx). 3 rides out a container restart
# without turning a genuinely dead ingest into a batch that takes all night to fail.
_SEND_ATTEMPTS = max(1, int(_os.environ.get("INGEST_SEND_ATTEMPTS", "3")))


def _ingest_client():
    import httpx
    global _INGEST_CLIENT
    if _INGEST_CLIENT is None:
        with _INGEST_LOCK:
            if _INGEST_CLIENT is None:
                limits = httpx.Limits(
                    max_connections=int(_os.environ.get("CRAWLER_INGEST_MAX_CONNECTIONS", "64")),
                    max_keepalive_connections=int(_os.environ.get("CRAWLER_INGEST_KEEPALIVE", "32")))
                _INGEST_CLIENT = httpx.Client(timeout=30, limits=limits)
    return _INGEST_CLIENT


class CollectingIngestClient:
    """Captures every sent page bundle (and validates it against the ingest
    rules locally) instead of/in addition to POSTing. Used by the Crawler API
    to return documents inline. If ``forwarders`` are set, each bundle is also
    POSTed to every target Ingest API and the last outcome is authoritative."""

    def __init__(self, forwarders: "list[HttpIngestClient] | None" = None):
        self.forwarders = forwarders or []
        self.collected: list[dict] = []
        # Retain each page's FULL document (incl. html) in memory ONLY for the pull flow — a caller
        # that reads documents off the inline return (/v1/crawl with no forwarders, L2 testing).
        # When forwarding, every page is already POSTed to ingest AND persisted in Postgres, and the
        # background batch keeps only summary COUNTS, so holding the html here is pure waste:
        # measured 2026-07-23 a 47-site keyword=false crawl grew this list to ~2GB of html over 11h,
        # exhausting RAM + 8.6GB swap → thrash → throughput fell 4471→94 pages/hr. Env override
        # forces retention if some caller ever needs docs inline AND forwards.
        self._retain_docs = (not self.forwarders
                             or _os.environ.get("CRAWLER_RETAIN_INLINE_DOCS") == "1")

    def send(self, doc: Document) -> IngestOutcome:
        from ingest_api.validation import validate_page
        docd = doc.model_dump()
        ok, rule = validate_page(docd)
        local = IngestOutcome(ok, rule, doc.document_id)
        fwd_outcomes: list[dict] = []
        for fw in self.forwarders:
            try:
                o = fw.send(doc)
                fwd_outcomes.append({"url": fw.base_url, "accepted": o.accepted, "failing_rule": o.failing_rule})
            except Exception as exc:
                fwd_outcomes.append({"url": fw.base_url, "accepted": False, "failing_rule": f"transport_error:{exc}"})
        outcome = fwd_outcomes[-1] if fwd_outcomes else None
        # A page counts as accepted if ANY forwarder accepted it (or, with no forwarders,
        # local validation passed). Previously we returned `local` regardless, so batches
        # that forwarded to L2 reported accepted=0 even when L2 stored every page.
        any_accepted = (any(o["accepted"] for o in fwd_outcomes)
                        if fwd_outcomes else local.accepted)
        self.collected.append({
            "document_id": doc.document_id,
            "document": docd if self._retain_docs else None,
            "accepted": any_accepted,
            "failing_rule": outcome["failing_rule"] if (outcome and not any_accepted) else local.failing_rule,
            "forwarded_to": [o["url"] for o in fwd_outcomes if o["accepted"]],
        })
        return IngestOutcome(any_accepted, local.failing_rule, doc.document_id)


class HttpIngestClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or config.INGEST_BASE_URL).rstrip("/")

    def send(self, doc: Document) -> IngestOutcome:
        """POST one document, retrying only the failures that retrying can fix.

        This used to be a single POST whose every failure became IngestOutcome(False, ...). That was
        silent data loss: the caller has ALREADY written this page's content_hash to crawl history,
        so the next run classifies it "unchanged" and never re-sends it. A thirty-second ingest
        restart mid-batch permanently deleted every document in flight, and nothing said so.

        A 4xx is retried never: it is the store's considered verdict on THIS document (422 = failed
        a validation rule), and re-POSTing identical bytes buys an identical answer. Transport
        errors and 5xx ARE retried — those are "the store is having a moment", not "this page is bad".
        """
        url = f"{self.base_url}{config.INGEST_API_PREFIX}/page"
        bundle = _bundle(doc)
        last = "transport_error:unknown"
        for attempt in range(_SEND_ATTEMPTS):
            try:
                resp = _ingest_client().post(url, json=bundle)
            except Exception as exc:  # noqa: BLE001
                last = f"transport_error:{exc}"
            else:
                if resp.status_code == 200:
                    return IngestOutcome(True, None, resp.json().get("document_id"))
                if resp.status_code < 500:
                    try:
                        fr = resp.json().get("failing_rule")
                    except Exception:  # noqa: BLE001
                        fr = f"http_{resp.status_code}"
                    return IngestOutcome(False, fr, None)
                last = f"http_{resp.status_code}"
            if attempt + 1 < _SEND_ATTEMPTS:
                # Exponential WITH JITTER. A batch loses ingest all at once, so every finalizer
                # fails in the same instant; retrying them on an identical schedule would land the
                # whole herd on the service the moment it comes back up.
                _time.sleep((2 ** attempt) + _random.uniform(0.0, 1.0))
        return IngestOutcome(False, last, None)


def is_retryable_failure(failing_rule: str | None) -> bool:
    """Did this document fail because the STORE was unavailable, rather than because the document
    was unacceptable? Only the former justifies re-crawling the page on the next run — treating a
    validation rejection as retryable would re-fetch a permanently-invalid page forever."""
    fr = failing_rule or ""
    return fr.startswith("transport_error:") or fr.startswith("http_5")


class InProcessIngestClient:
    """Posts through the ASGI app directly (no network)."""

    def __init__(self):
        from fastapi.testclient import TestClient

        from ingest_api.app import app
        self._client = TestClient(app)

    def send(self, doc: Document) -> IngestOutcome:
        url = f"{config.INGEST_API_PREFIX}/page"
        resp = self._client.post(url, json=_bundle(doc))
        if resp.status_code == 200:
            return IngestOutcome(True, None, resp.json().get("document_id"))
        try:
            fr = resp.json().get("failing_rule")
        except Exception:  # noqa: BLE001
            fr = f"http_{resp.status_code}"
        return IngestOutcome(False, fr, None)

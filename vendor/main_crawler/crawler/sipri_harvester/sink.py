"""Adapter letting the special-request crawler write into the SIPRI schema + bucket.

special_request_crawler.crawl_target() takes any object with `.send(doc) -> outcome`, which is how
the normal path reaches the ingest API. SIPRI is supposed to live in its own Postgres schema and its
own MinIO bucket, so this sink implements the same interface and routes there instead — no fork of
the crawler, no special-casing inside it.

Returns a real IngestOutcome so the crawler's accounting keeps working unchanged (it reads
`.accepted` — the attribute a hand-rolled fake once got wrong and silently under-reported success).
"""
from __future__ import annotations

import hashlib
import logging

from crawler.ingest_client import IngestOutcome

log = logging.getLogger("sipri.sink")


class SipriSink:
    """Writes crawled pages to sipri.documents + the SIPRI bucket."""

    def __init__(self, store):
        self.store = store
        self.stored = 0
        self.failed = 0

    def send(self, doc) -> IngestOutcome:
        html = (getattr(doc, "html", "") or "").encode("utf-8", "replace")
        body = html or (getattr(doc, "main_text", "") or "").encode("utf-8", "replace")
        if not body:
            self.failed += 1
            return IngestOutcome(accepted=False, failing_rule="empty_body", document_id=None)

        sha = hashlib.sha256(body).hexdigest()
        # Content-addressed key: a re-crawl of an unchanged page overwrites itself instead of
        # accumulating a new object per run.
        key = self.store.put_blob(f"crawl/{sha[:2]}/{sha[:16]}.html", body, "text/html")
        ok = self.store.record_document(
            getattr(doc, "url", ""),
            title=getattr(doc, "title", "") or "",
            html_len=len(body),
            sha256=sha,
            minio_key=key or "",
        )
        if ok:
            self.stored += 1
            return IngestOutcome(accepted=True, failing_rule=None,
                                 document_id=getattr(doc, "document_id", None))
        self.failed += 1
        return IngestOutcome(accepted=False, failing_rule="sipri_store_write_failed",
                             document_id=None)

    def stats(self) -> dict:
        return {"stored": self.stored, "failed": self.failed}

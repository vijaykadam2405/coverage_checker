"""The PDF backfill: what it takes, what it refuses, and what it must never store.

Two of these matter more than the rest. A dead .pdf link answered with an HTML error page at HTTP
200 is routine on Drupal sites, and storing that body as a PDF puts a file in the bucket that opens
as garbage and counts as a success — the same "a number nobody measured" failure this codebase keeps
finding, one layer down. And the URLs come out of harvested HTML, which is untrusted: a page can
link 169.254.169.254 as easily as a guideline.

Runnable directly:  python tests/test_sipri_attachments.py
"""
from __future__ import annotations

import hashlib

from sipri_harvester import attachments as att

PAGE = "https://www.att-assistance.org/document/rmds-5-40"

HTML = """<html><body>
  <a href="/sites/default/files/RMDS-5.40.pdf">RMDS 5.40</a>
  <a href="https://www.wassenaar.org/app/uploads/2019/best-practice.pdf">Wassenaar best practice</a>
  <a href="handbook.pdf?download=1">relative with query</a>
  <a href="https://www.sipri.org/yearbook">a page, not a document</a>
  <a href="/files/data.xlsx">a spreadsheet, handled by discover.py</a>
  <a href="mailto:someone@example.org">mailto</a>
  <a href="javascript:void(0)">javascript</a>
</body></html>"""


_REAL_SAFE = att._safe          # captured at import, before anything stubs it


def _allow_all():
    """Storage tests use .test hostnames, which do not resolve — and an unresolvable host is a
    REFUSAL now, correctly. Those tests are about what gets WRITTEN, not about the guard, so they
    stub it.

    Stubbing a module attribute leaks into every later test in the same process, which is how the
    SSRF test first "passed" by never running the guard at all. `_real_guard()` puts it back, and
    the guard tests call it.
    """
    att._safe = lambda url: None


def _real_guard():
    att._safe = _REAL_SAFE


class _FakeStore:
    """Records what would be written, stores nothing."""

    def __init__(self):
        self.blobs: dict[str, tuple[bytes, str]] = {}
        self.rows: list[dict] = []

    def put_blob(self, key, data, content_type):
        self.blobs[key] = (data, content_type)
        return key

    def record_attachment(self, url, **kw):
        self.rows.append({"url": url, **kw})
        return True


def test_pdf_links_resolves_and_keeps_off_host_documents():
    """Off-host links are kept on purpose: a third of the ATT library lives on wassenaar.org and
    poa-iss.org. Same-domain scope is a rule about where to CRAWL, not which document a page cites."""
    got = att.pdf_links(HTML, PAGE)
    assert "https://www.att-assistance.org/sites/default/files/RMDS-5.40.pdf" in got, got
    assert "https://www.wassenaar.org/app/uploads/2019/best-practice.pdf" in got, "off-host dropped"
    assert "https://www.att-assistance.org/document/handbook.pdf?download=1" in got, got
    assert len(got) == 3, got                       # the page, the xlsx, mailto and js are not PDFs
    assert not any(u.endswith(".xlsx") for u in got), "spreadsheets belong to discover.py"
    assert not any(u.startswith(("mailto:", "javascript:")) for u in got), got


def test_an_html_error_page_served_as_a_pdf_is_never_stored():
    """THE one that matters. A 200 with an HTML body is a dead link, not a document."""
    store = _FakeStore()
    _allow_all()
    att._fetch = lambda url, timeout: (b"<!DOCTYPE html><html><head><title>404", "text/html", "ok")
    status = att._harvest_one("https://x.test/ghost.pdf", PAGE, store, 0, 5, False)
    assert status == "skipped_not_pdf", status
    assert not store.blobs, f"an HTML error page was written to the bucket: {list(store.blobs)}"
    row = store.rows[-1]
    assert row["status"] == "skipped_not_pdf"
    assert "starts with" in row["detail"], row          # the evidence, not just the verdict


def test_a_real_pdf_is_stored_content_addressed_with_the_right_type():
    store = _FakeStore()
    _allow_all()
    body = b"%PDF-1.7\n" + b"x" * 500
    att._fetch = lambda url, timeout: (body, "application/pdf", "ok")
    status = att._harvest_one("https://x.test/real.pdf", PAGE, store, 0, 5, False)
    assert status == "stored", status
    sha = hashlib.sha256(body).hexdigest()
    key = f"attachments/{sha[:2]}/{sha[:16]}.pdf"
    assert key in store.blobs, list(store.blobs)
    stored_body, ctype = store.blobs[key]
    assert stored_body == body
    assert ctype == "application/pdf", f"stored as {ctype} — the bug that made every object .html"
    row = store.rows[-1]
    assert (row["minio_key"], row["sha256"], row["status"]) == (key, sha, "stored")
    assert row["found_on"] == PAGE, "the page a document was cited from is the provenance"


def test_the_same_pdf_from_two_pages_occupies_one_object():
    """Content-addressed keys are what make a re-run cheap and the bucket honest."""
    store = _FakeStore()
    _allow_all()
    body = b"%PDF-1.4 shared"
    att._fetch = lambda url, timeout: (body, "application/pdf", "ok")
    att._harvest_one("https://a.test/one.pdf", "https://a.test/p1", store, 0, 5, False)
    att._harvest_one("https://b.test/two.pdf", "https://b.test/p2", store, 0, 5, False)
    assert len(store.blobs) == 1, store.blobs
    assert len(store.rows) == 2, "both source URLs must still be recorded"


def test_an_internal_address_is_refused_and_never_fetched():
    """Harvested HTML is untrusted input, so the guard runs on every URL before any fetch."""
    _real_guard()
    store = _FakeStore()
    called = []
    att._fetch = lambda url, timeout: called.append(url) or (b"%PDF-1.7", "application/pdf", "ok")
    status = att._harvest_one("http://169.254.169.254/latest/meta-data/x.pdf", PAGE, store, 0, 5,
                              False)
    assert status == "refused", status
    assert not called, "the SSRF target was fetched anyway"
    assert not store.blobs
    assert "metadata" in store.rows[-1]["detail"], store.rows


def test_the_guard_refuses_rather_than_passes_when_it_cannot_decide():
    """Fail-closed. The first dry-run hit exactly this: the SIPRI image ships a trimmed crawler
    package with no `edgecase`, and the run refused all 12 candidates instead of fetching them
    unguarded. A host that will not resolve gets the same treatment."""
    _real_guard()
    store = _FakeStore()
    called = []
    att._fetch = lambda url, timeout: called.append(url) or (b"%PDF-1.7", "application/pdf", "ok")
    status = att._harvest_one("https://nx.invalid/doc.pdf", PAGE, store, 0, 5, False)
    assert status == "refused", status
    assert not called, "an unresolvable host was fetched anyway"
    assert "resolve" in store.rows[-1]["detail"], store.rows


def test_a_public_host_is_allowed_through_the_guard():
    """The guard has to let real documents past, or the backfill is a no-op that looks safe."""
    _real_guard()
    assert att._safe("https://www.sipri.org/sites/default/files/x.pdf") is None
    assert att._safe("http://127.0.0.1/x.pdf") is not None, "loopback must be refused"
    assert att._safe("http://10.0.0.5/x.pdf") is not None, "private range must be refused"


def test_a_failure_is_recorded_so_a_rerun_does_not_repeat_it():
    """Without a row, every re-run re-fetches the same 404s forever and nobody can see why a
    listed document is missing."""
    store = _FakeStore()
    _allow_all()
    att._fetch = lambda url, timeout: (None, "text/html", "http_404")
    status = att._harvest_one("https://x.test/gone.pdf", PAGE, store, 0, 5, False)
    assert status == "http_404", status
    assert store.rows[-1]["status"] == "http_404", store.rows
    assert not store.blobs


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    real = att._fetch
    try:
        for fn in fns:
            fn()
            print(f"  ok  {fn.__name__}")
    finally:
        att._fetch = real
    print(f"ALL {len(fns)} SIPRI-ATTACHMENT TESTS PASSED")


if __name__ == "__main__":
    main()

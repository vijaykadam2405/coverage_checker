"""The ingest-failure data-loss path — the one bug in this crawler that destroys data silently.

The failure it guards, in order:
  1. a page is fetched fine and `_finalize` writes its content_hash to crawl history
  2. the ingest POST fails (container restart, 5xx, network blip)
  3. nothing undoes step 1
  4. the NEXT run reads the stored content_hash, classifies the page "unchanged", and skips it
  5. the document is gone forever, and the crawler is confident it already has it

Step 4 is what makes this worse than an ordinary dropped write: the loss conceals itself and there
is no counter that ever goes back up. These tests assert the two halves of the fix — retry the
failures that retrying can fix, and undo the history write for the ones it cannot.

Runnable directly:  python tests/test_ingest_loss.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import ingest_client                      # noqa: E402
from crawler.dedup import CrawlHistory, classify       # noqa: E402


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self) -> dict:
        return self._body


def _client(responses: list, calls: list):
    """A fake httpx client that replays `responses`; an Exception entry is raised, not returned."""
    class C:
        def post(self, url, json=None):
            calls.append(url)
            r = responses[min(len(calls) - 1, len(responses) - 1)]
            if isinstance(r, BaseException):
                raise r
            return r
    return C()


_REAL_SLEEP = ingest_client._time.sleep


def _patch(monkey_responses, calls):
    ingest_client._ingest_client = lambda: _client(monkey_responses, calls)   # noqa: SLF001
    # Skip the retry backoff so the suite stays fast — but see teardown_module. `_time` here is the
    # stdlib `time` MODULE, shared with every other test in the process, so leaving this installed
    # silently removed sleep() from the whole run: test_tiers' hung-probe check then returned in
    # 0ms and reported the tier UP, which reads as an engine bug rather than a leaked patch.
    ingest_client._time.sleep = lambda _s: None


def teardown_module(module=None) -> None:
    ingest_client._time.sleep = _REAL_SLEEP


def t_transport_error_is_retried_then_reported() -> None:
    calls: list = []
    _patch([ConnectionError("ingest is restarting")], calls)
    out = ingest_client.HttpIngestClient("http://ingest").send(_doc())
    assert out.accepted is False
    assert len(calls) == 3, f"a transport error must be retried, got {len(calls)} attempt(s)"
    assert out.failing_rule.startswith("transport_error:")


def t_a_blip_that_recovers_is_not_a_loss() -> None:
    """The whole point: ingest bounces, the second attempt lands, nothing is lost."""
    calls: list = []
    _patch([ConnectionError("down"), _Resp(200, {"document_id": "d1"})], calls)
    out = ingest_client.HttpIngestClient("http://ingest").send(_doc())
    assert out.accepted is True and out.document_id == "d1"
    assert len(calls) == 2


def t_validation_rejection_is_never_retried() -> None:
    """422 is the store's verdict on THIS document. Re-POSTing identical bytes buys the same answer,
    so retrying it just multiplies the load on an already-unhappy service."""
    calls: list = []
    _patch([_Resp(422, {"failing_rule": "rule1_empty_main_text"})], calls)
    out = ingest_client.HttpIngestClient("http://ingest").send(_doc())
    assert out.accepted is False and out.failing_rule == "rule1_empty_main_text"
    assert len(calls) == 1, f"a 422 was retried {len(calls)} times"


def t_server_error_is_retried() -> None:
    calls: list = []
    _patch([_Resp(503, {})], calls)
    out = ingest_client.HttpIngestClient("http://ingest").send(_doc())
    assert out.accepted is False and out.failing_rule == "http_503"
    assert len(calls) == 3


def t_retryable_classifier_splits_store_faults_from_bad_documents() -> None:
    assert ingest_client.is_retryable_failure("transport_error:boom") is True
    assert ingest_client.is_retryable_failure("http_500") is True
    assert ingest_client.is_retryable_failure("http_503") is True
    # A bad document must NOT be re-crawled forever.
    assert ingest_client.is_retryable_failure("rule1_empty_main_text") is False
    assert ingest_client.is_retryable_failure("http_422") is False
    assert ingest_client.is_retryable_failure(None) is False


def t_mark_unsent_makes_the_page_come_back_next_run() -> None:
    """THE regression test. Without mark_unsent the second classify() returns 'unchanged' and the
    document is never re-sent — silent permanent loss."""
    db = str(Path(tempfile.mkdtemp()) / "h.sqlite")
    h = CrawlHistory(db)
    url, chash = "https://example.test/a", "sha256:abc"

    # run 1: fetched, history written... then the ingest POST fails.
    h.upsert(url, content_hash=chash, etag='"v1"', last_modified="Mon, 01 Jan 2029 00:00:00 GMT",
             status=200, fetched_at="2026-07-21T00:00:00Z")
    assert classify(h.get(url), status=200, content_hash=chash) == "unchanged", \
        "precondition: without the fix the page would be skipped next run"

    h.mark_unsent(url)

    stored = h.get(url)
    assert classify(stored, status=200, content_hash=chash) != "unchanged", \
        "the page is still classified unchanged — it would never be re-sent"
    # The ETag must go too, or the next fetch earns a 304 and short-circuits the re-send anyway.
    assert not stored.etag and not stored.last_modified, \
        "a surviving validator will produce a 304 and skip the re-send"


def t_mark_unsent_keeps_the_audit_trail() -> None:
    """Clearing the content identity must not erase that we have seen this URL before."""
    db = str(Path(tempfile.mkdtemp()) / "h2.sqlite")
    h = CrawlHistory(db)
    url = "https://example.test/b"
    h.record_failure(url, status=500, category="server_error", failed_at="2026-07-21T00:00:00Z")
    h.upsert(url, content_hash="sha256:x", etag=None, last_modified=None,
             status=200, fetched_at="2026-07-21T00:01:00Z")
    h.mark_unsent(url)
    row = h.get(url)
    assert row is not None, "mark_unsent deleted the row instead of clearing the validators"
    assert row.content_hash in (None, ""), "content_hash survived"


def _doc():
    class D:
        url = "https://example.test/a"
        document_id = "d1"

        def model_dump(self, exclude_none=False):
            return {"url": self.url, "document_id": self.document_id}
    return D()


def main() -> None:
    for fn in (t_transport_error_is_retried_then_reported,
               t_a_blip_that_recovers_is_not_a_loss,
               t_validation_rejection_is_never_retried,
               t_server_error_is_retried,
               t_retryable_classifier_splits_store_faults_from_bad_documents,
               t_mark_unsent_makes_the_page_come_back_next_run,
               t_mark_unsent_keeps_the_audit_trail):
        fn()
        print(f"  ok  {fn.__name__}")
    teardown_module()          # pytest calls this for us; a standalone run has to say so itself
    print("ALL INGEST-LOSS TESTS PASSED")



def test_ingest_loss_suite() -> None:
    """pytest collects `test_*` only, and this file uses the repo's `t_*` + main() convention — so
    without this wrapper `pytest tests/` collects nothing here and reports success. A test that
    never runs is indistinguishable from one that passes."""
    main()

if __name__ == "__main__":
    main()

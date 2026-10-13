"""Collapsing same-bytes-different-url pages: what it must never take with it.

The corpus held 48,278 URL records whose exact bytes already existed under another URL of the same
site — a login form re-encoding its own address into `?back=`, a scanner's injected query strings,
a paginated encyclopedia reachable by several routes. Removing them is safe in principle: the bytes
survive under the URL that is kept.

In practice the first real run would have orphaned 264 stored PDFs. Identical HTML does NOT imply
an identical attachment list — two captures of one page can differ in which PDFs were reachable at
the time — and MinIO blobs are content-addressed and shared, so an orphaned blob is not deleted, it
is merely unreachable for ever. That is the failure this file exists to prevent, so most of it is
about what SURVIVES rather than what goes.

These tests exercise the SQL against a real Postgres and skip without one, because the whole risk
lives in the window function and the NOT EXISTS — neither of which a mock can get wrong.
"""
from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("psycopg")
from ingest_api import pgstore                                          # noqa: E402

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"),
                                reason="needs a Postgres to exercise the real SQL")


@pytest.fixture()
def corpus():
    """A throwaway source_id, so this never touches real rows even against the live corpus."""
    sid = "dedupe-test-%s.example" % uuid.uuid4().hex[:8]

    def add(url, content_hash, attachments="[]"):
        pgstore.query_strict(
            "INSERT INTO documents (document_id, url, content_hash, source_id, fetched_at,"
            " attachments) VALUES (%s,%s,%s,%s, now()::text, %s::jsonb) RETURNING 1",
            (uuid.uuid4().hex, url, content_hash, sid, attachments))

    def urls():
        rows = pgstore.query_strict(
            "SELECT url FROM documents WHERE source_id = %s ORDER BY url", (sid,))
        return [r[0] for r in rows or []]

    yield sid, add, urls
    pgstore.query_strict("DELETE FROM documents WHERE source_id = %s RETURNING 1", (sid,))


def _run(sid, apply):
    """SCOPED to this fixture's site, and that is not a convenience.

    dedupe() is corpus-wide by default and these tests run against the shared store, so an
    unscoped `apply=True` here is a production delete wearing a unit test's name. It is not
    hypothetical: the first green run of this file deduplicated the live corpus.
    """
    out = pgstore.dedupe(apply=apply, sample=200, source_id=sid)
    out["mine"] = [s for s in out.get("sample", []) if s["source_id"] == sid]
    return out


def test_the_shortest_url_in_a_group_is_the_one_kept(corpus):
    sid, add, urls = corpus
    add("https://x/contact-us/manufacturing", "sha256:aaa")
    add("https://x/contact-us/products/?Part=2694&QuestionTopic=Quote+Request", "sha256:aaa")
    add("https://x/contact-us/products/?Part=2694N&QuestionTopic=Quote+Request", "sha256:aaa")

    _run(sid, apply=True)
    assert urls() == ["https://x/contact-us/manufacturing"], "the canonical URL was not the survivor"


def test_a_page_whose_bytes_are_unique_is_never_touched(corpus):
    sid, add, urls = corpus
    add("https://x/a", "sha256:one")
    add("https://x/b", "sha256:two")
    _run(sid, apply=True)
    assert urls() == ["https://x/a", "https://x/b"]


def test_a_duplicate_holding_the_only_copy_of_a_pdf_is_KEPT(corpus):
    """THE one that matters. The blob is content-addressed and shared, so deleting this row does not
    free the PDF — it strands it: paid for, and reachable by nothing."""
    sid, add, urls = corpus
    add("https://x/short", "sha256:same")
    add("https://x/a-much-longer-url-that-would-normally-lose",
        "sha256:same", '[{"storage_path": "doc/only-here.pdf"}]')

    out = _run(sid, apply=True)
    assert set(urls()) == {"https://x/short",
                           "https://x/a-much-longer-url-that-would-normally-lose"}, \
        "a stored PDF was orphaned"
    assert out["kept_for_attachments"] >= 1


def test_a_duplicate_whose_pdf_the_survivor_also_has_IS_removed(corpus):
    """The protection must be narrow. If it kept every row with an attachment it would barely
    delete anything, and the duplicate count would never move."""
    sid, add, urls = corpus
    add("https://x/short", "sha256:same", '[{"storage_path": "doc/shared.pdf"}]')
    add("https://x/a-much-longer-duplicate-url", "sha256:same",
        '[{"storage_path": "doc/shared.pdf"}]')
    _run(sid, apply=True)
    assert urls() == ["https://x/short"]


def test_duplicates_are_grouped_per_SITE_not_globally():
    """Two sites serving the same boilerplate — a cookie notice, an empty 404 body, a shared CMS
    template — must each keep their own copy. Grouping by content_hash ALONE would delete one site's
    page because a different site happened to serve the same bytes, and the loss would be silent.

    Asserted against the SQL rather than by inserting two sites and running the pass: proving it
    functionally needs a CORPUS-WIDE apply, and a test that deletes across the whole store to check
    a grouping rule is the exact trade this file refuses to make.
    """
    rank = pgstore._DUP_RANK
    partition = rank.split("PARTITION BY", 1)[1].split(")", 1)[0]
    assert "source_id" in partition, "the dedupe groups across sites — %r" % partition
    assert "content_hash" in partition
    # The delete must also join on source_id, or a correctly-ranked row could still take another
    # site's row with it.
    assert "t.source_id = d.source_id" in (pgstore.dedupe.__doc__ or "") + _delete_sql()


def _delete_sql() -> str:
    import inspect
    return inspect.getsource(pgstore.dedupe)


def test_a_scoped_run_cannot_touch_another_site(corpus):
    """The guard that makes the tests above safe to run against a shared store."""
    sid, add, urls = corpus
    add("https://x/short", "sha256:same")
    add("https://x/longer-duplicate", "sha256:same")
    other = "dedupe-test-other-%s.example" % uuid.uuid4().hex[:6]
    pgstore.query_strict(
        "INSERT INTO documents (document_id, url, content_hash, source_id, fetched_at, attachments)"
        " VALUES (%s,%s,%s,%s, now()::text, '[]'::jsonb) RETURNING 1",
        (uuid.uuid4().hex, "https://y/a", "sha256:same", other))
    try:
        out = pgstore.dedupe(apply=True, source_id=sid)
        assert urls() == ["https://x/short"]
        assert out["sources"] == 1
        left = pgstore.query_strict("SELECT count(*) FROM documents WHERE source_id = %s", (other,))
        assert left[0][0] == 1, "a scoped run reached outside its site"
    finally:
        pgstore.query_strict("DELETE FROM documents WHERE source_id = %s RETURNING 1", (other,))


def test_the_scope_is_a_bound_parameter_not_string_interpolation():
    """This function builds SQL with `%` because its CTEs are code. A source_id spliced into that
    string would be an injection straight into a DELETE."""
    src = _delete_sql()
    assert 'scope = " AND source_id = %s"' in src, "the scope is not a placeholder"
    assert "source_id = '" not in src and "+ source_id" not in src


def test_a_dry_run_writes_nothing(corpus):
    sid, add, urls = corpus
    add("https://x/short", "sha256:same")
    add("https://x/longer-duplicate", "sha256:same")
    out = _run(sid, apply=False)
    assert out["applied"] is False
    assert len(urls()) == 2, "the dry run deleted rows"
    assert any(s["deleting"] == "https://x/longer-duplicate" for s in out["mine"])


def test_a_revision_with_unique_content_survives_its_duplicated_sibling(corpus):
    """A URL crawled twice has one row per capture. If one capture's bytes are duplicated elsewhere
    and the other's are not, only the duplicated capture may go — the URL keeps its unique one."""
    sid, add, urls = corpus
    add("https://x/page-that-changed", "sha256:dup")
    add("https://x/page-that-changed", "sha256:unique-later-version")
    add("https://x/other", "sha256:dup")            # shorter, so it wins the `dup` group

    _run(sid, apply=True)
    rows = pgstore.query_strict(
        "SELECT content_hash FROM documents WHERE source_id = %s AND url = %s",
        (sid, "https://x/page-that-changed"))
    assert [r[0] for r in rows] == ["sha256:unique-later-version"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))

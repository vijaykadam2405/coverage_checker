"""Rendered text that collapses distinct pages must not silently delete them.

LEARNED FROM SCRAPLING. Benchmarked 2026-08-18 on serviziocontrattipubblici.it (it-anac):
Scrapling recorded 13 pages, we recorded 1. The instinct is "it reached pages we could not" —
the crawl stats say otherwise: we FETCHED 9 and kept 1, dropping 8 as skipped_duplicate. The
pages were fetched, extracted, hashed, found equal, and thrown away.

They are not equal. Extracted from raw HTML the same three URLs give 1943 / 1036 / 786 chars
and three different hashes. When interactions run we extract from the rendered inner_text
instead, and on that portal the rendered text is the shared shell.

Scrapling has no cleverer fetch here. Its get_all_text() simply reads the whole DOM, and it has
no content-hash dedup to collapse the result. The dedup is worth keeping — it is what stops the
corpus filling with the same page under twenty URLs — so the fix is to re-extract from the
markup before dropping, not to stop deduping.

    python -m pytest tests/test_static_text_recovery.py
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crawler import extract  # noqa: E402


def test_build_document_accepts_static_text():
    """The recovery path needs a way to ask for the raw-HTML extraction explicitly."""
    sig = inspect.signature(extract.build_document)
    assert "static_text" in sig.parameters
    assert sig.parameters["static_text"].default is False, \
        "static_text must be OPT-IN: defaulting it on would discard dynamically-revealed " \
        "content everywhere, which is the reason the inner_text path exists"


def test_static_text_bypasses_inner_text():
    """With static_text on, a rendered inner_text must not win over the markup."""
    src = inspect.getsource(extract.build_document)
    assert "if res.inner_text and not static_text:" in src, \
        "static_text no longer bypasses the rendered-text branch"


def test_duplicate_path_retries_before_dropping():
    """The engine must attempt recovery, and must still drop a genuine duplicate.

    Asserted against the source: the surrounding call needs a live browser tab and a job
    context. If the retry is ever removed, or made to keep the page unconditionally, this
    fails — the second is as bad as the first, because then every real duplicate is stored.
    """
    eng = (Path(__file__).resolve().parents[1] / "crawler" / "async_engine.py").read_text(
        encoding="utf-8")
    i = eng.index("A DUPLICATE HASH IS NOT ALWAYS A DUPLICATE PAGE")
    block = eng[i:i + 1800]
    assert "static_text=True" in block, "no raw-HTML retry on the duplicate path"
    assert "alt.content_hash not in ctx.seen_hashes" in block, \
        "the retry must only keep text that is genuinely new"
    assert "ctx.skipped_duplicate += 1" in block, \
        "a genuine duplicate must still be dropped and counted"


def test_recovery_exists_on_BOTH_duplicate_paths():
    """There are two places a rendered doc can be dropped as a duplicate, and the first fix
    only covered one of them.

    Measured: after patching the C3 path alone, it-anac still reported fetched=9 kept=1
    skipped_duplicate=8 and the recovery log never fired — because that job runs on the MAIN
    render path, which drops in _finalize. A fix on the path the traffic does not take is not
    a fix, and nothing in the test suite said so.
    """
    eng = (Path(__file__).resolve().parents[1] / "crawler" / "async_engine.py").read_text(
        encoding="utf-8")
    assert eng.count("static_text=True") >= 2,         "only one duplicate path recovers; the other still deletes fetched pages"
    # and every drop site must still be able to drop
    assert eng.count("ctx.skipped_duplicate += 1") >= 2

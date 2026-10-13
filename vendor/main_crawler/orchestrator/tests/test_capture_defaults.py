"""PDF capture must not depend on which category name the operator typed.

The same 802 manufacturer sources produced two different capture lists. Dispatched as
`manufacture` they carried html+text+pdf, because the UI bucket default supplied it. Dispatched as
`all` they fell back to jobgen._CAPTURE["profile"] — html+text — and collected no brochures,
datasheets or spec sheets at all.

Measured when the live-sites panel first put a PDF column beside a page count: 97 of 149 sites held
PDFs (bhel.com 6,410, bel-india.in 4,302) and the seven crawling at that moment held zero. Nothing
errored; the column was simply 0 forever, and on a defence-manufacturer corpus the PDFs are the
part worth having.

Runnable directly:  python tests/test_capture_defaults.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator import jobgen  # noqa: E402


def test_every_job_type_captures_html_text_and_pdf():
    """The floor. Extras differ per type; these three do not."""
    for jt, kinds in jobgen._CAPTURE.items():
        for need in ("html", "text", "pdf"):
            assert need in kinds, f"job_type {jt!r} does not capture {need}: {kinds}"


def test_profile_is_the_one_that_regressed():
    """Named explicitly because it is the type EVERY manufacturer source generates, and it is the
    one that was html+text only. If a future edit trims it again, this is the line that says why."""
    assert "pdf" in jobgen._CAPTURE["profile"], (
        "profile lost pdf again — every manufacturer source is a profile job, so this silently "
        "stops collecting brochures and spec sheets across the whole catalogue")


def test_no_job_type_is_missing_from_the_table():
    """A job type absent here falls back to a bare ['html','text'] at the call site, which is
    exactly the hole this file exists to close."""
    for jt in ("news", "tender", "spec", "profile"):
        assert jt in jobgen._CAPTURE, f"{jt} missing from _CAPTURE — it would default to html+text"


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} CAPTURE-DEFAULT TESTS PASSED")


if __name__ == "__main__":
    main()

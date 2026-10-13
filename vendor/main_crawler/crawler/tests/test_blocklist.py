"""The operator blocklist — what makes a deletion stick.

nordkurier.de and deki.dk were each purged from Postgres, MinIO, the schedules and all twelve durable
frontiers, and both came back (16 and 7 pages; one first seen six hours AFTER the purge). They are in
no catalogue, no shard table, no schedule and no job definition — they arrive as children of a
catalogued page or through a cross-domain redirect. So the guard has to sit on the enqueue path, and
these tests are mostly about the two ways it could be wrong: letting a subdomain through, or blocking
something legitimate.

Runnable directly:  python tests/test_blocklist.py
"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _fresh(lines: str | None = None):
    """A blocklist module bound to a throwaway data dir, optionally pre-seeded."""
    d = tempfile.mkdtemp(prefix="bl_")
    os.environ["CRAWLER_DATA_DIR"] = d
    from crawler import config
    importlib.reload(config)
    from crawler import blocklist
    importlib.reload(blocklist)
    if lines is not None:
        Path(d, blocklist.FILENAME).write_text(lines, encoding="utf-8")
    return blocklist, d


def test_a_listed_host_is_blocked():
    bl, _ = _fresh("nordkurier.de\ndeki.dk\n")
    assert bl.blocked("https://www.nordkurier.de/politik/x") == "nordkurier.de"
    assert bl.blocked("https://deki.dk/shop/glas") == "deki.dk"


def test_subdomains_are_covered():
    """The whole point is that a site cannot return wearing a different hostname."""
    bl, _ = _fresh("deki.dk\n")
    assert bl.blocked("https://shop.deki.dk/x") == "deki.dk"
    assert bl.blocked("https://a.b.deki.dk/x") == "deki.dk"


def test_a_lookalike_domain_is_NOT_blocked():
    """`deki.dk` must not block `notdeki.dk` or `deki.dk.example.com` — over-blocking silently
    removes real sites from the corpus, which is harder to notice than under-blocking."""
    bl, _ = _fresh("deki.dk\n")
    assert bl.blocked("https://notdeki.dk/x") is None
    assert bl.blocked("https://deki.dk.example.com/x") is None
    assert bl.blocked("https://mydeki.dk/x") is None


def test_everything_else_is_untouched():
    bl, _ = _fresh("nordkurier.de\n")
    for u in ("https://www.airbus.com/en/products", "https://sigsauer.com/",
              "https://www.rohde-schwarz.com/x", "https://nordkurier.example.org/"):
        assert bl.blocked(u) is None, u


def test_no_file_means_nothing_is_blocked():
    """The normal case for every deployment that has never purged a site."""
    bl, _ = _fresh(None)
    assert bl.blocked("https://anything.com/") is None


def test_comments_and_blank_lines_are_ignored():
    bl, _ = _fresh("# a comment\n\n  deki.dk  # inline reason\n\n#nordkurier.de\n")
    assert bl.blocked("https://deki.dk/") == "deki.dk"
    assert bl.blocked("https://nordkurier.de/") is None, "a commented-out host must not block"


def test_www_and_trailing_dot_are_normalised():
    bl, _ = _fresh("www.deki.dk\n")
    assert bl.blocked("https://deki.dk/") == "deki.dk"
    assert bl.blocked_host("deki.dk.") == "deki.dk"


def test_add_appends_and_takes_effect_immediately():
    """The purge calls this, so it has to be live without a restart."""
    bl, d = _fresh("")
    assert bl.blocked("https://deki.dk/") is None
    assert bl.add(["deki.dk", "nordkurier.de"], reason="not defence") == 2
    assert bl.blocked("https://deki.dk/") == "deki.dk"
    assert bl.blocked("https://www.nordkurier.de/x") == "nordkurier.de"


def test_add_is_idempotent_and_skips_covered_hosts():
    bl, _ = _fresh("deki.dk\n")
    assert bl.add(["deki.dk"]) == 0, "already listed"
    assert bl.add(["shop.deki.dk"]) == 0, "already covered by its parent domain"
    assert bl.add(["other.com"]) == 1


def test_an_edit_on_a_live_crawler_is_picked_up():
    """Re-read on mtime change — the file is meant to be editable without a rebuild or a restart."""
    import time
    bl, d = _fresh("")
    assert bl.blocked("https://late.com/") is None
    p = Path(d, bl.FILENAME)
    time.sleep(0.01)
    p.write_text("late.com\n", encoding="utf-8")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 1))   # force a distinct mtime
    assert bl.blocked("https://late.com/") == "late.com"


def test_a_malformed_url_does_not_raise():
    """This runs on every discovered link; it must never be the thing that kills a crawl."""
    bl, _ = _fresh("deki.dk\n")
    for u in ("", "not a url", "http://", "///", "javascript:void(0)"):
        assert bl.blocked(u) is None, u


def test_add_accepts_a_bare_string():
    """add("kia.com") used to iterate the CHARACTERS and write one entry per letter.

    It fails silently in both directions: single-character entries can never match a real host, so
    nothing is over-blocked AND nothing is blocked, while the return value still looks like a
    success. 27 junk lines went into all twelve live instances before it was caught.
    """
    bl, d = _fresh("")
    assert bl.add("kia.com") == 1
    assert bl.blocked("https://www.kia.com/hu/pbv") == "kia.com"
    written = [ln.strip() for ln in Path(d, bl.FILENAME).read_text(encoding="utf-8").splitlines()
               if ln.strip() and not ln.startswith("#")]
    assert written == ["kia.com"], written
    assert not [h for h in bl._load() if "." not in h], "a single-character entry was written"


def test_the_engine_actually_consults_it():
    """Guards against the fix being present but unwired — the failure mode that made the locale trap
    and the camofox relaunch look fixed when they were not."""
    src = (Path(__file__).resolve().parents[1] / "crawler" / "async_engine.py").read_text(encoding="utf-8")
    assert "blocklist" in src.split("\n")[32] or "blocklist," in src, "blocklist not imported"
    assert "blocklist.blocked(cl)" in src, "the enqueue path does not check the blocklist"
    assert "blocklist.blocked(cu)" in src, "the SEED path does not check the blocklist"
    # The FETCH path too. Guarding only the enqueue leaves everything already past that gate — the
    # in-memory queues, the render lane, retry timers — still fetching and STORING a blocked host,
    # so a purge cannot converge while the crawl runs (measured: ~16 rows per 20s after 80,289 were
    # deleted). _process is where a URL becomes a fetch, so that is where blocking has to bite.
    proc = src.split("async def _process(")[1][:2000]
    assert "blocklist.blocked(url)" in proc, "the FETCH path (_process) does not check the blocklist"
    assert "errors.BLOCKLISTED" in proc, "a blocked fetch is not counted as a skip"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} BLOCKLIST TESTS PASSED")

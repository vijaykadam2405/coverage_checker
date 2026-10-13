"""The relative-link path-accumulation trap that ate 71% of a batch.

cyientdlm.com serves menu links WITHOUT a leading slash (`href="capabilities"`). Resolved against
/about-us/ that becomes /about-us/capabilities, which serves the same menu, which becomes
/about-us/capabilities/industries, forever — every hop a 200, every hop a soft-404 shell.

Measured on the 2026-07-28 batch, from the durable frontier after the crawl was stopped:

    cat_all_cyientdlm    16,717 pages_done   of a 23,664-page batch total   (71%)
    16,601 frontier rows for that one job; 98% carried a repeated path segment
    path depth 8 = 5,476 rows, depth 9 = 5,397 rows
    639 rows had a scheme (`https:`) sitting in the path
    the guards that shipped caught 0 of 16,601

It slipped both existing checks by a hair: no segment reached 3 repeats (`about-us` and
`about-us.html` counted as different strings) and no path reached the 12-segment cap.

Scored against the same batch's other 8,887 real frontier URLs: 0 false positives.

Runnable directly:  python tests/test_path_accumulation_trap.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler.errors import TRAP, looks_like_trap  # noqa: E402

# Verbatim from the frontier of the stopped batch.
REAL_TRAP = [
    "https://www.cyientdlm.com/about-us/about-us/capabilities/capabilities/about-us.html",
    "https://www.cyientdlm.com/about-us/about-us/capabilities/capabilities/industries/about-us.html",
    "https://www.cyientdlm.com/about-us/about-us/capabilities/capabilities/industries/aerospace-and-defense.html",
    "https://www.cyientdlm.com/about-us/about-us/capabilities/capabilities/contact-us.html",
    "https://www.cyientdlm.com/about-us/about-us/capabilities/capabilities/industries/industries/prlisting/prlisting/about-us.html",
]
REAL_SCHEME_IN_PATH = [
    "https://www.cyientdlm.com/https:/careers.cyientdlm.com/jobs/Careers",
    "https://www.cyientdlm.com/https:/cyientdlm.com",
    "https://www.cyientdlm.com/https:/www.cyient.com/about-us/diversity-and-inclusion",
]
# Also verbatim from the same batch — these must keep crawling.
REAL_OK = [
    "https://cyientdlm.com/",
    "https://www.cyientdlm.com/about-us",
    "https://www.cyientdlm.com/about-us.html",
    "https://www.cyientdlm.com/about-us/Award.html",
    "https://www.bhel.com/product-services/power-generation/thermal",
    "https://www.chemring.com/investors/results-and-reports/annual-report-2024.pdf",
    "https://www.babcockinternational.com/news/babcock-awarded-contract/",
]


def test_the_real_trap_urls_are_refused():
    for u in REAL_TRAP:
        assert looks_like_trap(u) == TRAP, f"still followed: {u}"


def test_a_scheme_in_the_path_is_refused():
    """`/https:/host/path` is a malformed href we resolved relative to the current page. It is
    never a real resource, and 639 of them were fetched."""
    for u in REAL_SCHEME_IN_PATH:
        assert looks_like_trap(u) == TRAP, f"still followed: {u}"


def test_real_pages_from_the_same_batch_still_crawl():
    for u in REAL_OK:
        assert looks_like_trap(u) is None, f"false positive, this is a real page: {u}"


def test_extension_no_longer_hides_a_repeat():
    """The exact miss. /a/a/a.html is three visits to `a`; treating `.html` as a different segment
    is what let the loop check read it as 2+1 and pass."""
    assert looks_like_trap("https://x.com/about-us/about-us/about-us.html") == TRAP
    assert looks_like_trap("https://x.com/a/a/a") == TRAP        # unchanged behaviour


def test_one_repeated_segment_is_still_allowed():
    """The discriminator is TWO segments repeating. A single repeat is ordinary
    (/download/download.pdf, /news/news) and refusing it would cost real pages."""
    assert looks_like_trap("https://x.com/download/download.pdf") is None
    assert looks_like_trap("https://x.com/news/news.html") is None
    assert looks_like_trap("https://x.com/en/products/en-route") is None


def test_two_repeated_segments_is_the_trap_signature():
    assert looks_like_trap("https://x.com/a/b/a/b") == TRAP
    assert looks_like_trap("https://x.com/about/team/about/team/contact") == TRAP


def test_existing_guards_still_hold():
    """Regression fence: the new checks must not have displaced the old ones."""
    assert looks_like_trap("https://x.com/" + "a/" * 13) == TRAP          # >12 segments
    assert looks_like_trap("https://x.com/x?" + "q" * 2100) == TRAP       # length cap
    assert looks_like_trap("https://x.com/forum/page-300") == TRAP        # deep pager
    assert looks_like_trap("https://x.com/forum/page-2") is None          # real pagination
    assert looks_like_trap("https://x.com/") is None


def test_a_saved_frontier_is_re_filtered_on_resume():
    """A guard added today must reach a frontier saved yesterday.

    _resume_job re-queues pending rows straight off disk. Without a re-check, every guard we add is
    retroactively defeated by any crawl that stopped before it shipped — and this one stopped with
    316 pending cyientdlm URLs that the guard now refuses, all of which would have been fetched
    again the moment Resume was pressed. Asserted against the source because the reload path needs
    a live engine, and the property that matters is simply that the check is on that path."""
    src = (Path(__file__).resolve().parents[1] / "crawler" / "async_engine.py").read_text(
        encoding="utf-8")
    body = src[src.index("Reload a job's frontier from sqlite"):]
    body = body[:body.index("return len(pending)")]
    assert "errors.looks_like_trap(url)" in body, (
        "resume re-queues saved URLs without re-applying the trap guard")
    assert "ctx.seen.add(url)" in body, (
        "a dropped URL must also be marked seen, or a link on any page re-enqueues it")


# ── The same trap with only ONE repeated segment (godrejenterprises.com, 2026-07-30) ─────────────
# The two-segments-repeating rule needs a menu to revisit TWO of its own entries. This one revisits
# exactly "about-us" while appending new segments, so it grew to 11-12 segments untouched and
# reached 24,244 pending URLs — 78% of that instance's entire frontier, at 0.0 pages/s because each
# one burned five renders and then gave up host_busy.
GODREJ_TRAP = [
    "https://www.godrejenterprises.com/contact-us/about-us/good-&-green/newsroom/about-us"
    "/locks-and-security/legal/intralogistics/material-handling-equipment"
    "/godrej-locking-solutions-and-systems/support",
    "https://www.godrejenterprises.com/contact-us/about-us/good-&-green/about-us/careers"
    "/intralogistics/material-handling-equipment/godrej-locking-solutions-and-systems/security",
    "https://www.godrejenterprises.com/contact-us/about-us/good-&-green/newsroom/about-us"
    "/policies-and-reports/locks-and-security/legal/intralogistics/material-handling-equipment"
    "/godrej-locking-solutions-and-systems/furniture",
]

# Real URLs sitting in the SAME frontiers, kept deliberately. The deepest legitimate path found
# anywhere across four live instances was SEVEN segments, which is why the bound is eight.
REAL_DEEP = [
    "https://www.wartsila.com/media/events/2026/10/31/marine/interferry-conference",
    "https://automation.honeywell.com/us/en/secure/services-and-support/support/downloads/software",
    "https://www.gdbiw.com/events/category/hiring-events/career-fairs/list",
    "https://www.larsentoubro.com/pressreleases/2026/2026-05-20-larsen-toubro-wins-orders",
    # A repeated segment on its own is NOT a trap — these are ordinary and must stay crawlable.
    "https://example.com/download/download",
    "https://example.com/news/news.html",
    "https://example.com/products/products/overview",
]


def test_a_single_repeat_deep_in_the_path_is_a_trap():
    for u in GODREJ_TRAP:
        assert looks_like_trap(u) == TRAP, u


def test_the_shipped_two_repeat_rule_did_NOT_catch_these():
    """Proves the new rule is doing the work, not the old one — otherwise this test passes for the
    wrong reason and would keep passing if the new rule were deleted."""
    from urllib.parse import urlsplit

    from crawler.errors import _norm_seg
    for u in GODREJ_TRAP:
        segs = [_norm_seg(s) for s in urlsplit(u).path.split("/") if s]
        counts = {}
        for s in segs:
            counts[s] = counts.get(s, 0) + 1
        assert max(counts.values()) < 3, u                       # no 3x loop
        assert sum(1 for v in counts.values() if v >= 2) < 2, u  # no two-segments rule
        assert len(segs) <= 12, u                                # under the depth cap


def test_a_shallow_repeat_is_still_allowed():
    for u in REAL_DEEP:
        assert looks_like_trap(u) is None, u


def test_the_depth_bound_sits_above_the_real_data():
    """Seven segments with a repeat is kept, eight is refused. The boundary is where it is because
    the deepest real URL measured across four live frontiers was seven."""
    seven = "https://example.com/a/b/c/d/e/f/a"
    eight = "https://example.com/a/b/c/d/e/f/g/a"
    assert len([s for s in seven.split("/") if s][2:]) == 7
    assert looks_like_trap(seven) is None
    assert looks_like_trap(eight) == TRAP


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"ALL {len(fns)} PATH-ACCUMULATION-TRAP TESTS PASSED")


if __name__ == "__main__":
    main()

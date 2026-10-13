"""The error families the Logs tab groups by, and the two ways that grouping can lie.

A grouping is only useful if every member of a family takes the SAME action, so the tests below are
mostly about the boundaries: 403 and 404 are both 4xx and belong in different families; 429 is a
refusal you retry and 403 is one you do not; `host_busy` is a URL we declined AND one worth another
go, and filing it as unrecoverable is the exact mistake that buried 617 live bel-india pages.

The vocabulary is duplicated from `crawler/errors.py` because the orchestrator does not import the
crawler package — so the last test pins the two lists together. Two vocabularies that can drift
silently need something that fails when they do.

Runnable directly:  python tests/test_errorclass.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mallory_orchestrator import errorclass as ec  # noqa: E402


def test_every_family_is_a_different_action():
    """The point of the grouping. If two of these collapsed, the tab would be telling an operator
    that "the site is blocking us" and "the page does not exist" are the same problem."""
    cases = {
        "needs_network_path": "blocked",   # the site refused this egress
        "needs_captcha_solver": "blocked",
        "rate_limited": "blocked",
        "auth_wall": "wall", "paywall": "wall", "consent_wall": "wall", "geo_block": "wall",
        "skipped_gone": "missing",
        "dns": "network", "conn_refused": "network", "timeout": "network", "ssl": "network",
        "origin_ssl": "network",
        "needs_render": "render", "render_timeout": "render",
        "blocked_by_robots": "skipped", "trap": "skipped", "too_large": "skipped",
        "host_busy": "skipped", "skipped_off_peak": "skipped",
        "other": "unknown",
    }
    for reason, want in cases.items():
        got, _ = ec.classify(reason)
        assert got == want, f"{reason} -> {got}, expected {want}"


def test_http_codes_split_where_the_action_splits():
    """`http_<n>` carries its meaning in the number, and 4xx is not one thing."""
    assert ec.classify("http_403") == ("blocked", False), "403 retried is 403 again"
    assert ec.classify("http_401") == ("blocked", False)
    assert ec.classify("http_429") == ("blocked", True), "429 is the one 4xx worth retrying"
    assert ec.classify("http_451") == ("wall", False), "withheld by law, not a fault"
    assert ec.classify("http_404") == ("missing", False)
    assert ec.classify("http_410") == ("missing", False)
    assert ec.classify("http_503") == ("server", True), "their fault, usually transient"
    assert ec.classify("http_500") == ("server", True)
    assert ec.classify("http_418") == ("blocked", False)


def test_recoverable_is_not_the_same_question_as_the_family():
    """host_busy is the case that proves they are independent: the URL was never fetched because the
    host's slots stayed full, so it is BOTH 'we declined it' and 'try again'. Reading the family as
    the answer to both is how 617 live URLs were reported unrecoverable."""
    fam, rec = ec.classify("host_busy")
    assert (fam, rec) == ("skipped", True), (fam, rec)
    assert ec.classify("blocked_by_robots") == ("skipped", False), "robots is our own permanent no"
    assert ec.classify("consent_wall")[1] is True, "a stealth render clears a CMP"
    assert ec.classify("auth_wall")[1] is False, "an account opens a login, not a retry"


def test_an_unknown_reason_is_counted_not_dropped():
    """A reason invented next year must land somewhere visible. A grouping that silently swallows
    what it does not recognise reports a cleaner picture than the one it measured."""
    assert ec.classify("some_future_reason") == ("unknown", False)
    assert ec.classify(None) == ("unknown", False)
    assert ec.classify("") == ("unknown", False)
    rows = [{"reason": "brand_new"}, {"reason": "http_404"}]
    out = ec.summarise(rows)
    assert sum(f["count"] for f in out) == len(rows), "a row went missing in the summary"
    assert any(f["family"] == "unknown" and f["count"] == 1 for f in out), out


def test_summarise_keeps_the_raw_codes_and_the_order():
    rows = ([{"reason": "http_403"}] * 3 + [{"reason": "needs_network_path"}]
            + [{"reason": "dns"}] * 2 + [{"reason": "skipped_gone"}])
    out = ec.summarise(rows)
    assert [f["family"] for f in out] == ["blocked", "network", "missing"], out
    blocked = out[0]
    assert blocked["count"] == 4
    assert blocked["reasons"] == {"http_403": 3, "needs_network_path": 1}
    assert blocked["recoverable"] == 1, "only needs_network_path is worth retrying here"
    assert blocked["label"] == "Blocked by the site"
    assert all(f["family"] in ec.FAMILY_ORDER for f in out)


def test_the_vocabulary_matches_the_crawlers_own():
    """The table is a literal because the orchestrator cannot import the crawler package. That is
    exactly why it needs pinning: a reason the crawler emits and this file has never heard of would
    silently pile up under 'unknown', which looks like a working grouping."""
    root = Path(__file__).resolve().parents[2]
    src = next((root / d / "crawler" / "errors.py" for d in ("crawler", "cralwer")
                if (root / d / "crawler" / "errors.py").exists()), None)
    if src is None:
        print("  -- crawler/errors.py not beside us, vocabulary pin skipped")
        return
    text = src.read_text(encoding="utf-8")
    # Module-level reason constants: NAME = "value" at column 0.
    emitted = {m.group(2) for m in re.finditer(r'^([A-Z_]+) = "([a-z0-9_]+)"$', text, re.M)}
    # ...and the anti-bot VENDOR names, which reach the log as reasons in their own right and do not
    # live in errors.py at all. The first real crawl through this grouping surfaced
    # `cloudflare_interactive` and it landed in "unclassified" — pinning only errors.py would have
    # let every future vendor do the same, silently.
    cap = src.parent / "captcha.py"
    if cap.exists():
        sig = re.search(r"_SIGNATURES[^=]*=\s*\{(.*?)\n\}", cap.read_text(encoding="utf-8"), re.S)
        if sig:
            emitted |= set(re.findall(r'^\s*"([a-z_0-9]+)":', sig.group(1), re.M))
        emitted |= {"cloudflare_interactive"}   # synthesised by cType, not a _SIGNATURES key
    # Success tags are deliberately not in the table — they say how a page WAS retrieved, and
    # errors.py is explicit that they only ever appear in the recovered-by breakdown, never as a
    # crawl_history error_category. `tls_unverified` is one of them (a page fetched with
    # verification disabled by the per-host opt-in): a success, an untrusted one, but not a failure.
    emitted -= {v for v in emitted
                if v.startswith("served_") or v.startswith("captcha_solved")
                or v == "tls_unverified"}
    unmapped = sorted(v for v in emitted if ec.classify(v)[0] == "unknown" and v != "other")
    assert not unmapped, (f"crawler/errors.py emits reasons this table does not classify, so they "
                          f"would pile up under 'unknown': {unmapped}")


def main() -> None:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    ec.demo()
    print(f"ALL {len(fns)} ERROR-CLASS TESTS PASSED")


if __name__ == "__main__":
    main()

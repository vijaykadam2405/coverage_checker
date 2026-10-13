"""The language-switcher trap guard.

hbc-radiomatic.com's switcher links the CURRENT path under every locale, so each page yields ~20
children that are the same page one segment deeper. Measured live: 45,169 pending URLs on one
instance, 28,540 of them nine or more locales deep, and the crawl could not finish because the
frontier grew faster than it drained.

The risk in fixing it is the opposite failure — refusing real localized sites, which is most of a
defence catalogue. So the false-positive cases below matter more than the trap cases.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crawler import errors  # noqa: E402

TRAP = errors.TRAP


def test_the_measured_trap_urls_are_refused():
    """Real URLs taken from the live frontier."""
    for url in (
        "https://www.hbc-radiomatic.com/de/de/pt/fi/tr/nl/it/es/cs/ch/no/startsideqwe.html",
        "https://www.hbc-radiomatic.com/de/de/pt/fi/tr/nl/it/es/cs/fr/sv/startsida.html",
        "https://www.hbc-radiomatic.com/de/de/pt/fi/tr/nl/sv/ch/en-GB/cs/no/startsideqwe.html",
        "https://www.hbc-radiomatic.com/de/de/pt/fi/tr/nl/it/es/cs/fr/en/Home.html",
    ):
        assert errors.looks_like_trap(url) == TRAP, url


def test_it_is_caught_at_the_SECOND_hop_not_the_twentieth():
    """Catching it only at depth 9 would still admit thousands of pages."""
    assert errors.looks_like_trap("https://x.com/de/en/fr/start.html") == TRAP
    # ...and the first hop, a genuinely localized page, is still allowed
    assert errors.looks_like_trap("https://x.com/de/start.html") is None


def test_ordinary_localized_sites_are_still_crawlable():
    """One locale segment is how the entire multilingual web is built. Two happens (a locale plus a
    word that is also a language code). Neither may be refused."""
    for url in (
        "https://www.airbus.com/en/products",
        "https://www.airbus.com/de/produkte",
        "https://example.de/en-GB/about/leadership",
        "https://example.com/pt_BR/produtos/motores",
        "https://example.com/zh-Hans/news",
        # `it` = IT department, `no` = a real word, `is` = a real word — each collides with an
        # ISO-639 code, and each must survive alongside one genuine locale.
        "https://example.com/en/support/it/helpdesk",
        "https://example.com/de/karriere/no/stellen",
        "https://example.com/services/it/consulting",
        "https://example.com/is/fréttir",
        "https://example.com/la/products",
    ):
        assert errors.looks_like_trap(url) is None, url


def test_two_letter_segments_that_are_not_languages_do_not_count():
    """`hr`, `pr`, `qa`, `ai` look like locale codes but are ordinary path words. If the guard
    matched a bare [a-z]{2} these would trip it and take real sites down with the trap."""
    assert errors.looks_like_trap("https://example.com/hr/pr/qa/openings") is None
    assert errors.looks_like_trap("https://example.com/ai/ml/nlp/research") is None


def test_the_guard_is_stateless_and_cheap():
    """It runs on every discovered link before the network does, so it must not need job state."""
    assert errors.looks_like_trap("https://x.com/de/en/fr/") == TRAP
    assert errors.looks_like_trap("https://x.com/de/en/fr/") == TRAP  # idempotent


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")
    print("ALL LOCALE-TRAP TESTS PASSED")

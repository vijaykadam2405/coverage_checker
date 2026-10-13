"""Self-checks for the site-classification probe (crawler/probe.py).

Assert-based, NO network — run directly:  python cralwer/tests/test_probe.py
Covers the pure decision layer (`recommend`) plus the offline HTML helpers (_identity_surface,
_section_breadth, _seed_path_prefix). The live fetch path is exercised by the manual sweep.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # cralwer/ on the path

from crawler.probe import (DEFENCE_TOPIC_TERMS, OFFTOPIC_SECTIONS, _IDENTITY_OFFTOPIC, _count_terms,
                           _identity_surface, _section_breadth, _seed_path_prefix, recommend)


def _m(**over) -> dict:
    """A healthy PURE-defence measurement set — every test perturbs one axis of it."""
    base = {"fetch_ok": True, "body_words": 900, "same_host_links": 60, "distinct_sections": 12,
            "distinct_offtopic_sections": 0, "offtopic_link_ratio": 0.0, "top_offtopic": [],
            "identity_offtopic_terms": 0, "identity_defence_terms": 21, "needs_render": False,
            "html_lang": "en", "seed_path_prefix": "", "status": 200, "error": None}
    base.update(over)
    return base


def test_pure_site_keeps_gate_off():
    r = recommend(_m())
    assert r["keyword"] is False, r          # capture everything — no silent drops
    assert r["render_js"] is False
    assert r["path_scope"] == []
    assert r["confidence"] == "high"
    assert r["review_required"] is False


def test_zero_corpus_hits_still_pure():
    # drdo.gov.in / sipri.org score 0.0 corpus hits/1000 words yet are pure — the corpus is
    # named entities, not topic vocabulary, so it must not move the gate.
    r = recommend(_m(corpus_hits_per_1k=0.0, identity_defence_terms=0))
    assert r["keyword"] is False, r


def test_high_offtopic_ratio_flips_to_mixed():
    r = recommend(_m(offtopic_link_ratio=0.41, same_host_links=140,
                     distinct_offtopic_sections=2, top_offtopic=["sport", "bollywood"]))
    assert r["keyword"] is True, r
    assert r["confidence"] == "high"


def test_high_ratio_on_too_few_links_does_not_flip():
    # 0.4 of 8 links is 3 links — not evidence of a publisher's section fan-out.
    r = recommend(_m(offtopic_link_ratio=0.4, same_host_links=8))
    assert r["keyword"] is False, r
    assert r["review_required"] is True       # ...but too thin a link graph to trust either way


def test_four_distinct_offtopic_sections_flips():
    r = recommend(_m(distinct_offtopic_sections=4, offtopic_link_ratio=0.09,
                     top_offtopic=["sport", "horoscope", "food", "travel"]))
    assert r["keyword"] is True, r


def test_three_distinct_offtopic_sections_does_not_flip():
    r = recommend(_m(distinct_offtopic_sections=3, offtopic_link_ratio=0.05))
    assert r["keyword"] is False, r


def test_identity_offtopic_terms_flip():
    # Nav says "Sports / Bollywood / Horoscope" even when the link slugs are opaque ids.
    r = recommend(_m(identity_offtopic_terms=3))
    assert r["keyword"] is True, r


def test_thin_page_is_low_confidence():
    r = recommend(_m(body_words=90))
    assert r["confidence"] == "low"
    assert r["review_required"] is True


def test_needs_render_is_low_confidence_and_sets_render_js():
    # A JS shell has no text, so the density measurement is MEANINGLESS — coupled signals.
    r = recommend(_m(body_words=40, same_host_links=2, needs_render=True))
    assert r["render_js"] is True
    assert r["confidence"] == "low"
    assert r["review_required"] is True


def test_fetch_failure_is_low_confidence():
    r = recommend(_m(fetch_ok=False, status=403, error="Forbidden"))
    assert r["confidence"] == "low"
    assert r["review_required"] is True
    assert r["keyword"] is False              # no evidence -> stay on the safe default


def test_non_english_is_low_confidence():
    r = recommend(_m(html_lang="fr"))
    assert r["confidence"] == "low"
    r_en = recommend(_m(html_lang="en-IN"))
    assert r_en["confidence"] == "high"


def test_medium_band():
    r = recommend(_m(body_words=200, same_host_links=14))
    assert r["confidence"] == "medium"
    assert r["review_required"] is False


def test_path_scope_only_when_seeded_deep_and_mixed():
    mixed = dict(distinct_offtopic_sections=5)
    assert recommend(_m(seed_path_prefix="/topic/defence", **mixed))["path_scope"] == \
        ["/topic/defence"]
    assert recommend(_m(seed_path_prefix="", **mixed))["path_scope"] == []      # bare host
    assert recommend(_m(seed_path_prefix="/topic/defence"))["path_scope"] == []  # pure site


def test_count_terms_is_word_boundary():
    # Regression: substring matching made "transport" hit "sport" and "healthcare" hit "health",
    # which alone pushed sipri.org (0 off-topic sections) over the 3-term flip.
    def n(text, terms=OFFTOPIC_SECTIONS):
        norm = " " + text.lower().replace("-", " ") + " "
        return _count_terms(terms, set(norm.split()), norm)

    assert n("arms transport and export, healthcare, automatic") == 0
    # Research themes / site chrome must not read as editorial sections in PROSE (they still
    # count as breadth when they are actual URL sections — different surface, different list).
    assert n("peace education; food, peace and security; film; photo gallery; videos",
             _IDENTITY_OFFTOPIC) == 0
    assert n("sport entertainment horoscope") == 3
    assert n("real-estate listings") == 1            # hyphens normalise to the phrase form
    assert n("ministry of defence procurement", DEFENCE_TOPIC_TERMS) >= 3   # multiword phrase


def test_seed_path_prefix():
    assert _seed_path_prefix("https://www.thehindu.com/topic/defence/") == "/topic/defence"
    assert _seed_path_prefix("https://x.com/topic/defence/index.html") == "/topic/defence"
    assert _seed_path_prefix("https://www.sipri.org/") == ""
    assert _seed_path_prefix("https://www.sipri.org") == ""


def test_section_breadth():
    links = ([f"https://p.test/sport/{i}" for i in range(6)]
             + [f"https://p.test/bollywood/{i}" for i in range(3)]
             + ["https://p.test/horoscope/a", "https://p.test/food/b"]
             + [f"https://p.test/defence/{i}" for i in range(10)])
    b = _section_breadth(links)
    assert b["distinct_sections"] == 5, b
    assert b["distinct_offtopic_sections"] == 4, b
    assert 0.5 < b["offtopic_link_ratio"] < 0.56, b
    assert "sport" in b["top_offtopic"] and b["top_offtopic"][0] == "sport"
    assert _section_breadth([])["offtopic_link_ratio"] == 0.0


def test_identity_surface_keeps_nav_and_meta():
    html = """<html lang="en"><head><title>Missile Systems Ltd</title>
      <meta name="description" content="naval and radar programmes">
      </head><body>
      <nav><a href="/sport/x">Sports</a><a href="/missiles/y">Missile Systems</a></nav>
      <p>body copy</p>
      <footer><a href="https://other.test/z">partner</a></footer></body></html>"""
    surface, same_host = _identity_surface(html, "https://p.test/")
    assert "missile systems ltd" in surface        # <title>
    assert "naval and radar programmes" in surface  # meta description
    assert "sports" in surface                      # nav text — visible_text would drop this
    assert same_host == ["https://p.test/sport/x", "https://p.test/missiles/y"]  # offsite dropped


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\nall {len(tests)} probe self-checks passed")


if __name__ == "__main__":
    main()

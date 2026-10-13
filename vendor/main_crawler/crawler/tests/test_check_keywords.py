"""Tests for scripts/check_keywords.py — the standalone keyword-relevance
probe, run offline against the shipped fixtures. The probe matches a
caller-supplied keyword list with the SAME word-boundary FlashText matcher the
gate uses over the global corpus, so its matching never disagrees with the gate
on the same keywords."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler import keywords as kwmod
from crawler.gate import evaluate
from crawler.models import Job
from scripts.check_keywords import check_keywords, discover_keywords

LT_URL = "https://idrw.org/lt-k9-vajra-followon/"


def test_check_keywords_match():
    result = check_keywords(LT_URL, ["K9 Vajra", "artillery", "submarine"])
    assert result["matched"] is True
    assert "K9 Vajra" in result["matched_keywords"]
    assert "artillery" in result["matched_keywords"]
    assert "submarine" not in result["matched_keywords"]


def test_check_keywords_no_match():
    result = check_keywords(LT_URL, ["submarine", "frigate"])
    assert result["matched"] is False
    assert result["matched_keywords"] == []


def test_check_keywords_word_boundary_not_substring():
    # "till" is a substring of "artillery" (present in the fixture text) but
    # never appears as its own word — the probe must NOT match it, proving
    # it reuses the gate's bounded regex rather than naive `kw in text`.
    result = check_keywords(LT_URL, ["till"])
    assert result["matched"] is False
    assert result["matched_keywords"] == []


def test_check_keywords_error_on_unfetchable_url():
    result = check_keywords("https://nonexistent.example/x", ["artillery"])
    assert result["matched"] is False
    assert result["matched_keywords"] == []
    assert result["error"]


def test_check_keywords_uses_same_matcher_as_gate():
    keywords = ["K9 Vajra", "L&T", "artillery", "howitzer", "submarine"]
    probe = check_keywords(LT_URL, keywords)

    # The probe reuses the gate's word-boundary FlashText matcher — ground truth.
    expected = kwmod.find(kwmod.from_list(keywords), probe["_title"], probe["_text"])
    assert probe["matched_keywords"] == expected

    # Feed those same keywords to the gate AS its corpus -> identical hits, page kept.
    kp = kwmod.from_list(keywords)
    job = Job(job_id="cmp", job_type="news", seed_urls=[LT_URL], keywords=keywords)
    g = evaluate(job, probe["_title"], probe["_text"], None, kp)
    assert g.keep is True
    assert set(g.matched_keywords) == set(probe["matched_keywords"])


def test_discover_keywords_returns_only_pool_members_that_hit():
    # Probe-adaptive selection: given a broad candidate pool, discover_keywords
    # returns exactly the subset present on the page — no more, no less.
    pool = ["K9 Vajra", "L&T", "artillery",   # present on the LT fixture
            "submarine", "frigate", "stealth destroyer"]  # absent
    disc = discover_keywords(LT_URL, pool)
    assert disc["pool_size"] == 6
    selected = set(disc["selected_keywords"])
    # every selected term is a pool member
    assert selected <= set(pool)
    # the present ones are selected, the absent ones are not
    assert {"K9 Vajra", "artillery"} <= selected
    assert selected.isdisjoint({"submarine", "frigate", "stealth destroyer"})
    # selection equals what the gate's own matcher finds (never disagrees)
    probe = check_keywords(LT_URL, pool)
    assert set(disc["selected_keywords"]) == set(probe["matched_keywords"])


def test_discover_keywords_empty_when_nothing_relevant():
    disc = discover_keywords(LT_URL, ["submarine", "frigate"])
    assert disc["selected_keywords"] == []

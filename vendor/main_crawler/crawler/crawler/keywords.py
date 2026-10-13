"""Stage-2 keep-filter corpus (§3) — the one global keyword trie.

A page is kept iff its title+main_text contains >=1 corpus keyword. The corpus
is a user-maintained CSV (``Keywords,Full form``); we match on BOTH surfaces so
a page hits on either the short designation (BEL) or the expanded name (Bharat
Electronics), and rows with an empty ``Keywords`` fall back to ``Full form``.
FlashText compiles the ~1.6k surfaces into one trie and scans the text in a
single O(len(text)) pass — the whole corpus costs the same as one keyword, with
native word-boundary matching (so ``AK-47`` matches whole, not inside a word).

ponytail: one FlashText trie, no entity fan-out — here we only need "does any
keyword appear?".
"""
from __future__ import annotations

import csv
import logging
import os

from . import config

log = logging.getLogger("keywords")

DEFAULT_CORPUS = config.PROJECT_ROOT / "All final keywords - combine keywords.csv"
_CACHE: dict | None = None

# Homograph stoplist: short company acronyms that ARE common English words, so matched
# case-insensitively they false-positive on ordinary news ("big"/"hit"/"sea"/"engine").
# We drop ONLY these exact standalone surfaces — the companies' multi-word full forms
# ("Heavy Industries Taxila", "Bavarian Industries Group", …) stay in the corpus, so those
# firms are still detected by full name. Extend via CRAWLER_KEYWORD_STOPLIST (comma-sep).
# ponytail: a 7-word blocklist, not a stopword NLP model — add a word when a new one bites.
_DEFAULT_STOPLIST = {"hit", "big", "sea", "alpha", "sigma", "spear", "engine"}


def _stoplist() -> set[str]:
    extra = os.environ.get("CRAWLER_KEYWORD_STOPLIST", "")
    return _DEFAULT_STOPLIST | {w.strip().lower() for w in extra.split(",") if w.strip()}


def _corpus_path() -> str:
    return os.environ.get("CRAWLER_KEYWORDS_FILE", str(DEFAULT_CORPUS))


def load_corpus(path: str | None = None):
    """Build a case-insensitive FlashText KeywordProcessor from the CSV.

    Adds the stripped ``Keywords`` value and, when present, its ``Full form``
    alias. A missing/empty file yields an EMPTY processor — the gate treats an
    empty corpus as keep-all (fail-open) and we log a warning, so a bad path
    can't silently zero the crawl output.
    """
    from flashtext import KeywordProcessor
    kp = KeywordProcessor(case_sensitive=False)
    stop = _stoplist()
    p = path or _corpus_path()
    dropped = 0
    try:
        # utf-8-sig strips a BOM if the CSV was saved from Excel; csv.DictReader
        # handles the quoted commas in some Full form cells.
        with open(p, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                for col in ("Keywords", "Full form"):
                    kw = (row.get(col) or "").strip()
                    if not kw:
                        continue
                    if kw.lower() in stop:          # homograph noise → skip this surface
                        dropped += 1
                        continue
                    kp.add_keyword(kw)              # clean_name defaults to kw itself
    except FileNotFoundError:
        log.warning("keyword corpus not found at %s — keep-all (fail-open)", p)
        return kp
    except Exception:                               # unreadable/mangled CSV -> fail-open, don't crash the crawl
        log.warning("keyword corpus at %s unreadable — keep-all (fail-open)", p, exc_info=True)
        return kp
    if len(kp) == 0:
        log.warning("keyword corpus %s parsed 0 keywords — keep-all (fail-open)", p)
    else:
        log.info("keyword corpus loaded: %d surfaces from %s (%d homograph surfaces stopped)",
                 len(kp), p, dropped)
    return kp


def get_corpus(path: str | None = None):
    """Cached default corpus (built once per process). Callers that don't already
    hold a KeywordProcessor use this so we compile the trie only once."""
    global _CACHE
    if _CACHE is None:
        _CACHE = {}
    key = path or _corpus_path()
    if key not in _CACHE:
        _CACHE[key] = load_corpus(path)
    return _CACHE[key]


def find(kp, title: str, main_text: str) -> list[str]:
    """Distinct corpus keywords present in title+main_text (>=1 => keep)."""
    found = kp.extract_keywords(f"{title}\n{main_text}")
    return list(dict.fromkeys(found))               # dedupe, preserve first-seen order


def from_list(words: list[str]):
    """Ad-hoc KeywordProcessor from an arbitrary keyword list — used by the
    /v1/check-keywords probe (which matches a caller-supplied list, not the
    global corpus) so its word-boundary matching stays identical to the gate."""
    from flashtext import KeywordProcessor
    kp = KeywordProcessor(case_sensitive=False)
    for w in words:
        w = (w or "").strip()
        if w:
            kp.add_keyword(w)
    return kp


if __name__ == "__main__":   # run: python -m crawler.keywords
    import io
    from flashtext import KeywordProcessor as _KP

    def _kp_from(text: str):
        kp = _KP(case_sensitive=False)
        for row in csv.DictReader(io.StringIO(text)):
            for col in ("Keywords", "Full form"):
                kw = (row.get(col) or "").strip()
                if kw:
                    kp.add_keyword(kw)
        return kp

    kp = _kp_from("Keywords,Full form\nBrahMos-NG,\nBEL,Bharat Electronics\n,Nexter Systems\n")
    assert find(kp, "News", "The BrahMos-NG missile was tested.") == ["BrahMos-NG"]
    assert find(kp, "", "Order placed with Bharat Electronics today.") == ["Bharat Electronics"]
    assert find(kp, "Nexter Systems wins deal", "") == ["Nexter Systems"]   # empty-Keywords row -> Full form
    assert find(kp, "Weather report", "Sunny with light rain.") == []
    assert find(kp, "", "A rebellion in the region.") == []                 # word-boundary: 'BEL' not inside 'rebellion'

    real = load_corpus()
    assert len(real) > 1500, f"real corpus surfaces={len(real)} (expected >1500)"
    # Homograph stoplist: the noisy short surface is gone, but the company's full form stays,
    # so a generic-news homepage no longer false-hits while the firm is still detectable.
    assert find(real, "Latest big news headlines and top stories", "") == [], \
        "stoplist should drop 'BIG'/'HIT' etc. so generic news doesn't hit"
    assert "Heavy Industries Taxila" in find(real, "", "Heavy Industries Taxila upgraded its plant."), \
        "full-form alias must still match after the short surface is stopped"
    print(f"OK — self-check passed; real corpus surfaces={len(real)}")

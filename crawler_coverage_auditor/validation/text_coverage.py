"""Duplicate-resistant word retention, not exact semantic/text completeness."""
import re

from .fingerprints import clean_text, sha256_text, text_similarity

ASSESSED_TEXT_STATUSES = {'FOUND_FULL', 'FOUND_MOSTLY_FULL', 'FOUND_PARTIAL_TEXT',
                         'FOUND_VERY_PARTIAL_TEXT', 'TEXT_MISSING'}
TEXT_API_VERSION = 1


def compare_html_text(live: str, stored: str) -> dict:
    """Reuse the historical similarity and measure directional live-word coverage.

    Extra corpus boilerplate does not reduce retention. Duplicate live words do
    not inflate the denominator (carousels/responsive clones may repeat text).
    Matching words do not prove ordering or semantic completeness.
    Snapshots may have different capture dates; differences are not proof of a bug.
    """
    live, stored = clean_text(live), clean_text(stored)
    words = lambda value: re.findall(r'\w+', value.casefold(), flags=re.UNICODE)
    source, target = words(live), set(words(stored))
    if not source:
        return {'status': 'SOURCE_TEXT_NOT_AVAILABLE', 'score': None, 'evidence': {}}
    retention = lambda items: len(set(items) & target) / len(set(items)) if items else None
    source_set = set(source)
    matched = source_set & target
    score = len(matched) / len(source_set)
    status = ('TEXT_MISSING' if not stored else 'FOUND_FULL' if score >= .98
              else 'FOUND_MOSTLY_FULL' if score >= .9 else 'FOUND_PARTIAL_TEXT' if score >= .5
              else 'FOUND_VERY_PARTIAL_TEXT')
    # Non-overlapping thirds; small pages still have meaningful sections.
    segments = [source[:len(source)//3], source[len(source)//3:2*len(source)//3], source[2*len(source)//3:]]
    return {'status': status, 'score': score, 'evidence': {
        'method': 'unique_word_coverage', 'live_words': len(source),
        'live_unique_words': len(source_set), 'matched_unique_words': len(matched),
        'live_characters': len(live), 'corpus_characters': len(stored),
        'token_set_similarity': text_similarity(live, stored),
        'segments': {name: round(retention(items)*100, 2) if items else None
                     for name, items in zip(('beginning', 'middle', 'ending'), segments)},
        'live_hash': sha256_text(live), 'corpus_hash': sha256_text(stored)}}

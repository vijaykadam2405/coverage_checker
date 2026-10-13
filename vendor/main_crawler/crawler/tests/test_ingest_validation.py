"""Tests for the page-bundle acceptance rules (ingest_api/validation.py)."""
from ingest_api.validation import validate_page

GOOD_DOC = {
    "url": "https://idrw.org/x",
    "content_hash": "sha256:abc",
    "document_id": "doc_1",
    "main_text": "L&T won a K9 Vajra order.",
    "published_at": "2026-06-28T00:00:00Z",
    "entities_detected": [{"surface": "L&T", "resolved_id": "LT",
                           "type": "competitor", "confidence": 0.97}],
}


def test_accepts_valid_document():
    ok, rule = validate_page(GOOD_DOC)
    assert ok and rule is None


def test_rule1_empty_main_text():
    doc = {**GOOD_DOC, "main_text": "  "}
    ok, rule = validate_page(doc)
    assert not ok and rule == "rule1_empty_main_text"


def test_rule1_missing_canonical_url():
    doc = {**GOOD_DOC, "url": ""}
    ok, rule = validate_page(doc)
    assert not ok and rule == "rule1_missing_canonical_url"


def test_rule1_missing_content_hash():
    doc = {**GOOD_DOC, "content_hash": "sha256:empty"}
    ok, rule = validate_page(doc)
    assert not ok and rule == "rule1_missing_content_hash"


def test_rule4_bad_date():
    doc = {**GOOD_DOC, "published_at": "not-a-date"}
    ok, rule = validate_page(doc)
    assert not ok and rule == "rule4_bad_date:document.published_at"


def test_rejects_empty_document():
    ok, rule = validate_page({})
    assert not ok and rule == "rule1_missing_document"

from __future__ import annotations

import hashlib
import re


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def sha256_text(text: str) -> str:
    return hashlib.sha256(clean_text(text).encode("utf-8", "ignore")).hexdigest()


def text_similarity(left: str, right: str) -> float:
    """Token-set similarity appropriate for initial coverage classification."""
    # split() already removes/collapses whitespace. Keep legacy lower()/whitespace
    # tokens distinct from directional casefold()/Unicode-word tokenization.
    left_tokens, right_tokens = set(left.lower().split()), set(right.lower().split())
    if not left_tokens and not right_tokens:
        return 1.0
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)

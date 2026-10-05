"""Bounded text helpers for agent working-memory payloads."""

from __future__ import annotations

from hashlib import sha256

DEFAULT_PAGE_EXCERPT_CHAR_LIMIT = 4_000
DEFAULT_EVIDENCE_EXCERPT_CHAR_LIMIT = 4_000
DEFAULT_SHARED_LEARNING_EXCERPT_CHAR_LIMIT = 4_000
DEFAULT_PROMPT_ASSET_EXCERPT_CHAR_LIMIT = 4_000


def bounded_text_payload(value: str, *, limit: int) -> dict[str, object]:
    """Return a deterministic excerpt plus audit metadata for agent prompts."""
    if not isinstance(value, str):
        raise TypeError("bounded text value must be a string.")
    if not isinstance(limit, int) or limit <= 0:
        raise ValueError("bounded text limit must be a positive integer.")
    return {
        "sha256": sha256(value.encode("utf-8")).hexdigest(),
        "char_count": len(value),
        "truncated": len(value) > limit,
        "excerpt": value[:limit],
    }

"""Token estimates, hashing, truncation helpers."""

from __future__ import annotations

import hashlib
from typing import Any

import orjson


def count_tokens(text: str) -> int:
    """Budgeting estimate (~4 chars/token). Real usage is reconciled from gateway responses."""
    return max(1, len(text) // 4)


def canonical_hash(*parts: Any) -> str:
    return hashlib.sha256(orjson.dumps(parts, option=orjson.OPT_SORT_KEYS, default=str)).hexdigest()


def head_tail(text: str, max_chars: int = 6000) -> str:
    """Keep the start and end of long tool output (errors are usually at the end)."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return f"{text[:half]}\n\n[... {len(text) - max_chars} characters omitted ...]\n\n{text[-half:]}"

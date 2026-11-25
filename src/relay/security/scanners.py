"""Outbound content scanning: secrets (regex + entropy) and PII, applied to every argument of an
external write (comments, tickets, messages, emails) before it leaves the system."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from relay.common.logging import SECRET_PATTERNS

_PII = [
    ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("credit_card", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("us_ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
]
_TOKENISH = re.compile(r"[A-Za-z0-9+/_\-]{32,}")


@dataclass(slots=True, frozen=True)
class ScanHit:
    kind: str
    category: str  # secret | pii
    excerpt: str


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _luhn(digits: str) -> bool:
    d = [int(x) for x in digits if x.isdigit()]
    if len(d) < 13:
        return False
    s = 0
    for i, v in enumerate(reversed(d)):
        if i % 2:
            v *= 2
            if v > 9:
                v -= 9
        s += v
    return s % 10 == 0


def scan_text(text: str, *, allow_emails: bool = True) -> list[ScanHit]:
    hits: list[ScanHit] = []
    for name, pat in SECRET_PATTERNS:
        for m in pat.finditer(text):
            hits.append(ScanHit(name, "secret", m.group(0)[:12] + "…"))
    for m in _TOKENISH.finditer(text):
        tok = m.group(0)
        # high-entropy blobs look like keys; skip hex digests (git shas, checksums) which are common in reviews
        if shannon_entropy(tok) > 4.5 and not re.fullmatch(r"[0-9a-f]+", tok):
            hits.append(ScanHit("high_entropy", "secret", tok[:8] + "…"))
    for name, pat in _PII:
        if name == "email" and allow_emails:
            continue
        for m in pat.finditer(text):
            if name == "credit_card" and not _luhn(m.group(0)):
                continue
            hits.append(ScanHit(name, "pii", m.group(0)[:4] + "…"))
    return hits


def scan_args(args: dict[str, Any], **kw: Any) -> list[ScanHit]:
    out: list[ScanHit] = []

    def walk(v: Any) -> None:
        if isinstance(v, str):
            out.extend(scan_text(v, **kw))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(args)
    return out

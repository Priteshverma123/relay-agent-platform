"""Webhook signature verification (constant-time, replay-windowed)."""

from __future__ import annotations

import hashlib
import hmac
import time


def verify_github(secret: str, body: bytes, signature_header: str | None) -> bool:
    """GitHub: ``X-Hub-Signature-256: sha256=<hex hmac of raw body>``."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def verify_slack(secret: str, body: bytes, timestamp: str | None, signature: str | None, *,
                 now: float | None = None, max_age_s: int = 300) -> bool:
    """Slack: ``v0=<hex hmac of 'v0:{ts}:{body}'>``; rejects requests older than 5 minutes (replay)."""
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now or time.time()) - ts) > max_age_s:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def sign_github(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def sign_slack(secret: str, body: bytes, timestamp: str) -> str:
    return "v0=" + hmac.new(secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()

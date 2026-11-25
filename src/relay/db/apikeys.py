"""API keys for Relay's own API (operator-issued, from settings: ``key:tenant_uuid`` pairs).

Relay is an internal platform service; tenants and keys are provisioned by operators (or by
Atlas' admin API in a combined deployment). Comparison is constant-time.
"""

from __future__ import annotations

import hmac

from relay.common.settings import get_settings


def resolve_key(raw: str) -> str | None:
    for entry in get_settings().api_keys:
        key, _, tenant = entry.partition(":")
        if key and hmac.compare_digest(key.encode(), raw.encode()):
            return tenant
    return None

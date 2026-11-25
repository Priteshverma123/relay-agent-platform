"""Shared request helpers (tenant resolution for routers mounted with the auth dependency)."""

from __future__ import annotations

from fastapi import Request

from relay.common.errors import Unauthorized
from relay.db.apikeys import resolve_key


def tenant_of(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    t = resolve_key(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
    if t is None:
        raise Unauthorized("invalid api key")
    return t

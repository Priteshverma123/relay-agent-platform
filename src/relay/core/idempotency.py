"""Idempotency keys for side effects.

LangGraph re-executes the *whole node* when a run resumes after an interrupt or a crash, so any
side effect (comment, ticket, push) can be attempted twice. The key is derived from stable
inputs only, the run id, the model-assigned tool_call id (persisted in the checkpoint) and the
canonical arguments, so a replay maps to the same key and the stored result is returned instead
of acting again.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from relay.common.text import canonical_hash


def idempotency_key(run_id: uuid.UUID | str, tool: str, args: dict[str, Any], call_id: str = "") -> str:
    return canonical_hash(str(run_id), tool, args, call_id)


class IdempotencyStore(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def put(self, key: str, result: dict[str, Any]) -> None: ...


class MemoryIdempotencyStore:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    async def get(self, key: str) -> dict[str, Any] | None:
        return self.data.get(key)

    async def put(self, key: str, result: dict[str, Any]) -> None:
        self.data.setdefault(key, result)  # first write wins


class RedisIdempotencyStore:
    """Fast path in Redis (SET NX) with a 7-day TTL; ``tool_calls.idempotency_key`` (unique) is
    the durable record."""

    def __init__(self, redis: Any, ttl_s: int = 7 * 86400) -> None:
        self.r, self.ttl = redis, ttl_s

    async def get(self, key: str) -> dict[str, Any] | None:
        import orjson

        raw = await self.r.get(f"idem:{key}")
        return orjson.loads(raw) if raw else None  # type: ignore[no-any-return]

    async def put(self, key: str, result: dict[str, Any]) -> None:
        import orjson

        await self.r.set(f"idem:{key}", orjson.dumps(result, default=str), ex=self.ttl, nx=True)

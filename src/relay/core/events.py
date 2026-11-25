"""Typed, append-only run event log.

Every message, action (tool call), observation (tool result), approval, budget/stuck signal and
outcome is appended here, redacted, with a gap-free per-run sequence number. Status and spend on
``runs`` are *derived* projections; the log is the source of truth for replay, the run viewer,
trajectory evals and audits (OpenHands-style event sourcing).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import func, select, text

from relay.common.logging import redact
from relay.db.models import RunEvent
from relay.db.session import tenant_session


class EventType(StrEnum):
    RUN_STARTED = "run_started"
    MESSAGE = "message"  # LLM output (assistant text)
    ACTION = "action"  # tool call proposed
    DECISION = "decision"  # policy decision for an action
    OBSERVATION = "observation"  # tool result
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    BUDGET = "budget"
    STUCK = "stuck"
    CONDENSED = "condensed"
    TAINTED = "tainted"
    PHASE = "phase"  # graph node entered (planner, programmer, verifier, ...)
    RUN_FINISHED = "run_finished"


@dataclass(slots=True)
class Event:
    type: EventType
    payload: dict[str, Any] = field(default_factory=dict)
    tokens: int = 0
    cost_usd: float = 0.0
    seq: int = 0


class EventSink(Protocol):
    async def append(self, run_id: uuid.UUID, ev: Event) -> Event: ...


class PostgresEventLog:
    """Sequence numbers are allocated under a per-run transaction advisory lock, so concurrent
    writers (parallel tool executions) still produce a gap-free, ordered log."""

    def __init__(self, tenant_id: str, publish: Any = None) -> None:
        self.tenant_id = tenant_id
        self.publish = publish  # optional async callable(run_id, event_dict) → live SSE

    async def append(self, run_id: uuid.UUID, ev: Event) -> Event:
        payload = redact(ev.payload)
        async with tenant_session(self.tenant_id) as s:
            await s.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": str(run_id)})
            seq = (await s.execute(select(func.coalesce(func.max(RunEvent.seq), 0))
                                   .where(RunEvent.run_id == run_id))).scalar_one() + 1
            s.add(RunEvent(tenant_id=uuid.UUID(self.tenant_id), run_id=run_id, seq=seq, type=ev.type.value,
                           payload=payload, tokens=ev.tokens, cost_usd=ev.cost_usd))
        ev.seq, ev.payload = seq, payload
        if self.publish:
            await self.publish(run_id, {"seq": seq, "type": ev.type.value, "payload": payload})
        return ev


class MemoryEventLog:
    """In-memory sink for tests and offline evals."""

    def __init__(self) -> None:
        self.events: dict[uuid.UUID, list[Event]] = {}

    async def append(self, run_id: uuid.UUID, ev: Event) -> Event:
        lst = self.events.setdefault(run_id, [])
        ev.seq, ev.payload = len(lst) + 1, redact(ev.payload)
        lst.append(ev)
        return ev

    def of(self, run_id: uuid.UUID, type_: EventType | None = None) -> list[Event]:
        return [e for e in self.events.get(run_id, []) if type_ is None or e.type == type_]

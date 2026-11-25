"""Runs: start manually, list, inspect (run viewer data), live events (SSE), cancel."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from relay.api.deps import tenant_of
from relay.common.errors import InvalidInput, NotFound
from relay.core.runs import audit, set_status
from relay.db.models import Approval, Run, RunEvent, ToolCall
from relay.db.session import tenant_session
from relay.runtime import AGENTS
from relay.workers.main import AGENTS_Q

router = APIRouter(prefix="/v1/runs", tags=["runs"])


class StartRun(BaseModel):
    agent: str
    trigger: dict[str, Any]


def _run(r: Run) -> dict[str, Any]:
    return {"id": str(r.id), "agent": r.agent, "status": r.status, "tainted": r.tainted, "trigger": r.trigger,
            "budget": r.budget, "spent": r.spent, "outcome": r.outcome, "error": r.error,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None, "trace_id": r.trace_id}


@router.post("", status_code=202)
async def start(body: StartRun, request: Request) -> dict[str, str]:
    if body.agent not in AGENTS:
        raise InvalidInput(f"agent must be one of {sorted(AGENTS)}")
    t = tenant_of(request)
    rid = await request.app.state.rt.start(t, body.agent, body.trigger)
    await request.app.state.arq.enqueue_job("execute_run", t, str(rid), _queue_name=AGENTS_Q, _job_id=f"run:{rid}:0")
    await audit(t, "api", "run.start", str(rid), {"agent": body.agent})
    return {"run_id": str(rid), "status": "queued"}


@router.get("")
async def list_runs(request: Request, agent: str | None = None, status: str | None = None, limit: int = 50
                    ) -> list[dict[str, Any]]:
    async with tenant_session(tenant_of(request)) as s:
        q = select(Run).order_by(Run.started_at.desc()).limit(min(limit, 200))
        if agent:
            q = q.where(Run.agent == agent)
        if status:
            q = q.where(Run.status == status)
        return [_run(r) for r in (await s.execute(q)).scalars()]


@router.get("/{run_id}")
async def get_run(run_id: uuid.UUID, request: Request) -> dict[str, Any]:
    async with tenant_session(tenant_of(request)) as s:
        r = await s.get(Run, run_id)
        if r is None:
            raise NotFound("run not found")
        calls = (await s.execute(select(ToolCall).where(ToolCall.run_id == run_id))).scalars().all()
        aps = (await s.execute(select(Approval).where(Approval.run_id == run_id))).scalars().all()
    return {**_run(r), "tool_calls": [{"tool": c.tool, "class": c.tool_class, "status": c.status,
                                       "decision": c.decision} for c in calls],
            "approvals": [{"id": str(a.id), "kind": a.kind, "status": a.status, "decided_by": a.decided_by}
                          for a in aps]}


@router.get("/{run_id}/events")
async def events(run_id: uuid.UUID, request: Request, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
    async with tenant_session(tenant_of(request)) as s:
        rows = (await s.execute(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after)
                                .order_by(RunEvent.seq).limit(min(limit, 2000)))).scalars()
        return [{"seq": e.seq, "type": e.type, "payload": e.payload, "tokens": e.tokens, "cost_usd": e.cost_usd,
                 "ts": e.created_at.isoformat()} for e in rows]


@router.get("/{run_id}/stream")
async def stream(run_id: uuid.UUID, request: Request) -> EventSourceResponse:
    """Live events: replay persisted events, then follow Redis pub/sub."""
    t = tenant_of(request)
    redis = request.app.state.rt.redis

    async def gen() -> AsyncIterator[dict[str, str]]:
        import orjson

        pubsub = redis.pubsub()
        await pubsub.subscribe(f"run:{run_id}")
        try:
            last = 0
            for e in await events(run_id, request, 0, 2000):
                last = e["seq"]
                yield {"event": e["type"], "data": orjson.dumps(e, default=str).decode()}
            while not await request.is_disconnected():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
                if msg is None:
                    await asyncio.sleep(0.1)
                    continue
                ev = orjson.loads(msg["data"])
                if ev.get("seq", 0) > last:
                    last = ev["seq"]
                    yield {"event": ev["type"], "data": msg["data"].decode()}
                if ev.get("type") == "run_finished":
                    return
        finally:
            await pubsub.unsubscribe(f"run:{run_id}")
            await pubsub.aclose()

    _ = t
    return EventSourceResponse(gen(), ping=15)


@router.post("/{run_id}/cancel")
async def cancel(run_id: uuid.UUID, request: Request) -> dict[str, str]:
    t = tenant_of(request)
    await set_status(t, run_id, "cancelled")
    await audit(t, "api", "run.cancel", str(run_id))
    return {"status": "cancelled"}

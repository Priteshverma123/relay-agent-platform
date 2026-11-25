"""Human-in-the-loop approvals (API channel; Slack buttons hit /webhooks/slack/interactions)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from relay.api.deps import tenant_of
from relay.common.errors import Conflict
from relay.core.runs import decide_approval
from relay.db.models import Approval
from relay.db.session import tenant_session
from relay.workers.main import AGENTS_Q

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])


class Decision(BaseModel):
    approved: bool
    actor: str = Field(min_length=1, max_length=200)
    reason: str | None = None
    approved_calls: list[str] | None = Field(None, description="approve only a subset of tool calls")
    hint: str | None = Field(None, description="guidance for a stuck agent / plan edits")


@router.get("")
async def pending(request: Request, status: str = "pending") -> list[dict[str, Any]]:
    async with tenant_session(tenant_of(request)) as s:
        rows = (await s.execute(select(Approval).where(Approval.status == status)
                                .order_by(Approval.created_at.desc()).limit(200))).scalars()
        return [{"id": str(a.id), "run_id": str(a.run_id), "kind": a.kind, "request": a.request,
                 "created_at": a.created_at.isoformat()} for a in rows]


@router.post("/{approval_id}")
async def decide(approval_id: uuid.UUID, body: Decision, request: Request) -> dict[str, str]:
    t = tenant_of(request)
    try:
        run_id, resume = await decide_approval(t, approval_id, approved=body.approved, actor=f"api:{body.actor}",
                                               approved_calls=body.approved_calls, reason=body.reason, hint=body.hint)
    except ValueError as exc:
        raise Conflict(str(exc)) from exc
    await request.app.state.arq.enqueue_job("execute_run", t, str(run_id), resume, _queue_name=AGENTS_Q,
                                            _job_id=f"run:{run_id}:{approval_id}")
    return {"status": "resumed", "run_id": str(run_id)}

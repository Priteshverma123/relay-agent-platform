"""Run lifecycle persistence + the durable graph runner (checkpoint, interrupt → approval, resume).

    queued → running → (waiting_approval ↔ running) → succeeded | failed | budget_exceeded | cancelled
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from langgraph.types import Command
from sqlalchemy import select, update

from relay.common.logging import get_logger, redact
from relay.common.telemetry import current_trace_id
from relay.db.models import Approval, AuditLog, Run
from relay.db.session import tenant_session

log = get_logger(__name__)
TERMINAL = {"succeeded", "failed", "budget_exceeded", "cancelled"}


async def create_run(tenant_id: str, agent: str, trigger: dict[str, Any], budget: dict[str, Any],
                     parent: uuid.UUID | None = None) -> uuid.UUID:
    rid = uuid.uuid4()
    async with tenant_session(tenant_id) as s:
        s.add(Run(id=rid, tenant_id=uuid.UUID(tenant_id), agent=agent, trigger=redact(trigger), budget=budget,
                  status="queued", parent_run_id=parent, trace_id=current_trace_id()))
    return rid


async def set_status(tenant_id: str, run_id: uuid.UUID, status: str, **fields: Any) -> None:
    values: dict[str, Any] = {"status": status, **fields}
    if status in TERMINAL:
        values["finished_at"] = datetime.now(UTC)
    async with tenant_session(tenant_id) as s:
        await s.execute(update(Run).where(Run.id == run_id).values(**values))


async def audit(tenant_id: str, actor: str, action: str, target: str, details: dict[str, Any] | None = None) -> None:
    async with tenant_session(tenant_id) as s:
        s.add(AuditLog(tenant_id=uuid.UUID(tenant_id), actor=actor, action=action, target=target,
                       details=redact(details or {})))


async def pending_interrupt(app: Any, config: dict[str, Any]) -> dict[str, Any] | None:
    snap = await app.aget_state(config)
    for task in getattr(snap, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            return {"value": intr.value, "id": getattr(intr, "id", None) or getattr(intr, "interrupt_id", None)}
    return None


async def run_graph(app: Any, *, tenant_id: str, run_id: uuid.UUID, inputs: dict[str, Any] | None,
                    resume: Any = None, notify: Any = None) -> dict[str, Any]:
    """Drive a compiled graph until it finishes or pauses on an interrupt.

    On interrupt an ``approvals`` row is created (idempotent per interrupt id) and ``notify``
    (e.g. Slack) is called; the API's approval endpoint later re-enqueues with ``resume``.
    """
    config = {"configurable": {"thread_id": str(run_id)}, "recursion_limit": 250}
    await set_status(tenant_id, run_id, "running")
    payload = Command(resume=resume) if resume is not None else inputs
    state = await app.ainvoke(payload, config=config)
    intr = await pending_interrupt(app, config)
    if intr is not None:
        approval_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{intr['id'] or intr['value']}")
        async with tenant_session(tenant_id) as s:
            if await s.get(Approval, approval_id) is None:
                s.add(Approval(id=approval_id, tenant_id=uuid.UUID(tenant_id), run_id=run_id,
                               kind=str(intr["value"].get("kind", "tool_calls")), request=redact(intr["value"])))
        await set_status(tenant_id, run_id, "waiting_approval")
        if notify:
            try:
                await notify(approval_id, intr["value"])
            except Exception:
                log.warning("approval_notify_failed", exc_info=True)
        return {"status": "waiting_approval", "approval_id": str(approval_id)}
    status = state.get("status", "done")
    final = {"done": "succeeded", "partial": "budget_exceeded", "aborted": "failed"}.get(status, "succeeded")
    await set_status(tenant_id, run_id, final, outcome=redact({k: v for k, v in state.items()
                                                                if k in ("result", "status", "outcome", "pr_url",
                                                                         "findings", "report")}),
                     spent=state.get("spent", {}), tainted=bool(state.get("tainted")))
    return {"status": final, "result": state.get("result")}


async def decide_approval(tenant_id: str, approval_id: uuid.UUID, *, approved: bool, actor: str,
                          approved_calls: list[str] | None = None, reason: str | None = None,
                          hint: str | None = None) -> tuple[uuid.UUID, dict[str, Any]]:
    """Records the human decision and returns (run_id, resume_value) for the worker."""
    async with tenant_session(tenant_id) as s:
        ap = await s.get(Approval, approval_id)
        if ap is None or ap.status != "pending":
            raise ValueError("approval not found or already decided")
        ap.status = "approved" if approved else "rejected"
        ap.decided_by, ap.reason, ap.decided_at = actor, reason, datetime.now(UTC)
        kind, req, run_id = ap.kind, ap.request, ap.run_id
    await audit(tenant_id, actor, f"approval.{'approve' if approved else 'reject'}", str(approval_id),
                {"run_id": str(run_id), "reason": reason})
    if kind == "tool_calls":
        all_ids = [c["call_id"] for c in req.get("calls", [])]
        resume = {"approved": (approved_calls if approved_calls is not None else all_ids) if approved else []}
    elif kind == "stuck":
        resume = {"abort": not approved, "hint": hint}
    else:
        resume = {"approved": approved, "reason": reason, "edits": hint}
    return run_id, resume


async def list_runs(tenant_id: str, limit: int = 50, agent: str | None = None) -> list[Run]:
    async with tenant_session(tenant_id) as s:
        q = select(Run).order_by(Run.started_at.desc()).limit(limit)
        if agent:
            q = q.where(Run.agent == agent)
        return list((await s.execute(q)).scalars())

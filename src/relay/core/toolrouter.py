"""The ONLY path from a model's tool call to an effect.

    plan():    parse args → policy decision per call (pure; safe to re-run when a node re-executes)
    execute(): DENY → error result, APPROVE without approval → rejected result,
               ALLOW / approved → idempotency check → handler (timeout) → events, taint, stuck, budget

OpenHands' security docs note that its ``execute_tool()`` bypasses confirmation; here nothing
does: agents never receive handler references, only this router.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from relay.common.text import head_tail
from relay.core.budget import BudgetGuard
from relay.core.events import Event, EventSink, EventType
from relay.core.idempotency import IdempotencyStore, idempotency_key
from relay.core.stuck import StuckDetector, StuckSignal
from relay.llm.client import tool_args
from relay.security.policy import Decision, PolicyDecision, PolicyEngine, ToolClass
from relay.security.taint import TaintState
from relay.tools.base import ToolContext, ToolRegistry, ToolResult

NO_OUTPUT = "(command ran successfully and produced no output)"


@dataclass(slots=True)
class PlannedCall:
    call_id: str
    tool: str
    args: dict[str, Any]
    decision: PolicyDecision
    key: str

    def summary(self) -> dict[str, Any]:
        return {"call_id": self.call_id, "tool": self.tool, "args": self.args, **self.decision.to_dict()}


@dataclass(slots=True)
class ExecutionResult:
    messages: list[dict[str, Any]] = field(default_factory=list)
    stuck: StuckSignal | None = None
    newly_tainted: bool = False
    denied: int = 0


class ToolRouter:
    def __init__(self, registry: ToolRegistry, engine: PolicyEngine, ctx: ToolContext, events: EventSink,
                 idem: IdempotencyStore, taint: TaintState, budget: BudgetGuard,
                 stuck: StuckDetector | None = None, ledger: Any = None) -> None:
        self.registry, self.engine, self.ctx = registry, engine, ctx
        self.events, self.idem, self.taint, self.budget = events, idem, taint, budget
        self.stuck = stuck
        self.ledger = ledger  # optional async (PlannedCall, ToolResult, status) → durable tool_calls row

    @property
    def run_id(self) -> uuid.UUID:
        return self.ctx.run_id

    async def plan(self, tool_calls: list[dict[str, Any]]) -> list[PlannedCall]:
        out = []
        for tc in tool_calls:
            name = tc.get("function", {}).get("name", "")
            args = tool_args(tc)
            decision = await self.engine.evaluate(name, args, tainted=self.taint.tainted)
            if self.registry.get(name) is None and decision.decision != Decision.DENY:
                decision = PolicyDecision(Decision.DENY, decision.tool_class, decision.risk,
                                          [*decision.reasons, "tool not registered"])
            out.append(PlannedCall(tc.get("id") or uuid.uuid4().hex, name, args, decision,
                                   idempotency_key(self.run_id, name, args, tc.get("id") or "")))
        return out

    @staticmethod
    def needs_approval(planned: list[PlannedCall]) -> list[PlannedCall]:
        return [p for p in planned if p.decision.decision == Decision.APPROVE]

    async def execute(self, planned: list[PlannedCall], approved: set[str] | None = None,
                      rejected_reason: str = "rejected by a human reviewer") -> ExecutionResult:
        approved = approved or set()
        res = ExecutionResult()

        async def run_one(p: PlannedCall) -> tuple[PlannedCall, ToolResult]:
            await self.events.append(self.run_id, Event(EventType.ACTION, {"call_id": p.call_id, "tool": p.tool,
                                                                           "args": p.args}))
            await self.events.append(self.run_id, Event(EventType.DECISION, p.summary()))
            d = p.decision.decision
            if d == Decision.DENY:
                res.denied += 1
                return p, ToolResult(f"Denied by policy: {'; '.join(p.decision.reasons)}. Choose another approach "
                                     "or finish and explain the limitation.", is_error=True)
            if d == Decision.APPROVE and p.call_id not in approved:
                return p, ToolResult(f"Not executed: {rejected_reason}.", is_error=True)
            cached = await self.idem.get(p.key)
            if cached is not None:  # replay after resume/crash: never repeat the side effect
                return p, ToolResult(cached.get("output", ""), bool(cached.get("is_error")), cached.get("data", {}))
            tool = self.registry.get(p.tool)
            assert tool is not None
            self.budget.charge_tool()
            try:
                r = await asyncio.wait_for(tool.handler(p.args, self.ctx), timeout=tool.timeout_s)
            except TimeoutError:
                r = ToolResult(f"Tool timed out after {tool.timeout_s:.0f}s.", is_error=True)
            except Exception as exc:  # tool errors go back to the model as observations
                r = ToolResult(f"Tool error: {type(exc).__name__}: {str(exc)[:500]}", is_error=True)
            if not r.output.strip() and not r.is_error:
                r.output = NO_OUTPUT
            if p.decision.tool_class in (ToolClass.WRITE_EXTERNAL, ToolClass.DANGEROUS) or not r.is_error:
                await self.idem.put(p.key, {"output": r.output, "is_error": r.is_error, "data": r.data})
            return p, r

        # Reads can run in parallel; anything with effects runs sequentially in model order.
        reads = [p for p in planned if p.decision.tool_class == ToolClass.READ]
        others = [p for p in planned if p.decision.tool_class != ToolClass.READ]
        results: dict[str, ToolResult] = {}
        for p, r in await asyncio.gather(*(run_one(p) for p in reads)):
            results[p.call_id] = r
        for p in others:
            _, r = await run_one(p)
            results[p.call_id] = r
        if self.ledger is not None:
            for p in planned:
                r = results[p.call_id]
                status = "denied" if p.decision.decision == Decision.DENY else "failed" if r.is_error else "done"
                await self.ledger(p, r, status)

        for p in planned:  # tool results must answer every call, in order (provider requirement)
            r = results[p.call_id]
            await self.events.append(self.run_id, Event(EventType.OBSERVATION, {
                "call_id": p.call_id, "tool": p.tool, "is_error": r.is_error, "output": head_tail(r.output, 4000)}))
            if not r.is_error and self.taint.observe_tool(p.tool, self.registry.untrusted_tools()):
                res.newly_tainted = True
                await self.events.append(self.run_id, Event(EventType.TAINTED, {"source": f"tool:{p.tool}"}))
            if self.stuck is not None and res.stuck is None:
                res.stuck = self.stuck.observe(p.tool, p.args, r.output, is_error=r.is_error)
            res.messages.append({"role": "tool", "tool_call_id": p.call_id, "content": head_tail(r.output, 12_000)})
        return res

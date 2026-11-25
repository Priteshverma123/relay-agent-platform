"""Workflow executor with durable suspend/resume.

Execution state (``state["steps"]`` outputs, ``state["approvals"]``) is persisted on the run after
every step. Resuming re-walks the spec and *skips completed steps* (their outputs are reused), so
execution is idempotent at step granularity; side-effect steps additionally carry idempotency
keys via the ToolRouter. Approvals suspend the run (``Suspended``) until a human decides.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import jsonschema
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from relay.agents.workflows.spec import Step, WorkflowSpec
from relay.core.toolrouter import ToolRouter
from relay.llm.client import LLM
from relay.security.policy import Decision

_env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
_env.filters["tojson"] = lambda v: json.dumps(v, default=str)
AgentRunner = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class Suspended(Exception):  # noqa: N818 - control flow, not an error
    def __init__(self, step_id: str, request: dict[str, Any]) -> None:
        super().__init__(step_id)
        self.step_id, self.request = step_id, request


class StepFailed(Exception):
    pass


@dataclass
class WorkflowContext:
    event: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, Any] = field(default_factory=dict)
    approvals: dict[str, bool] = field(default_factory=dict)
    errors: list[dict[str, str]] = field(default_factory=list)

    def vars(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"event": self.event, "steps": self.steps, **(extra or {})}

    def to_state(self) -> dict[str, Any]:
        return {"event": self.event, "steps": self.steps, "approvals": self.approvals, "errors": self.errors}

    @classmethod
    def from_state(cls, d: dict[str, Any]) -> WorkflowContext:
        return cls(d.get("event", {}), d.get("steps", {}), d.get("approvals", {}), d.get("errors", []))


def render(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        if "{{" not in value and "{%" not in value:
            return value
        stripped = value.strip()
        # A pure "{{ expr }}" keeps its native type (list/dict/number) instead of becoming a string.
        if stripped.startswith("{{") and stripped.endswith("}}") and stripped.count("{{") == 1:
            return _env.compile_expression(stripped[2:-2].strip())(**variables)
        return _env.from_string(value).render(**variables)
    if isinstance(value, dict):
        return {k: render(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, variables) for v in value]
    return value


def truthy(expr: str, variables: dict[str, Any]) -> bool:
    e = expr.strip()
    if e.startswith("{{") and e.endswith("}}"):
        e = e[2:-2]
    return bool(_env.compile_expression(e)(**variables))


class WorkflowEngine:
    def __init__(self, spec: WorkflowSpec, *, router: ToolRouter, llm: LLM, run_agent: AgentRunner | None = None,
                 persist: Callable[[dict[str, Any]], Awaitable[None]] | None = None, tenant_id: str | None = None) -> None:
        self.spec, self.router, self.llm = spec, router, llm
        self.run_agent, self.persist, self.tenant_id = run_agent, persist, tenant_id

    async def run(self, ctx: WorkflowContext) -> dict[str, Any]:
        try:
            await self._steps(self.spec.steps, ctx, {})
            return {"status": "succeeded", "steps": ctx.steps}
        except Suspended as s:
            await self._save(ctx)
            return {"status": "suspended", "step": s.step_id, "request": s.request}
        except StepFailed as e:
            ctx.errors.append({"error": str(e)})
            if self.spec.on_error:
                try:
                    await self._steps(self.spec.on_error, ctx, {"error": str(e)})
                except Exception as he:  # handler failure must not hide the original error
                    ctx.errors.append({"handler_error": str(he)})
            await self._save(ctx)
            return {"status": "failed", "error": str(e), "steps": ctx.steps}

    async def _save(self, ctx: WorkflowContext) -> None:
        if self.persist:
            await self.persist(ctx.to_state())

    async def _steps(self, steps: list[Step], ctx: WorkflowContext, local: dict[str, Any], suffix: str = "") -> None:
        for step in steps:
            sid = step.id + suffix
            if sid in ctx.steps:  # completed before a suspend/crash: skip (idempotent resume)
                continue
            out = await self._with_retries(step, sid, ctx, local, suffix)
            ctx.steps[sid] = out
            if suffix == "":
                ctx.steps[step.id] = out
            await self._save(ctx)

    async def _with_retries(self, step: Step, sid: str, ctx: WorkflowContext, local: dict[str, Any],
                            suffix: str) -> Any:
        if step.approval or step.kind == "approval":
            decided = ctx.approvals.get(sid)
            if decided is None:
                raise Suspended(sid, {"kind": "workflow_step", "step": sid,
                                      "message": render(step.message or f"Approve step '{step.id}'?",
                                                        ctx.vars(local)),
                                      "preview": render(step.args, ctx.vars(local)) if step.args else None})
            if not decided:
                raise StepFailed(f"step '{sid}' rejected by approver")
            if step.kind == "approval":
                return {"approved": True}
        attempt = 0
        while True:
            try:
                return await asyncio.wait_for(self._exec(step, sid, ctx, local, suffix), timeout=step.timeout_s)
            except Suspended:
                raise
            except Exception as e:
                attempt += 1
                if attempt <= step.retries:
                    await asyncio.sleep(step.retry_delay_s * attempt)
                    continue
                if step.on_error == "continue":
                    ctx.errors.append({"step": sid, "error": str(e)[:500]})
                    return {"error": str(e)[:500]}
                raise StepFailed(f"step '{sid}' failed: {e}") from e

    async def _exec(self, step: Step, sid: str, ctx: WorkflowContext, local: dict[str, Any], suffix: str) -> Any:
        v = ctx.vars(local)
        if step.kind == "tool":
            args = render(step.args, v)
            call = {"id": sid, "type": "function", "function": {"name": step.tool, "arguments": json.dumps(args,
                                                                                                        default=str)}}
            planned = await self.router.plan([call])
            decided = ctx.approvals.get(sid)
            if planned[0].decision.decision == Decision.APPROVE and not step.approval:
                if decided is None:  # policy demands a human: suspend until decided
                    raise Suspended(sid, {"kind": "tool_calls", "calls": [planned[0].summary()]})
                if not decided:
                    raise StepFailed(f"step '{sid}' rejected by approver")
            approved = {sid} if (step.approval or decided) else set()
            res = await self.router.execute(planned, approved)
            content = res.messages[0]["content"]
            if res.denied or content.startswith(("Denied", "Not executed", "Tool error")):
                raise RuntimeError(content)
            return {"output": content}
        if step.kind == "llm":
            assert step.llm is not None
            payload = render(step.llm.input, v)
            rf = {"type": "json_schema", "json_schema": {"name": sid, "strict": False,
                                                           "schema": step.llm.output_schema}}
            resp = await self.llm.chat(step.llm.route, [
                {"role": "system", "content": render(step.llm.prompt, v)},
                {"role": "user", "content": json.dumps(payload, default=str)[:30000]}],
                max_tokens=step.llm.max_tokens, response_format=rf, tenant_id=self.tenant_id)
            self.router.budget.charge_llm(resp.tokens, resp.cost_usd)
            data = json.loads(resp.content)
            jsonschema.validate(data, step.llm.output_schema)
            return data
        if step.kind == "agent":
            if self.run_agent is None:
                raise RuntimeError("agent steps are not available in this context")
            return await self.run_agent(step.agent or "", render(step.input, v))
        if step.kind == "branch":
            branch = step.then if truthy(step.condition or "false", v) else step.otherwise
            await self._steps(branch, ctx, local, suffix)
            return {"branch": "then" if branch is step.then else "else"}
        if step.kind == "foreach":
            items = render("{{ " + (step.items or "[]").strip("{} ") + " }}", v)
            for i, item in enumerate(list(items or [])[:100]):
                await self._steps(step.steps, ctx, {**local, "item": item, "index": i}, f"{suffix}_{i}")
            return {"count": len(items or [])}
        return None

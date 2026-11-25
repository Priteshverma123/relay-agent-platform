"""Reusable, durable tool-use loop as a LangGraph state machine.

    agent ──tool calls──▶ gate ──▶ tools ──▶ agent ...
      │                    │ interrupt(approval)     │ stuck: re_plan | ask_human(interrupt) | abort
      └──no tool calls──▶ judge ──achieved──▶ finish
                                └─missing──▶ agent (once)

Design notes
* The approval gate is its own node: LangGraph re-executes the whole node on resume, so the node
  contains only deterministic policy evaluation + ``interrupt``; effects happen in ``tools``.
* The gate persists the evaluated decisions in state; ``tools`` executes exactly what the human
  saw and approved (no re-evaluation drift).
* Budget and taint are persisted in state and restored into the guard/taint objects on each node,
  so a crashed run resumes with correct accounting.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel

from relay.common.telemetry import GenAI, span
from relay.core.budget import BudgetGuard, Spent
from relay.core.condenser import condense, deterministic_summary
from relay.core.events import Event, EventSink, EventType
from relay.core.stuck import Recovery, StuckDetector
from relay.core.toolrouter import PlannedCall, ToolRouter
from relay.llm.client import LLM, structured
from relay.security.policy import Decision, PolicyDecision, ToolClass
from relay.security.analyzers import Risk


class LoopState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    planned: list[dict[str, Any]]
    approved: list[str]
    spent: dict[str, Any]
    tainted: bool
    taint_sources: list[str]
    judge_rounds: int
    turns: int
    status: str  # running | done | partial | aborted
    result: str
    ask_human: dict[str, Any] | None


class GoalVerdict(BaseModel):
    achieved: bool
    missing: list[str]


def _restore(state: LoopState, router: ToolRouter) -> None:
    sp = state.get("spent")
    if sp:
        g = router.budget.spent
        g.usd, g.tokens, g.steps, g.tool_calls = sp.get("usd", 0.0), sp.get("tokens", 0), sp.get("steps", 0), \
            sp.get("tool_calls", 0)
    if state.get("tainted"):
        router.taint.tainted = True
        router.taint.sources = list(state.get("taint_sources", []))


def _snapshot(router: ToolRouter) -> dict[str, Any]:
    s: Spent = router.budget.spent
    return {"spent": {"usd": s.usd, "tokens": s.tokens, "steps": s.steps, "tool_calls": s.tool_calls},
            "tainted": router.taint.tainted, "taint_sources": list(router.taint.sources)}


def _planned_from(d: dict[str, Any]) -> PlannedCall:
    pd = PolicyDecision(Decision(d["decision"]), ToolClass(d["class"]) if d.get("class") else None,
                        Risk[d["risk"]], list(d.get("reasons", [])))
    return PlannedCall(d["call_id"], d["tool"], d["args"], pd, d["key"])


def build_tool_loop(*, agent: str, llm: LLM, router: ToolRouter, tools: list[str], route: str = "answer",
                    tenant_id: str | None = None, max_turns: int = 60, goal_judge: bool = True,
                    task_for_judge: str = "",
                    summarize: Callable[[list[dict[str, Any]]], Awaitable[str]] | None = None,
                    context_max_tokens: int = 80_000, graph: StateGraph | None = None, entry: str | None = START,
                    exit_to: str = END, prefix: str = "") -> StateGraph:
    """Builds the loop into ``graph`` (or a new one). ``entry``: node that flows into the loop
    (None = caller wires the edge to ``f"{prefix}agent"``); ``exit_to``: where ``finish`` goes."""
    events: EventSink = router.events
    schemas = router.registry.schemas(tools)

    def n(name: str) -> str:
        return f"{prefix}{name}"

    async def summarizer(middle: list[dict[str, Any]]) -> str:
        if summarize is not None:
            return await summarize(middle)
        return deterministic_summary(middle)

    async def agent_node(state: LoopState) -> dict[str, Any]:
        _restore(state, router)
        msgs = list(state["messages"])
        st = router.budget.status()
        if st.exhausted or state.get("turns", 0) >= max_turns:
            reason = st.reason or "max_turns"
            await events.append(router.run_id, Event(EventType.BUDGET, {"exhausted": reason}))
            return {"status": "partial", "result": f"Stopped: budget exhausted ({reason}).", **_snapshot(router)}
        msgs, condensed = await condense(msgs, summarizer, max_tokens=context_max_tokens,
                                         keep_tokens=context_max_tokens // 3)
        if condensed:
            await events.append(router.run_id, Event(EventType.CONDENSED, {"messages": len(msgs)}))
        if st.fraction > 0.7:
            msgs.append({"role": "user", "content": router.budget.remaining_hint() + " Wrap up soon."})
        with span("agent.step", **{GenAI.AGENT_NAME: agent, GenAI.OPERATION: "chat"}):
            resp = await llm.chat(route, msgs, tools=schemas, max_tokens=4096, tenant_id=tenant_id)
        router.budget.charge_llm(resp.tokens, resp.cost_usd)
        msgs.append(resp.assistant_message())
        await events.append(router.run_id, Event(EventType.MESSAGE, {
            "content": resp.content, "tool_calls": [tc.get("function", {}).get("name") for tc in resp.tool_calls]},
            tokens=resp.tokens, cost_usd=resp.cost_usd))
        return {"messages": msgs, "planned": [], "approved": [], "turns": state.get("turns", 0) + 1,
                "result": resp.content if not resp.tool_calls else state.get("result", ""), **_snapshot(router)}

    def after_agent(state: LoopState) -> str:
        if state.get("status") in ("partial", "aborted"):
            return n("finish")
        last = state["messages"][-1]
        return n("gate") if last.get("tool_calls") else (n("judge") if goal_judge else n("finish"))

    async def gate_node(state: LoopState) -> dict[str, Any]:
        _restore(state, router)
        calls = state["messages"][-1].get("tool_calls") or []
        planned = await router.plan(calls)
        need = router.needs_approval(planned)
        approved: list[str] = []
        if need:
            decision = interrupt({"kind": "tool_calls", "calls": [p.summary() for p in need],
                                  "tainted": router.taint.tainted, "taint_sources": router.taint.sources})
            approved = [c for c in (decision or {}).get("approved", []) if c in {p.call_id for p in need}]
        return {"planned": [{**p.summary(), "key": p.key} for p in planned], "approved": approved}

    async def tools_node(state: LoopState) -> dict[str, Any]:
        _restore(state, router)
        planned = [_planned_from(d) for d in state.get("planned", [])]
        res = await router.execute(planned, set(state.get("approved", [])))
        msgs = [*state["messages"], *res.messages]
        update: dict[str, Any] = {"messages": msgs, "ask_human": None}
        if res.stuck is not None:
            await events.append(router.run_id, Event(EventType.STUCK, {"kind": res.stuck.kind.value,
                                                                       "detail": res.stuck.detail,
                                                                       "recovery": res.stuck.recovery.value}))
            if res.stuck.recovery == Recovery.RE_PLAN:
                msgs.append({"role": "user", "content": StuckDetector.reflection(res.stuck)})
            elif res.stuck.recovery == Recovery.ASK_HUMAN:
                update["ask_human"] = {"kind": res.stuck.kind.value, "detail": res.stuck.detail}
            else:
                update.update(status="aborted", result=f"Aborted: agent stuck ({res.stuck.detail}).")
        return {**update, **_snapshot(router)}

    def after_tools(state: LoopState) -> str:
        if state.get("status") == "aborted":
            return n("finish")
        return n("human_help") if state.get("ask_human") else n("agent")

    async def human_help_node(state: LoopState) -> dict[str, Any]:
        reply = interrupt({"kind": "stuck", **(state.get("ask_human") or {})})
        reply = reply or {}
        if reply.get("abort"):
            return {"status": "aborted", "result": "Aborted by human after the agent got stuck.", "ask_human": None}
        hint = reply.get("hint") or "No hint provided; try a different approach."
        return {"messages": [*state["messages"], {"role": "user", "content": f"Human guidance: {hint}"}],
                "ask_human": None}

    def after_help(state: LoopState) -> str:
        return n("finish") if state.get("status") == "aborted" else n("agent")

    async def judge_node(state: LoopState) -> dict[str, Any]:
        if state.get("judge_rounds", 0) >= 1 or not task_for_judge:
            return {"status": "done"}
        verdict, resp = await structured(llm, "judge", [
            {"role": "system", "content": "Judge whether the agent achieved the task given its final message. "
                                          "List concrete missing items if not."},
            {"role": "user", "content": f"Task:\n{task_for_judge}\n\nFinal message:\n{state.get('result', '')}"}],
            GoalVerdict, max_tokens=300, tenant_id=tenant_id)
        router.budget.charge_llm(resp.tokens, resp.cost_usd)
        if verdict.achieved or not verdict.missing:
            return {"status": "done", "judge_rounds": 1, **_snapshot(router)}
        return {"judge_rounds": 1, **_snapshot(router), "messages": [*state["messages"], {
            "role": "user", "content": "Not finished yet. Missing:\n- " + "\n- ".join(verdict.missing[:5])}]}

    def after_judge(state: LoopState) -> str:
        return n("finish") if state.get("status") == "done" else n("agent")

    async def finish_node(state: LoopState) -> dict[str, Any]:
        status = state.get("status") or "done"
        if status == "running":
            status = "done"
        await events.append(router.run_id, Event(EventType.RUN_FINISHED, {"status": status,
                                                                          "result": state.get("result", "")[:4000]}))
        return {"status": status}

    g = graph if graph is not None else StateGraph(LoopState)
    for name, fn in (("agent", agent_node), ("gate", gate_node), ("tools", tools_node),
                     ("human_help", human_help_node), ("judge", judge_node), ("finish", finish_node)):
        g.add_node(n(name), fn)
    if entry is not None:
        g.add_edge(entry, n("agent"))
    g.add_conditional_edges(n("agent"), after_agent, [n("gate"), n("judge"), n("finish")])
    g.add_edge(n("gate"), n("tools"))
    g.add_conditional_edges(n("tools"), after_tools, [n("agent"), n("human_help"), n("finish")])
    g.add_conditional_edges(n("human_help"), after_help, [n("agent"), n("finish")])
    g.add_conditional_edges(n("judge"), after_judge, [n("agent"), n("finish")])
    g.add_edge(n("finish"), exit_to)
    return g

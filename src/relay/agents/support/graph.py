"""Customer Support Agent (Slack / web widget / helpdesk webhook).

    quarantine ─▶ route ─┬─ question ─▶ answer (Atlas RAG) ─▶ confidence gate ─┬─ high ─▶ reply
                         │                                                    └─ low  ─▶ draft → human approval → reply
                         ├─ bug_report ─▶ create Jira ticket ─▶ acknowledge
                         └─ account_action | out_of_scope ─▶ escalate (ticket + handoff message)

Dual-LLM: the customer's raw message is read ONLY by the quarantined extractor (no tools). The
routing/answering side sees the validated fields. The agent has no account-mutating tools at all:
refunds, deletions, etc. are always escalated to humans.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from relay.core.events import Event, EventSink, EventType
from relay.core.idempotency import IdempotencyStore, idempotency_key
from relay.llm.client import LLM
from relay.security.policy import Decision, PolicyEngine
from relay.security.quarantine import quarantined_extract


class Ticket(BaseModel):
    intent: Literal["question", "bug_report", "account_action", "out_of_scope"]
    question: str = Field(description="the customer's question restated neutrally, max 300 chars")
    product_area: str
    details: str = Field(description="repro steps / environment / error messages, if any")
    urgency: Literal["low", "normal", "high"]


class SupportState(TypedDict, total=False):
    channel: str
    thread_id: str
    customer: str
    raw_message: str
    ticket: dict[str, Any]
    answer: str
    citations: list[dict[str, Any]]
    confidence: float
    reply: str
    jira_key: str
    approved: bool
    status: str
    result: str


def confidence_of(atlas_result: dict[str, Any]) -> float:
    """Grounding-based confidence: abstained → 0; else support ratio × citation presence."""
    if atlas_result.get("abstained") or not atlas_result.get("citations"):
        return 0.0
    return float((atlas_result.get("verification") or {}).get("supported_ratio", 0.0))


def build_support_graph(*, llm: LLM, atlas: Any, jira: Any, slack: Any, engine: PolicyEngine, events: EventSink,
                        idem: IdempotencyStore, run_id: uuid.UUID, tenant_id: str, threshold: float = 0.8,
                        dry_run: bool = False) -> StateGraph:
    async def quarantine(s: SupportState) -> dict[str, Any]:
        t = await quarantined_extract(llm, s["raw_message"], Ticket, source=f"customer:{s.get('channel')}",
                                      tenant_id=tenant_id, cap=1500)
        await events.append(run_id, Event(EventType.TAINTED, {"source": "customer_message"}))
        await events.append(run_id, Event(EventType.PHASE, {"phase": "triage", "intent": t.intent,
                                                            "urgency": t.urgency}))
        return {"ticket": t.model_dump()}

    def route(s: SupportState) -> str:
        return {"question": "answer", "bug_report": "ticket"}.get(s["ticket"]["intent"], "escalate")

    async def answer(s: SupportState) -> dict[str, Any]:
        res = await atlas.query(s["ticket"]["question"])
        conf = confidence_of(res)
        await events.append(run_id, Event(EventType.OBSERVATION, {"tool": "atlas.query", "confidence": conf,
                                                                  "citations": len(res.get("citations", []))}))
        sources = "\n".join(f"- {c.get('title')} (p.{c.get('page_start')})" for c in res.get("citations", [])[:4])
        reply = f"{res.get('answer', '')}\n\nSources:\n{sources}" if sources else res.get("answer", "")
        return {"answer": res.get("answer", ""), "citations": res.get("citations", []), "confidence": conf,
                "reply": reply}

    async def reply_gate(s: SupportState) -> dict[str, Any]:
        d = await engine.evaluate("reply_customer", {"channel": s.get("channel"), "text": s["reply"]}, tainted=True)
        low = s.get("confidence", 0.0) < threshold
        if d.decision == Decision.DENY:
            return {"approved": False, "result": "reply denied by policy: " + "; ".join(d.reasons)}
        if (low or d.decision == Decision.APPROVE) and not dry_run:
            r = interrupt({"kind": "tool_calls", "calls": [{
                "call_id": "reply_customer", "tool": "reply_customer", "args": {"draft": s["reply"][:3000]},
                **d.to_dict(), "reasons": [*d.reasons, *(["low grounding confidence"] if low else [])]}],
                "tainted": True, "taint_sources": ["customer_message"]})
            return {"approved": "reply_customer" in (r or {}).get("approved", [])}
        return {"approved": not dry_run}

    async def send_reply(s: SupportState) -> dict[str, Any]:
        if not s.get("approved"):
            return {"status": "done", "result": s.get("result") or "reply not sent (needs human)"}
        key = idempotency_key(run_id, "reply_customer", {"thread": s.get("thread_id"), "text": s["reply"]})
        if await idem.get(key) is None:
            await slack.post(s["channel"], s["reply"], thread_ts=s.get("thread_id"))
            await idem.put(key, {"sent": True})
        return {"status": "done", "result": "replied"}

    async def ticket(s: SupportState) -> dict[str, Any]:
        t = s["ticket"]
        args = {"summary": f"[{t['product_area']}] {t['question'][:120]}", "description": t["details"]}
        d = await engine.evaluate("create_ticket", args, tainted=True)
        if d.decision == Decision.DENY or dry_run:
            return {"status": "done", "result": "ticket not created"}
        key = idempotency_key(run_id, "create_ticket", args)
        cached = await idem.get(key)
        if cached is None:
            issue = await jira.create_issue(args["summary"], f"Reported via {s.get('channel')}\n\n{args['description']}",
                                            issue_type="Bug", labels=["relay", "support", t["urgency"]])
            cached = {"key": issue.get("key")}
            await idem.put(key, cached)
        ack = f"Thanks, I've logged this as {cached['key']}. Our team will follow up."
        return {"jira_key": cached["key"], "reply": ack, "confidence": 1.0}

    async def escalate(s: SupportState) -> dict[str, Any]:
        out = await ticket(s)
        out["reply"] = "I've passed this to a member of our team, who will get back to you shortly."
        return out

    g = StateGraph(SupportState)
    for name, fn in (("quarantine", quarantine), ("answer", answer), ("ticket", ticket), ("escalate", escalate),
                     ("reply_gate", reply_gate), ("send_reply", send_reply)):
        g.add_node(name, fn)
    g.add_edge(START, "quarantine")
    g.add_conditional_edges("quarantine", route, ["answer", "ticket", "escalate"])
    for n in ("answer", "ticket", "escalate"):
        g.add_edge(n, "reply_gate")
    g.add_edge("reply_gate", "send_reply")
    g.add_edge("send_reply", END)
    return g

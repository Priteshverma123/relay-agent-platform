"""Meeting agent graph: transcribe → extract (map-reduce) → propose writes → approval gate → execute."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from relay.agents.meetings.pipeline import MeetingSummary, Utterance, extract, plan_writes, segment, transcribe
from relay.core.events import Event, EventType
from relay.core.toolrouter import ToolRouter
from relay.llm.client import LLM


class MeetingState(TypedDict, total=False):
    audio_path: str
    transcript: list[dict[str, Any]]
    attendees: list[str]
    summary: dict[str, Any]
    planned: list[dict[str, Any]]
    approved: list[str]
    tainted: bool
    status: str
    result: str


def build_meeting_graph(*, llm: LLM, router: ToolRouter, tenant_id: str, hf_token: str | None = None) -> StateGraph:
    events, run_id = router.events, router.run_id

    async def asr(s: MeetingState) -> dict[str, Any]:
        if s.get("transcript"):
            return {}
        utts = await asyncio.to_thread(transcribe, s["audio_path"], hf_token=hf_token)
        router.taint.mark("meeting_audio")
        await events.append(run_id, Event(EventType.PHASE, {"phase": "transcribed", "utterances": len(utts)}))
        return {"transcript": [dataclasses.asdict(u) for u in utts], "tainted": True}

    async def notes(s: MeetingState) -> dict[str, Any]:
        utts = [Utterance(**u) for u in s["transcript"]]
        m = await extract(llm, segment(utts), s.get("attendees", []), tenant_id=tenant_id)
        await events.append(run_id, Event(EventType.PHASE, {"phase": "extracted", "action_items": len(m.action_items),
                                                            "decisions": len(m.decisions)}))
        return {"summary": m.model_dump()}

    async def gate(s: MeetingState) -> dict[str, Any]:
        m = MeetingSummary.model_validate(s["summary"])
        router.taint.mark("meeting_audio")  # restored after resume: meeting content is always untrusted
        calls = [{"id": f"w{i}", "type": "function", "function": {"name": c["tool"],
                                                                  "arguments": json.dumps(c["args"])}}
                 for i, c in enumerate(plan_writes(m))]
        planned = await router.plan(calls)
        need = router.needs_approval(planned)
        approved: list[str] = []
        if need:
            reply = interrupt({"kind": "tool_calls", "calls": [p.summary() for p in need], "tainted": True,
                               "taint_sources": ["meeting_audio"]})
            approved = list((reply or {}).get("approved", []))
        return {"planned": [{**p.summary(), "key": p.key} for p in planned], "approved": approved}

    async def execute(s: MeetingState) -> dict[str, Any]:
        from relay.core.agent_loop import _planned_from

        res = await router.execute([_planned_from(d) for d in s.get("planned", [])], set(s.get("approved", [])))
        done = sum(1 for m in res.messages if not str(m["content"]).startswith(("Denied", "Not executed")))
        return {"status": "done", "result": f"{done}/{len(res.messages)} writes executed"}

    g = StateGraph(MeetingState)
    for name, fn in (("asr", asr), ("notes", notes), ("gate", gate), ("execute", execute)):
        g.add_node(name, fn)
    g.add_edge(START, "asr")
    g.add_edge("asr", "notes")
    g.add_edge("notes", "gate")
    g.add_edge("gate", "execute")
    g.add_edge("execute", END)
    return g


def _new() -> uuid.UUID:
    return uuid.uuid4()

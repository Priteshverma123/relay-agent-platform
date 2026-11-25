"""Background ('sleep-time') memory consolidation, run after each finished run and nightly.

1. Summarise the run's event log into an episode (+ lessons) with the fast route.
2. Propose candidate facts (e.g. "repo X runs tests with `make test`") → write gate applies.
3. Merge near-duplicate facts per scope (cosine ≥ 0.92 → older invalidated, newer kept).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field
from sqlalchemy import select

from relay.db.models import MemoryFact, Run, RunEvent
from relay.db.session import tenant_session
from relay.llm.client import LLM, structured
from relay.memory.embed import cosine
from relay.memory.store import MemoryStore, MemoryWriteRefused


class RunDigest(BaseModel):
    summary: str = Field(description="what was attempted and what worked, 3-5 sentences")
    lessons: list[str] = Field(description="reusable lessons, max 3")
    facts: list[str] = Field(description="durable facts about the repository/environment (build commands, "
                                         "conventions); max 3; empty if none")


async def consolidate_run(llm: LLM, memory: MemoryStore, tenant_id: str, run_id: uuid.UUID) -> dict[str, int]:
    async with tenant_session(tenant_id) as s:
        run = await s.get(Run, run_id)
        events = list((await s.execute(select(RunEvent).where(RunEvent.run_id == run_id)
                                       .order_by(RunEvent.seq).limit(400))).scalars())
    if run is None:
        return {}
    trace = "\n".join(f"{e.seq} {e.type}: {str(e.payload)[:300]}" for e in events)
    digest, _ = await structured(llm, "fast", [
        {"role": "system", "content": "Summarise an agent run for future reuse. Be concrete."},
        {"role": "user", "content": f"Agent: {run.agent}\nStatus: {run.status}\nTrigger: {str(run.trigger)[:800]}\n"
                                    f"Events:\n{trace[-20000:]}"}], RunDigest, max_tokens=600, tenant_id=tenant_id)
    await memory.add_episode(tenant_id, run.agent, run_id, str(run.trigger)[:2000], digest.summary, run.status,
                             digest.lessons, float(run.spent.get("usd", 0.0)), run.tainted)
    scope = f"repo:{run.trigger.get('repo')}" if run.trigger.get("repo") else f"agent:{run.agent}"
    stored = refused = 0
    for f in digest.facts[:3]:
        try:
            await memory.add_fact(tenant_id, scope, f, run_id=run_id, event_seq=None, tainted=run.tainted)
            stored += 1
        except MemoryWriteRefused:
            refused += 1
    merged = await dedupe_scope(memory, tenant_id, scope)
    return {"facts_stored": stored, "facts_refused": refused, "merged": merged}


async def dedupe_scope(memory: MemoryStore, tenant_id: str, scope: str, threshold: float = 0.92) -> int:
    async with tenant_session(tenant_id) as s:
        facts = list((await s.execute(select(MemoryFact).where(MemoryFact.scope == scope,
                                                               MemoryFact.invalid_at.is_(None))
                                      .order_by(MemoryFact.valid_from.desc()))).scalars())
    dead: set[uuid.UUID] = set()
    for i, newer in enumerate(facts):
        if newer.id in dead or newer.embedding is None:
            continue
        for older in facts[i + 1:]:
            if older.id not in dead and older.embedding is not None and \
                    cosine(list(newer.embedding), list(older.embedding)) >= threshold:
                await memory.invalidate(tenant_id, [older.id], newer.id)
                dead.add(older.id)
    return len(dead)

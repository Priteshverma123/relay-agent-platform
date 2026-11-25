"""Agent memory (four kinds) on Postgres + pgvector.

  working    : LangGraph state + condenser (core/agent_loop.py, core/condenser.py)
  episodic   : one summary per finished run; similar past episodes are injected as few-shot
               experience ("last time a flaky test looked like this, the fix was ...")
  semantic   : durable facts scoped to a repo/agent/user, bi-temporal (valid_from/invalid_at),
               with provenance (run + event seq) and a taint label
  procedural : versioned playbooks (instructions) per agent; a candidate becomes ``active`` only
               after its eval run passes (see evals/ and ``promote_playbook``)

Write gate (OWASP ASI06 memory poisoning): facts derived from a tainted run are refused unless
explicitly confirmed by a human; episodic summaries from tainted runs are stored but flagged and
excluded from few-shot retrieval.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from relay.common.logging import get_logger
from relay.db.models import Episode, MemoryFact, Playbook
from relay.db.session import tenant_session
from relay.memory.embed import Embedder

log = get_logger(__name__)


class MemoryWriteRefused(Exception):
    pass


class MemoryStore:
    def __init__(self, embedder: Embedder) -> None:
        self.embedder = embedder

    # ------------------------------------------------------------------ semantic
    async def add_fact(self, tenant_id: str, scope: str, fact: str, *, run_id: uuid.UUID | None,
                       event_seq: int | None, tainted: bool, confidence: float = 0.8,
                       human_confirmed: bool = False) -> uuid.UUID:
        if tainted and not human_confirmed:
            raise MemoryWriteRefused("fact derived from untrusted content; needs human confirmation")
        vec = (await self.embedder.embed([fact]))[0]
        fid = uuid.uuid4()
        async with tenant_session(tenant_id) as s:
            s.add(MemoryFact(id=fid, tenant_id=uuid.UUID(tenant_id), scope=scope, fact=fact[:1000], embedding=vec,
                             source_run_id=run_id, source_event_seq=event_seq, tainted=tainted,
                             confidence=confidence))
        return fid

    async def facts(self, tenant_id: str, scope: str, query: str, k: int = 5) -> list[str]:
        vec = (await self.embedder.embed([query]))[0]
        async with tenant_session(tenant_id) as s:
            rows = (await s.execute(
                select(MemoryFact.fact).where(MemoryFact.scope == scope, MemoryFact.invalid_at.is_(None))
                .order_by(MemoryFact.embedding.cosine_distance(vec)).limit(k))).scalars()
            return list(rows)

    async def invalidate(self, tenant_id: str, fact_ids: list[uuid.UUID], superseded_by: uuid.UUID | None) -> None:
        async with tenant_session(tenant_id) as s:
            await s.execute(update(MemoryFact).where(MemoryFact.id.in_(fact_ids), MemoryFact.invalid_at.is_(None))
                            .values(invalid_at=datetime.now(UTC), superseded_by=superseded_by))

    # ------------------------------------------------------------------ episodic
    async def add_episode(self, tenant_id: str, agent: str, run_id: uuid.UUID, task: str, summary: str,
                          outcome: str, lessons: list[str], cost_usd: float, tainted: bool) -> None:
        vec = (await self.embedder.embed([task]))[0]
        async with tenant_session(tenant_id) as s:
            s.add(Episode(tenant_id=uuid.UUID(tenant_id), agent=agent, run_id=run_id, task=task[:4000],
                          summary=summary[:4000], outcome=("tainted:" if tainted else "") + outcome,
                          lessons=lessons[:10], cost_usd=cost_usd, embedding=vec))

    async def similar_episodes(self, tenant_id: str, agent: str, task: str, k: int = 2) -> list[str]:
        vec = (await self.embedder.embed([task]))[0]
        async with tenant_session(tenant_id) as s:
            rows = (await s.execute(
                select(Episode).where(Episode.agent == agent, Episode.outcome.in_(["succeeded"]))
                .order_by(Episode.embedding.cosine_distance(vec)).limit(k))).scalars()
            return [f"Task: {e.task[:500]}\nOutcome: {e.outcome}\nWhat worked: {e.summary[:800]}\n"
                    f"Lessons: {'; '.join(e.lessons)}" for e in rows]

    # ------------------------------------------------------------------ procedural
    async def active_playbook(self, tenant_id: str, agent: str) -> tuple[int, str] | None:
        async with tenant_session(tenant_id) as s:
            pb = (await s.execute(select(Playbook).where(Playbook.agent == agent, Playbook.status == "active")
                                  .order_by(Playbook.version.desc()).limit(1))).scalar_one_or_none()
        return (pb.version, pb.content) if pb else None

    async def propose_playbook(self, tenant_id: str, agent: str, content: str) -> int:
        async with tenant_session(tenant_id) as s:
            latest = (await s.execute(select(Playbook.version).where(Playbook.agent == agent)
                                      .order_by(Playbook.version.desc()).limit(1))).scalar_one_or_none() or 0
            s.add(Playbook(tenant_id=uuid.UUID(tenant_id), agent=agent, version=latest + 1, content=content))
        return latest + 1

    async def promote_playbook(self, tenant_id: str, agent: str, version: int, eval_report: dict[str, Any],
                               *, passed: bool) -> bool:
        """Eval-gated promotion: a playbook that didn't pass its eval can never become active."""
        async with tenant_session(tenant_id) as s:
            pb = (await s.execute(select(Playbook).where(Playbook.agent == agent, Playbook.version == version))
                  ).scalar_one_or_none()
            if pb is None:
                return False
            pb.eval_report = eval_report
            if not passed:
                pb.status = "retired"
                return False
            await s.execute(update(Playbook).where(Playbook.agent == agent, Playbook.status == "active")
                            .values(status="retired"))
            pb.status = "active"
        return True

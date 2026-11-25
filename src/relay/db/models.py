"""Relay schema. Tenant-owned tables carry ``tenant_id`` and are RLS-protected (migration 0001).

Core idea: ``run_events`` is the append-only source of truth for what an agent did; ``runs``
holds derived status/spend for fast queries; ``tool_calls`` carries idempotency keys so a resumed
run never repeats a side effect; ``approvals`` + ``audit_log`` give a complete human-oversight trail.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EMBED_DIM = 384


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[str]: ARRAY(String), uuid.UUID: UUID(as_uuid=True)}


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _tenant() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), index=True)


class AgentConfig(Base):
    """Versioned agent definitions; a run pins the version it started with."""

    __tablename__ = "agent_configs"
    __table_args__ = (UniqueConstraint("tenant_id", "name", "version"),)
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    model_policy: Mapped[dict[str, Any]] = mapped_column(default=dict)
    tool_policy: Mapped[dict[str, Any]] = mapped_column(default=dict)
    budget_defaults: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _ts()


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_tenant_status", "tenant_id", "status"),)
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    agent: Mapped[str] = mapped_column(String(100))
    agent_version: Mapped[int] = mapped_column(Integer, default=1)
    trigger: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    # queued | running | waiting_approval | suspended | succeeded | failed | budget_exceeded | cancelled
    tainted: Mapped[bool] = mapped_column(default=False)
    budget: Mapped[dict[str, Any]] = mapped_column(default=dict)
    spent: Mapped[dict[str, Any]] = mapped_column(default=dict)
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)  # workflow engine state / resumable cursor
    outcome: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    trace_id: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    """Append-only, typed, redacted event log (message | action | observation | approval | ...)."""

    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = _tenant()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = _ts()


class ToolCall(Base):
    __tablename__ = "tool_calls"
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    tool: Mapped[str] = mapped_column(String(100))
    tool_class: Mapped[str] = mapped_column(String(20))
    args: Mapped[dict[str, Any]] = mapped_column(default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|done|failed|denied
    decision: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _ts()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # tool_calls | plan | stuck | workflow_step
    request: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|rejected|expired
    channel: Mapped[str] = mapped_column(String(50), default="api")
    decided_by: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[uuid.UUID] = _tenant()
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(300))
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    ts: Mapped[datetime] = _ts()


class WebhookDelivery(Base):
    """Dedup table: webhook providers retry; the delivery id makes intake idempotent."""

    __tablename__ = "webhook_deliveries"
    delivery_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    source: Mapped[str] = mapped_column(String(30))
    tenant_id: Mapped[uuid.UUID] = _tenant()
    event: Mapped[str] = mapped_column(String(100))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    received_at: Mapped[datetime] = _ts()


class Workflow(Base):
    __tablename__ = "workflows"
    __table_args__ = (UniqueConstraint("tenant_id", "name", "version"),)
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    spec: Mapped[dict[str, Any]] = mapped_column(default=dict)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = _ts()


# --------------------------------------------------------------------------- memory
class MemoryFact(Base):
    """Semantic memory: bi-temporal facts with provenance and a taint label."""

    __tablename__ = "memory_facts"
    __table_args__ = (Index("ix_facts_scope", "tenant_id", "scope", "invalid_at"),)
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    scope: Mapped[str] = mapped_column(String(200))  # e.g. "repo:org/name", "agent:review", "user:alice"
    fact: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_event_seq: Mapped[int | None] = mapped_column(Integer)
    tainted: Mapped[bool] = mapped_column(default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.8)
    valid_from: Mapped[datetime] = _ts()
    invalid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class Episode(Base):
    """Episodic memory: one summary per finished run, retrieved as few-shot experience."""

    __tablename__ = "episodes"
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    agent: Mapped[str] = mapped_column(String(100), index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    task: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(30))
    lessons: Mapped[list[str]] = mapped_column(default=list)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    created_at: Mapped[datetime] = _ts()


class Playbook(Base):
    """Procedural memory: versioned instructions per agent; only ``active`` after an eval pass."""

    __tablename__ = "playbooks"
    __table_args__ = (UniqueConstraint("tenant_id", "agent", "version"),)
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    agent: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="candidate")  # candidate | active | retired
    eval_report: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = _ts()


class ReviewFeedback(Base):
    """Online quality signal for the review agent (acceptance rate)."""

    __tablename__ = "review_feedback"
    id: Mapped[uuid.UUID] = _pk()
    tenant_id: Mapped[uuid.UUID] = _tenant()
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    fingerprint: Mapped[str] = mapped_column(String(64))
    signal: Mapped[str] = mapped_column(String(20))  # thumbs_up | thumbs_down | resolved | line_changed
    created_at: Mapped[datetime] = _ts()


TENANT_TABLES = ["agent_configs", "runs", "run_events", "tool_calls", "approvals", "audit_log", "workflows",
                 "memory_facts", "episodes", "playbooks", "review_feedback"]

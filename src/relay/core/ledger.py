"""Durable tool-call ledger: one row per (idempotency key), insert-once."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from relay.common.logging import redact
from relay.common.text import head_tail
from relay.db.models import ToolCall
from relay.db.session import tenant_session


def postgres_ledger(tenant_id: str, run_id: uuid.UUID) -> Any:
    async def write(p: Any, r: Any, status: str) -> None:
        async with tenant_session(tenant_id) as s:
            await s.execute(pg_insert(ToolCall).values(
                id=uuid.uuid4(), tenant_id=uuid.UUID(tenant_id), run_id=run_id, tool=p.tool,
                tool_class=p.decision.tool_class.value if p.decision.tool_class else "unknown",
                args=redact(p.args), idempotency_key=p.key, status=status, decision=p.decision.to_dict(),
                result={"output": head_tail(redact(r.output), 4000), "is_error": r.is_error},
                finished_at=datetime.now(UTC)).on_conflict_do_nothing(index_elements=["idempotency_key"]))
    return write

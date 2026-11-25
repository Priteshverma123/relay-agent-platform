"""arq workers.

    arq relay.workers.main.AgentWorker   # runs/resumes agent graphs (needs Docker socket for sandboxes)
    arq relay.workers.main.LightWorker   # memory consolidation, cron workflows, janitor

Job ids are idempotency keys (``run:<id>:<resume-n>``) so a double-enqueue doesn't run twice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from arq import cron
from arq.connections import RedisSettings
from arq.worker import func

from relay.common.logging import configure_logging, get_logger
from relay.common.settings import get_settings
from relay.common.telemetry import setup_telemetry
from relay.memory.consolidation import consolidate_run
from relay.runtime import AgentRuntime

log = get_logger(__name__)
AGENTS_Q, LIGHT_Q = "relay:agents", "relay:light"
DEFAULT_TENANT = "00000000-0000-0000-0000-000000000001"


async def startup(ctx: dict[str, Any]) -> None:
    s = get_settings()
    configure_logging(s.log_level)
    setup_telemetry(f"{s.service_name}-worker", s.otlp_endpoint)
    ctx["rt"] = AgentRuntime(s)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["rt"].aclose()


async def execute_run(ctx: dict[str, Any], tenant_id: str, run_id: str, resume: Any = None) -> dict[str, Any]:
    res = await ctx["rt"].execute(tenant_id, uuid.UUID(run_id), resume)
    if res.get("status") not in ("waiting_approval",):
        await ctx["redis"].enqueue_job("consolidate", tenant_id, run_id, _queue_name=LIGHT_Q,
                                       _job_id=f"consolidate:{run_id}")
    return res


async def consolidate(ctx: dict[str, Any], tenant_id: str, run_id: str) -> dict[str, int]:
    rt: AgentRuntime = ctx["rt"]
    return await consolidate_run(rt.llm, rt.memory, tenant_id, uuid.UUID(run_id))


def cron_match(expr: str, t: datetime) -> bool:
    """Minimal 5-field cron matcher (*, a-b, a,b, */n). Enough for operator-written schedules."""
    vals = [t.minute, t.hour, t.day, t.month, (t.isoweekday() % 7)]
    for field, v in zip(expr.split(), vals, strict=True):
        ok = False
        for part in field.split(","):
            step = 1
            if "/" in part:
                part, s = part.split("/")
                step = int(s)
            if part == "*":
                ok = ok or v % step == 0
            elif "-" in part:
                a, b = (int(x) for x in part.split("-"))
                ok = ok or (a <= v <= b and (v - a) % step == 0)
            else:
                ok = ok or v == int(part)
        if not ok:
            return False
    return True


async def cron_workflows(ctx: dict[str, Any]) -> int:
    rt: AgentRuntime = ctx["rt"]
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    n = 0
    for spec in rt.workflows.values():
        if spec.on.cron and cron_match(spec.on.cron, now):
            rid = await rt.start(DEFAULT_TENANT, "workflows", {"workflow": spec.name,
                                                               "event": {"since": now.date().isoformat()}})
            await ctx["redis"].enqueue_job("execute_run", DEFAULT_TENANT, str(rid), _queue_name=AGENTS_Q,
                                           _job_id=f"cron:{spec.name}:{now.isoformat()}")
            n += 1
    return n


async def sandbox_janitor(ctx: dict[str, Any]) -> int:
    try:
        return await ctx["rt"].sandbox_provider().janitor()  # type: ignore[no-any-return]
    except Exception:
        log.warning("janitor_failed", exc_info=True)
        return 0


class _Base:
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    keep_result = 3600


class AgentWorker(_Base):
    queue_name = AGENTS_Q
    functions = [func(execute_run, name="execute_run", timeout=3600, max_tries=1)]  # graphs checkpoint; no blind retry
    max_jobs = 4


class LightWorker(_Base):
    queue_name = LIGHT_Q
    functions = [func(consolidate, name="consolidate", timeout=600, max_tries=3)]
    cron_jobs = [cron(cron_workflows, minute=set(range(60)), run_at_startup=False),
                 cron(sandbox_janitor, minute={7, 37})]
    max_jobs = 8

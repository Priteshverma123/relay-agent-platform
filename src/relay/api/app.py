"""relay-api: webhooks intake, runs, approvals, workflows, run viewer + SSE."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from relay.api.routers import approvals, runs, webhooks, workflows
from relay.common.errors import RelayError, Unauthorized
from relay.common.logging import configure_logging, get_logger
from relay.common.settings import get_settings
from relay.common.telemetry import setup_telemetry
from relay.db.apikeys import resolve_key
from relay.db.session import get_engine
from relay.runtime import AgentRuntime

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    s = get_settings()
    configure_logging(s.log_level)
    setup_telemetry(f"{s.service_name}-api", s.otlp_endpoint)
    app.state.rt = AgentRuntime(s)
    app.state.arq = await create_pool(RedisSettings.from_dsn(s.redis_url))
    yield
    await app.state.arq.aclose()
    await app.state.rt.aclose()


app = FastAPI(title="Relay", version="0.1.0", lifespan=lifespan,
              description="Durable, policy-enforced engineering agents.")


@app.exception_handler(RelayError)
async def relay_error(_: Request, exc: RelayError) -> JSONResponse:
    return JSONResponse({"type": f"https://relay.dev/errors/{exc.code}", "title": exc.code, "status": exc.status_code,
                         "detail": exc.message, **({"details": exc.details} if exc.details else {})},
                        status_code=exc.status_code, media_type="application/problem+json")


async def tenant(authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("missing bearer token")
    t = resolve_key(authorization[7:].strip())
    if t is None:
        raise Unauthorized("invalid api key")
    return t


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    checks: dict[str, str] = {}
    try:
        async with get_engine().connect() as c:
            await c.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = type(e).__name__
    try:
        await request.app.state.rt.redis.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = type(e).__name__
    checks["embeddings"] = "degraded" if request.app.state.rt.memory.embedder.degraded else "ok"
    ok = checks["postgres"] == "ok" and checks["redis"] == "ok"
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks}, status_code=200 if ok else 503)


@app.get("/metrics", include_in_schema=False)
async def metrics() -> PlainTextResponse:
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


Tenant = Annotated[str, Depends(tenant)]
app.include_router(webhooks.router)
for r in (runs.router, approvals.router, workflows.router):
    app.include_router(r, dependencies=[Depends(tenant)])


def tenant_dep() -> Any:
    return Depends(tenant)

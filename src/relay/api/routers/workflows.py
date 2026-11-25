"""Workflows: list built-ins, lint specs, register custom specs (versioned), trigger manually."""

from __future__ import annotations

import uuid
from typing import Any

import yaml
from fastapi import APIRouter, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select

from relay.agents.workflows.spec import WorkflowSpec, json_schema, lint
from relay.api.deps import tenant_of
from relay.common.errors import InvalidInput, NotFound
from relay.db.models import Workflow
from relay.db.session import tenant_session
from relay.runtime import AGENTS
from relay.tools.integration_tools import integration_tools
from relay.workers.main import AGENTS_Q

router = APIRouter(prefix="/v1/workflows", tags=["workflows"])
KNOWN_TOOLS = {t.name for t in integration_tools()}


class SpecBody(BaseModel):
    yaml: str


def _parse(text: str) -> WorkflowSpec:
    try:
        return WorkflowSpec.model_validate(yaml.safe_load(text))
    except (ValidationError, yaml.YAMLError) as exc:
        raise InvalidInput("invalid workflow spec", details={"errors": str(exc)[:2000]}) from exc


@router.get("/schema")
async def schema() -> dict[str, Any]:
    return json_schema()


@router.get("")
async def list_workflows(request: Request) -> list[dict[str, Any]]:
    out = [{"name": s.name, "description": s.description, "trigger": s.on.model_dump(exclude_none=True),
            "source": "builtin"} for s in request.app.state.rt.workflows.values()]
    async with tenant_session(tenant_of(request)) as s:
        for w in (await s.execute(select(Workflow).where(Workflow.enabled))).scalars():
            out.append({"name": w.name, "version": w.version, "source": "custom"})
    return out


@router.post("/lint")
async def lint_spec(body: SpecBody) -> dict[str, Any]:
    spec = _parse(body.yaml)
    errors = lint(spec, KNOWN_TOOLS, AGENTS)
    return {"valid": not errors, "errors": errors}


@router.post("", status_code=201)
async def register(body: SpecBody, request: Request) -> dict[str, Any]:
    spec = _parse(body.yaml)
    if errors := lint(spec, KNOWN_TOOLS, AGENTS):
        raise InvalidInput("workflow failed lint", details={"errors": errors})
    t = tenant_of(request)
    async with tenant_session(t) as s:
        latest = (await s.execute(select(func.max(Workflow.version)).where(Workflow.name == spec.name))).scalar() or 0
        s.add(Workflow(tenant_id=uuid.UUID(t), name=spec.name, version=latest + 1,
                       spec=spec.model_dump(by_alias=True)))
    request.app.state.rt.workflows[spec.name] = spec
    return {"name": spec.name, "version": latest + 1}


@router.post("/{name}/run", status_code=202)
async def trigger(name: str, request: Request, event: dict[str, Any] | None = None) -> dict[str, str]:
    if name not in request.app.state.rt.workflows:
        raise NotFound("workflow not found")
    t = tenant_of(request)
    rid = await request.app.state.rt.start(t, "workflows", {"workflow": name, "event": event or {}})
    await request.app.state.arq.enqueue_job("execute_run", t, str(rid), _queue_name=AGENTS_Q, _job_id=f"run:{rid}:0")
    return {"run_id": str(rid)}

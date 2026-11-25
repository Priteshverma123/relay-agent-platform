"""Webhook intake: GitHub App + Slack (events, interactive approvals).

Every delivery is signature-verified, then deduplicated by delivery id (providers retry), then
turned into a run and enqueued. Handlers return fast (<3 s Slack requirement); work is async.
"""

from __future__ import annotations

import json
import urllib.parse
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.dialects.postgresql import insert as pg_insert

from relay.common.errors import Unauthorized
from relay.common.settings import get_settings
from relay.core.runs import decide_approval
from relay.db.models import WebhookDelivery
from relay.db.session import admin_session
from relay.integrations.webhooks import verify_github, verify_slack
from relay.workers.main import AGENTS_Q, DEFAULT_TENANT

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


async def _first_delivery(delivery_id: str, source: str, event: str, tenant_id: str) -> bool:
    async with admin_session() as s:
        res = await s.execute(pg_insert(WebhookDelivery).values(
            delivery_id=delivery_id, source=source, tenant_id=uuid.UUID(tenant_id), event=event)
            .on_conflict_do_nothing(index_elements=["delivery_id"]))
        return bool(res.rowcount)


async def _launch(request: Request, tenant_id: str, agent: str, trigger: dict[str, Any]) -> str:
    rid = await request.app.state.rt.start(tenant_id, agent, trigger)
    await request.app.state.arq.enqueue_job("execute_run", tenant_id, str(rid), _queue_name=AGENTS_Q,
                                            _job_id=f"run:{rid}:0")
    return str(rid)


@router.post("/github")
async def github(request: Request, x_github_event: Annotated[str, Header()],
                 x_github_delivery: Annotated[str, Header()],
                 x_hub_signature_256: Annotated[str | None, Header()] = None) -> JSONResponse:
    body = await request.body()
    if not verify_github(get_settings().github_webhook_secret.get_secret_value(), body, x_hub_signature_256):
        raise Unauthorized("bad signature")
    ev = json.loads(body)
    action = ev.get("action", "")
    tenant_id = DEFAULT_TENANT  # single-tenant GitHub App install; map installation → tenant for SaaS
    if not await _first_delivery(x_github_delivery, "github", f"{x_github_event}.{action}", tenant_id):
        return JSONResponse({"status": "duplicate"})
    inst = ev.get("installation", {}).get("id")
    repo = ev.get("repository", {}).get("full_name")
    launched: list[str] = []
    if x_github_event == "pull_request" and action in ("opened", "synchronize", "ready_for_review"):
        pr = ev["pull_request"]
        if not pr.get("draft"):
            launched.append(await _launch(request, tenant_id, "review", {
                "repo": repo, "number": pr["number"], "installation_id": inst, "head_sha": pr["head"]["sha"]}))
    elif x_github_event == "issues" and action == "labeled" and ev.get("label", {}).get("name") == "relay:fix":
        issue = ev["issue"]
        launched.append(await _launch(request, tenant_id, "coding", {
            "repo": repo, "issue_number": issue["number"], "installation_id": inst, "title": issue["title"],
            "reuse_key": f"{repo}#{issue['number']}"}))
    rt = request.app.state.rt
    from relay.agents.workflows.builtins import matches

    for spec in rt.workflows.values():
        if matches(spec, "github", f"{x_github_event}.{action}", ev):
            launched.append(await _launch(request, tenant_id, "workflows", {
                "workflow": spec.name, "event": ev, "repo": repo}))
    return JSONResponse({"status": "accepted", "runs": launched}, status_code=202)


@router.post("/slack/interactions")
async def slack_interactions(request: Request, x_slack_request_timestamp: Annotated[str | None, Header()] = None,
                             x_slack_signature: Annotated[str | None, Header()] = None) -> JSONResponse:
    body = await request.body()
    if not verify_slack(get_settings().slack_signing_secret.get_secret_value(), body, x_slack_request_timestamp,
                        x_slack_signature):
        raise Unauthorized("bad signature")
    payload = json.loads(urllib.parse.parse_qs(body.decode())["payload"][0])
    action = payload["actions"][0]
    approved = action["action_id"] == "relay_approve"
    actor = f"slack:{payload['user']['id']}"
    run_id, resume = await decide_approval(DEFAULT_TENANT, uuid.UUID(action["value"]), approved=approved, actor=actor)
    await request.app.state.arq.enqueue_job("execute_run", DEFAULT_TENANT, str(run_id), resume, _queue_name=AGENTS_Q,
                                            _job_id=f"run:{run_id}:{action['value']}")
    return JSONResponse({"text": f"{'Approved' if approved else 'Rejected'} by <@{payload['user']['id']}>",
                         "replace_original": False})


@router.post("/slack/events")
async def slack_events(request: Request, x_slack_request_timestamp: Annotated[str | None, Header()] = None,
                       x_slack_signature: Annotated[str | None, Header()] = None) -> JSONResponse:
    body = await request.body()
    ev = json.loads(body)
    if ev.get("type") == "url_verification":
        return JSONResponse({"challenge": ev.get("challenge")})
    if not verify_slack(get_settings().slack_signing_secret.get_secret_value(), body, x_slack_request_timestamp,
                        x_slack_signature):
        raise Unauthorized("bad signature")
    e = ev.get("event", {})
    if e.get("type") != "message" or e.get("bot_id") or e.get("subtype"):
        return JSONResponse({"status": "ignored"})
    if not await _first_delivery(ev.get("event_id", uuid.uuid4().hex), "slack", "message", DEFAULT_TENANT):
        return JSONResponse({"status": "duplicate"})
    rid = await _launch(request, DEFAULT_TENANT, "support", {
        "channel": e.get("channel"), "thread_id": e.get("thread_ts") or e.get("ts"), "customer": e.get("user"),
        "raw_message": e.get("text", "")})
    return JSONResponse({"status": "accepted", "run": rid})

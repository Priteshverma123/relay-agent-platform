"""AgentRuntime: builds everything a run needs and drives it (start / resume).

Per run: ToolRegistry (only the agent's tools) → PolicyEngine (agent policy + analyzer ensemble)
→ ToolRouter (events, idempotency, taint, budget, stuck) → agent graph → compiled with the
Postgres checkpointer (thread_id = run id) → ``run_graph`` (interrupt ⇒ approval + Slack).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any

import orjson
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from redis.asyncio import Redis
from sqlalchemy import select

from relay.agents.coding.graph import build_coding_graph
from relay.agents.meetings.graph import build_meeting_graph
from relay.agents.review.graph import build_review_graph
from relay.agents.support.graph import build_support_graph
from relay.agents.workflows.builtins import load_builtin
from relay.agents.workflows.engine import WorkflowContext, WorkflowEngine
from relay.common.logging import get_logger
from relay.common.settings import Settings
from relay.core.budget import Budget, BudgetGuard
from relay.core.events import PostgresEventLog
from relay.core.idempotency import RedisIdempotencyStore
from relay.core.ledger import postgres_ledger
from relay.core.runs import create_run, run_graph, set_status
from relay.core.stuck import StuckDetector
from relay.core.toolrouter import ToolRouter
from relay.db.models import Run
from relay.db.session import tenant_session
from relay.integrations.clients import AtlasRAG, Jira, Notion
from relay.integrations.github_app import GitHubApp
from relay.integrations.slack import Slack
from relay.llm.client import GatewayLLM
from relay.memory.embed import Embedder
from relay.memory.store import MemoryStore
from relay.security.analyzers import EnsembleAnalyzer, PatternAnalyzer, PolicyRailsAnalyzer
from relay.security.policy import PolicyEngine, load_policy
from relay.security.taint import TaintState
from relay.tools.base import Tool, ToolContext, ToolRegistry
from relay.tools.builtin import coding_tools
from relay.tools.integration_tools import integration_tools

log = get_logger(__name__)
AGENTS = {"coding", "review", "support", "meetings", "workflows"}


def _pg(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


class AgentRuntime:
    def __init__(self, settings: Settings) -> None:
        s = self.s = settings
        self.redis = Redis.from_url(s.redis_url)
        self.llm = GatewayLLM(s)
        self.memory = MemoryStore(Embedder(s.embed_url, s.embed_dim))
        self.github = GitHubApp(s.github_app_id, s.github_private_key.get_secret_value()) \
            if s.github_app_id and s.github_private_key else None
        self.slack = Slack(s.slack_bot_token.get_secret_value()) if s.slack_bot_token else None
        self.jira = Jira(s.jira_url, s.jira_email or "", s.jira_token.get_secret_value(), s.jira_project) \
            if s.jira_url and s.jira_token else None
        self.notion = Notion(s.notion_token.get_secret_value()) if s.notion_token else None
        self.atlas = AtlasRAG(s.atlas_url, s.atlas_key.get_secret_value(), s.atlas_collections) \
            if s.atlas_key.get_secret_value() else None
        self.workflows = load_builtin(s.config_dir)
        self._sandbox_provider: Any = None

    # ------------------------------------------------------------------ helpers
    def sandbox_provider(self) -> Any:
        if self._sandbox_provider is None:
            from relay.sandbox.docker_provider import DockerSandboxProvider

            self._sandbox_provider = DockerSandboxProvider(self.s)
        return self._sandbox_provider

    async def publish(self, run_id: uuid.UUID, event: dict[str, Any]) -> None:
        await self.redis.publish(f"run:{run_id}", orjson.dumps(event, default=str))

    async def notify_approval(self, tenant_id: str, run_id: uuid.UUID, agent: str):  # type: ignore[no-untyped-def]
        async def notify(approval_id: uuid.UUID, request: dict[str, Any]) -> None:
            if self.slack:
                await self.slack.request_approval(self.s.slack_approvals_channel, str(approval_id), str(run_id),
                                                  agent, request)
        return notify

    def _router(self, agent: str, tenant_id: str, run_id: uuid.UUID, tools: list[Tool], sandbox: Any = None,
                services: dict[str, Any] | None = None, dry_run: bool = False) -> ToolRouter:
        policy = load_policy(self.s.config_dir, agent)
        engine = PolicyEngine(policy, EnsembleAnalyzer([PatternAnalyzer(), PolicyRailsAnalyzer()]))
        registry = ToolRegistry().add(*tools)
        ctx = ToolContext(run_id=run_id, tenant_id=tenant_id, sandbox=sandbox, dry_run=dry_run, services={
            "github": self.github, "slack": self.slack, "jira": self.jira, "notion": self.notion, "atlas": self.atlas,
            "notion_parent": self.s.notion_parent_page, **(services or {})})
        return ToolRouter(registry, engine, ctx, PostgresEventLog(tenant_id, self.publish),
                          RedisIdempotencyStore(self.redis), TaintState(), BudgetGuard(Budget.from_dict(policy.budget)),
                          StuckDetector(), ledger=postgres_ledger(tenant_id, run_id))

    @asynccontextmanager
    async def _checkpointer(self):  # type: ignore[no-untyped-def]
        async with AsyncPostgresSaver.from_conn_string(_pg(self.s.database_admin_url)) as saver:
            await saver.setup()
            yield saver

    # ------------------------------------------------------------------ entry points
    async def start(self, tenant_id: str, agent: str, trigger: dict[str, Any]) -> uuid.UUID:
        if agent not in AGENTS:
            raise ValueError(f"unknown agent {agent}")
        budget = load_policy(self.s.config_dir, agent).budget
        return await create_run(tenant_id, agent, trigger, budget)

    async def execute(self, tenant_id: str, run_id: uuid.UUID, resume: Any = None) -> dict[str, Any]:
        async with tenant_session(tenant_id) as s:
            run = (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()
        agent, trig = run.agent, dict(run.trigger)
        if agent == "workflows":
            return await self._execute_workflow(tenant_id, run, resume)
        sandbox = None
        try:
            if agent == "coding":
                sandbox = await self.sandbox_provider().acquire(str(run_id), reuse_key=trig.get("reuse_key"))
                router = self._router(agent, tenant_id, run_id, coding_tools() + integration_tools()[3:4], sandbox,
                                      {"test_command": trig.get("test_command") or "python -m pytest -q -x"})
                episodes = await self.memory.similar_episodes(tenant_id, "coding", str(trig.get("title", "")))
                pb = await self.memory.active_playbook(tenant_id, "coding")
                graph = build_coding_graph(llm=self.llm, router=router, github=self.github, sandbox=sandbox,
                                           tenant_id=tenant_id, require_plan_approval=bool(trig.get("plan_approval")),
                                           dry_run=bool(trig.get("dry_run")), episodes=episodes,
                                           playbook=pb[1] if pb else None)
                inputs = {"repo": trig["repo"], "issue_number": trig["issue_number"],
                          "installation_id": trig["installation_id"], "base_branch": trig.get("base_branch", "main")}
            elif agent == "review":
                sandbox = await self.sandbox_provider().acquire(str(run_id))
                router = self._router(agent, tenant_id, run_id, [], sandbox)
                graph = build_review_graph(llm=self.llm, github=self.github, sandbox=sandbox, engine=router.engine,
                                           events=router.events, idem=router.idem, run_id=run_id, tenant_id=tenant_id,
                                           dry_run=bool(trig.get("dry_run")))
                inputs = {"repo": trig["repo"], "number": trig["number"], "installation_id": trig["installation_id"]}
            elif agent == "support":
                router = self._router(agent, tenant_id, run_id, integration_tools())
                graph = build_support_graph(llm=self.llm, atlas=self.atlas, jira=self.jira, slack=self.slack,
                                            engine=router.engine, events=router.events, idem=router.idem,
                                            run_id=run_id, tenant_id=tenant_id)
                inputs = {k: trig.get(k, "") for k in ("channel", "thread_id", "customer", "raw_message")}
            else:  # meetings
                router = self._router(agent, tenant_id, run_id, integration_tools())
                graph = build_meeting_graph(llm=self.llm, router=router, tenant_id=tenant_id,
                                            hf_token=trig.get("hf_token"))
                inputs = {"audio_path": trig.get("audio_path", ""), "attendees": trig.get("attendees", []),
                          "transcript": trig.get("transcript", [])}
            async with self._checkpointer() as saver:
                app = graph.compile(checkpointer=saver)
                res = await run_graph(app, tenant_id=tenant_id, run_id=run_id, inputs=inputs, resume=resume,
                                      notify=await self.notify_approval(tenant_id, run_id, agent))
            return res
        except Exception as exc:
            log.exception("run_failed", run_id=str(run_id), agent=agent)
            await set_status(tenant_id, run_id, "failed", error=str(exc)[:2000])
            raise
        finally:
            if sandbox is not None:
                # coding sandboxes are paused for follow-ups on the same PR; others are destroyed
                if agent == "coding" and trig.get("reuse_key"):
                    await sandbox.pause()
                else:
                    await sandbox.destroy()

    async def _execute_workflow(self, tenant_id: str, run: Run, resume: Any) -> dict[str, Any]:
        spec = self.workflows[run.trigger["workflow"]]
        ctx = WorkflowContext.from_state(run.state or {"event": run.trigger.get("event", {})})
        if resume is not None and run.state.get("suspended_step"):
            ctx.approvals[run.state["suspended_step"]] = bool(resume.get("approved"))
        router = self._router("workflows", tenant_id, run.id, integration_tools(),
                              services={"repo": run.trigger.get("repo")})
        if run.tainted:
            router.taint.mark("trigger_event")

        async def persist(state: dict[str, Any]) -> None:
            await set_status(tenant_id, run.id, "running", state=state)

        async def run_agent(name: str, payload: dict[str, Any]) -> dict[str, Any]:
            child = await self.start(tenant_id, name, payload)
            out = await self.execute(tenant_id, child)
            return {"run_id": str(child), **out}

        engine = WorkflowEngine(spec, router=router, llm=self.llm, run_agent=run_agent, persist=persist,
                                tenant_id=tenant_id)
        await set_status(tenant_id, run.id, "running")
        res = await engine.run(ctx)
        if res["status"] == "suspended":
            from relay.core.runs import run_graph as _  # noqa: F401  (keeps import graph explicit)
            from relay.db.models import Approval

            approval_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{run.id}:{res['step']}")
            async with tenant_session(tenant_id) as s:
                if await s.get(Approval, approval_id) is None:
                    s.add(Approval(id=approval_id, tenant_id=uuid.UUID(tenant_id), run_id=run.id,
                                   kind="workflow_step", request=res["request"]))
            await set_status(tenant_id, run.id, "waiting_approval", state={**ctx.to_state(),
                                                                           "suspended_step": res["step"]})
            notify = await self.notify_approval(tenant_id, run.id, f"workflow:{spec.name}")
            await notify(approval_id, res["request"])
            return {"status": "waiting_approval"}
        await set_status(tenant_id, run.id, "succeeded" if res["status"] == "succeeded" else "failed",
                         state=ctx.to_state(), outcome={"status": res["status"], "error": res.get("error")})
        return res

    async def aclose(self) -> None:
        await self.llm.aclose()
        await self.redis.aclose()

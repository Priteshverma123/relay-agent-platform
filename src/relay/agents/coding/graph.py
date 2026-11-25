"""Autonomous Coding Agent.

    setup ─▶ plan ─▶ plan_gate ─▶ [ agent ⇄ gate ⇄ tools … judge ] ─▶ self_review ─▶ verify ─▶ publish_gate ─▶ publish
    (sandbox,  (deep tier,  (optional   (tool loop, standard tier,       (review agent   (tests +   (policy/HITL)  (API commit +
     repo map)  structured)  HITL)       ACI tools, stuck/budget guard)   on own diff)    lint)                     draft PR)

* Reproduce-first prompting: write/identify a failing test before fixing when feasible.
* The sandbox gets only a scoped, read-only, short-lived token for cloning; the commit and the
  draft PR are created by the runtime through the GitHub API (no push credentials in the sandbox).
* Never merges; always a DRAFT PR whose body carries plan, test evidence, cost and run link.
* Graceful exits: budget exhausted / stuck → draft PR (if a diff exists) labelled partial.
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from relay.agents.review.graph import review_diff
from relay.core.agent_loop import LoopState, build_tool_loop
from relay.core.events import Event, EventType
from relay.core.prompts import initial_messages
from relay.core.toolrouter import ToolRouter
from relay.llm.client import LLM, structured
from relay.security.policy import Decision
from relay.security.taint import wrap_untrusted
from relay.tools.repomap import repo_map_from_sandbox

ROLE = """You are Relay's coding agent. You fix issues in a repository checked out at /workspace.
Approach: (1) locate the relevant code (use the repo map and search_files), (2) reproduce the problem
with a failing test when feasible, (3) make the smallest correct change, (4) run the targeted tests,
then the broader suite, (5) finish with a summary of the root cause, the change and test evidence."""


class Plan(BaseModel):
    root_cause_hypothesis: str
    steps: list[str] = Field(description="3-7 concrete steps")
    files_to_inspect: list[str]
    test_strategy: str


class CodingState(LoopState, total=False):
    repo: str
    issue_number: int
    installation_id: int
    base_branch: str
    base_sha: str
    issue_title: str
    issue_body: str
    repo_map: str
    plan: dict[str, Any]
    review: dict[str, Any]
    fix_rounds: int
    tests_passed: bool
    test_output: str
    diff: str
    pr_url: str
    publish_approved: bool


def build_coding_graph(*, llm: LLM, router: ToolRouter, github: Any, sandbox: Any, tenant_id: str,
                       require_plan_approval: bool = False, dry_run: bool = False,
                       episodes: list[str] | None = None, playbook: str | None = None) -> StateGraph:
    events, run_id = router.events, router.run_id
    g = StateGraph(CodingState)

    async def setup(s: CodingState) -> dict[str, Any]:
        await events.append(run_id, Event(EventType.PHASE, {"phase": "setup"}))
        issue = await github.get_issue(s["installation_id"], s["repo"], s["issue_number"]) if github else \
            {"title": s.get("issue_title", ""), "body": s.get("issue_body", "")}
        router.taint.mark("issue_body")  # issue text is attacker-controllable in public repos
        if github:
            owner_repo = s["repo"].split("/", 1)[1]
            token = await github.scoped_read_token(s["installation_id"], owner_repo)
            env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader",
                   "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: bearer {token}"}
            r = await sandbox.exec(f"git clone --depth 50 --branch {s.get('base_branch', 'main')} "
                                   f"https://github.com/{s['repo']}.git . && git rev-parse HEAD", timeout_s=600,
                                   env=env)
            base_sha = r.output.strip().splitlines()[-1] if r.exit_code == 0 else ""
        else:
            base_sha = (await sandbox.exec("git rev-parse HEAD", timeout_s=30)).output.strip()
        rmap = await repo_map_from_sandbox(sandbox, f"{issue.get('title', '')}\n{issue.get('body', '')}")
        return {"issue_title": issue.get("title", ""), "issue_body": issue.get("body") or "", "base_sha": base_sha,
                "repo_map": rmap, "tainted": True, "taint_sources": list(router.taint.sources)}

    def task_text(s: CodingState) -> str:
        return (wrap_untrusted(f"Issue #{s.get('issue_number', '')}: {s.get('issue_title', '')}\n\n"
                               f"{s.get('issue_body', '')[:8000]}", "github_issue") +
                f"\n\n<repo_map>\n{s.get('repo_map', '')}\n</repo_map>")

    async def plan(s: CodingState) -> dict[str, Any]:
        p, resp = await structured(llm, "deep", [
            {"role": "system", "content": "Plan a minimal, test-driven fix. Be concrete about files."},
            {"role": "user", "content": task_text(s)}], Plan, max_tokens=1200, tenant_id=tenant_id)
        router.budget.charge_llm(resp.tokens, resp.cost_usd)
        await events.append(run_id, Event(EventType.PHASE, {"phase": "plan", "plan": p.model_dump()},
                                          tokens=resp.tokens, cost_usd=resp.cost_usd))
        return {"plan": p.model_dump()}

    async def plan_gate(s: CodingState) -> dict[str, Any]:
        p = dict(s["plan"])
        if require_plan_approval and not dry_run:
            reply = interrupt({"kind": "plan", "plan": p})
            if not (reply or {}).get("approved"):
                return {"status": "aborted", "result": "plan rejected by reviewer"}
            if (reply or {}).get("edits"):
                p["human_notes"] = reply["edits"]
        msgs = initial_messages(ROLE, task_text(s) + f"\n\n<plan>\n{p}\n</plan>", playbook=playbook,
                                episodes=episodes)
        return {"messages": msgs, "status": "running", "turns": 0, "fix_rounds": s.get("fix_rounds", 0)}

    def after_plan_gate(s: CodingState) -> str:
        return "publish" if s.get("status") == "aborted" else "agent"

    async def self_review(s: CodingState) -> dict[str, Any]:
        r = await sandbox.exec("git add -A -N . && git diff", timeout_s=60)
        diff = r.output
        if not diff.strip():
            return {"diff": "", "review": {"findings": []}}
        rep = await review_diff(llm, diff, sandbox=None, title=s.get("issue_title", ""), tenant_id=tenant_id)
        router.budget.charge_llm(0, rep.cost_usd)
        serious = [f for f in rep.findings if f.severity in ("high", "critical")]
        await events.append(run_id, Event(EventType.PHASE, {"phase": "self_review", "findings": len(rep.findings),
                                                            "serious": len(serious)}, cost_usd=rep.cost_usd))
        out: dict[str, Any] = {"diff": diff, "review": {"findings": [f.message for f in rep.findings]}}
        if serious and s.get("fix_rounds", 0) < 1 and not router.budget.status().exhausted:
            notes = "\n".join(f"- {f.path}:{f.line} {f.message}" for f in serious)
            out.update(fix_rounds=s.get("fix_rounds", 0) + 1, status="running", messages=[
                *s["messages"], {"role": "user", "content": f"A code review of your diff found:\n{notes}\n"
                                                            "Fix these, re-run tests, then finish."}])
        return out

    def after_self_review(s: CodingState) -> str:
        return "agent" if s.get("status") == "running" else "verify"

    async def verify(s: CodingState) -> dict[str, Any]:
        cmd = router.ctx.services.get("test_command", "python -m pytest -q -x --no-header -p no:cacheprovider")
        r = await sandbox.exec(cmd, timeout_s=1200)
        await events.append(run_id, Event(EventType.PHASE, {"phase": "verify", "exit_code": r.exit_code}))
        return {"tests_passed": r.exit_code == 0, "test_output": r.output[-3000:]}

    async def publish_gate(s: CodingState) -> dict[str, Any]:
        if not s.get("diff"):
            return {"publish_approved": False}
        d = await router.engine.evaluate("open_pr", {"repo": s.get("repo", ""), "title": s.get("issue_title", ""),
                                                     "diff": s["diff"][:20000]}, tainted=True)
        await events.append(run_id, Event(EventType.DECISION, {"tool": "open_pr", **d.to_dict()}))
        if d.decision == Decision.DENY or dry_run:
            return {"publish_approved": False}
        if d.decision == Decision.APPROVE:
            reply = interrupt({"kind": "tool_calls", "calls": [{"call_id": "open_pr", "tool": "open_pr",
                                                                "args": {"repo": s.get("repo")}, **d.to_dict()}],
                               "tainted": True, "taint_sources": s.get("taint_sources", [])})
            return {"publish_approved": "open_pr" in (reply or {}).get("approved", [])}
        return {"publish_approved": True}

    async def publish(s: CodingState) -> dict[str, Any]:
        status = s.get("status") or "done"
        if not s.get("publish_approved") or not github:
            return {"status": status if status != "running" else "done",
                    "result": s.get("result") or "no PR opened", "outcome": {"diff": s.get("diff", "")[:50000]}}
        porcelain = await sandbox.exec("git status --porcelain", timeout_s=60)
        files: dict[str, str | None] = {}
        for line in porcelain.output.splitlines():
            code, path = line[:2], line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            files[path] = None if "D" in code else await sandbox.read_file(path)
        branch = f"relay/issue-{s['issue_number']}-{str(run_id)[:8]}"
        spent = router.budget.spent.to_dict()
        await github.commit_files(s["installation_id"], s["repo"], branch, s["base_sha"], files,
                                  f"fix: {s.get('issue_title', '')[:60]} (#{s['issue_number']})")
        partial = status in ("partial", "aborted") or not s.get("tests_passed")
        body = (f"Closes #{s['issue_number']} (draft by Relay{' · ⚠️ partial' if partial else ''})\n\n"
                f"### Summary\n{s.get('result', '')[:3000]}\n\n### Plan\n" +
                "".join(f"- {x}\n" for x in s.get("plan", {}).get("steps", [])) +
                f"\n### Tests\n{'✅ passing' if s.get('tests_passed') else '❌ failing'}\n"
                f"```\n{s.get('test_output', '')[-1500:]}\n```\n\n### Self-review\n" +
                ("".join(f"- {m[:200]}\n" for m in s.get("review", {}).get("findings", [])) or "no findings\n") +
                f"\n<sub>run `{run_id}` · ${spent['usd']:.3f} · {spent['steps']} steps · {spent['wall_s']}s</sub>")
        pr = await github.open_draft_pr(s["installation_id"], s["repo"], branch, s.get("base_branch", "main"),
                                        f"[Relay] {s.get('issue_title', '')[:80]}", body)
        await events.append(run_id, Event(EventType.OBSERVATION, {"tool": "open_pr", "url": pr.get("html_url")}))
        return {"pr_url": pr.get("html_url", ""), "status": "partial" if partial else "done",
                "result": f"Opened draft PR {pr.get('html_url')}"}

    g.add_node("setup", setup)
    g.add_node("plan", plan)
    g.add_node("plan_gate", plan_gate)
    g.add_node("self_review", self_review)
    g.add_node("verify", verify)
    g.add_node("publish_gate", publish_gate)
    g.add_node("publish", publish)
    g.add_edge(START, "setup")
    g.add_edge("setup", "plan")
    g.add_edge("plan", "plan_gate")
    g.add_conditional_edges("plan_gate", after_plan_gate, ["agent", "publish"])
    build_tool_loop(agent="coding", llm=llm, router=router, tools=router.registry.names(), route="answer",
                    tenant_id=tenant_id, goal_judge=True, task_for_judge="Fix the GitHub issue described in the "
                    "first user message, with passing tests.", graph=g, entry=None, exit_to="self_review")
    g.add_conditional_edges("self_review", after_self_review, ["agent", "verify"])
    g.add_edge("verify", "publish_gate")
    g.add_edge("publish_gate", "publish")
    g.add_edge("publish", END)
    return g


async def run_baseline(llm: LLM, sandbox: Any, task: str, *, max_steps: int = 40) -> str:
    """mini-swe-agent-style baseline arm: one bash tool, linear history, no planner/map/review.
    Used in evals to show what the extra structure buys (or doesn't) per dollar."""
    tool = [{"type": "function", "function": {"name": "bash", "description": "Run a bash command in the repo.",
                                              "parameters": {"type": "object", "properties": {
                                                  "command": {"type": "string"}}, "required": ["command"]}}}]
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "Fix the issue in /workspace using bash. Finish with no tool call."},
        {"role": "user", "content": task}]
    for _ in range(max_steps):
        resp = await llm.chat("answer", msgs, tools=tool, max_tokens=2048)
        msgs.append(resp.assistant_message())
        if not resp.tool_calls:
            break
        for tc in resp.tool_calls:
            import json

            cmd = json.loads(tc["function"]["arguments"] or "{}").get("command", "")
            r = await sandbox.exec(cmd, timeout_s=300)
            msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": r.output[-6000:] or "(no output)"})
    return (await sandbox.exec("git add -A -N . && git diff", timeout_s=60)).output


def new_run_id() -> uuid.UUID:
    return uuid.uuid4()

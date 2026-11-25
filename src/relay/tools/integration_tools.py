"""Side-effect tools backed by integrations (Jira, Notion, Slack, GitHub, Atlas).

They are plain Tools: every call still goes through ToolRouter → policy → approval → idempotency.
In ``dry_run`` (evals) they return what they would have done.
"""

from __future__ import annotations

from typing import Any

from relay.security.taint import wrap_untrusted
from relay.tools.base import Tool, ToolContext, ToolResult, params


def _svc(ctx: ToolContext, name: str) -> Any:
    svc = ctx.services.get(name)
    if svc is None:
        raise RuntimeError(f"integration '{name}' is not configured")
    return svc


async def create_ticket(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if ctx.dry_run:
        return ToolResult(f"[dry-run] would create ticket: {a['summary']}", data={"dry_run": True})
    issue = await _svc(ctx, "jira").create_issue(a["summary"], a.get("description", ""))
    return ToolResult(f"Created {issue.get('key')}", data={"key": issue.get("key")})


async def create_notion_page(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if ctx.dry_run:
        return ToolResult(f"[dry-run] would create Notion page '{a['title']}'")
    page = await _svc(ctx, "notion").create_page(ctx.services["notion_parent"], a["title"], a.get("lines", []))
    return ToolResult(f"Created page {page.get('url', page.get('id'))}")


async def post_slack(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if ctx.dry_run:
        return ToolResult("[dry-run] would post to Slack")
    channel = a.get("channel") or ctx.services.get("default_channel", "#general")
    await _svc(ctx, "slack").post(channel, a["text"], thread_ts=a.get("thread_ts"))
    return ToolResult(f"Posted to {channel}")


async def kb_search(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rows = await _svc(ctx, "atlas").search(a["query"], int(a.get("top_k") or 6))
    text = "\n\n".join(f"[{r.get('title')} › {r.get('section_path')} p.{r.get('page_start')}] {r.get('text', '')[:800]}"
                       for r in rows)
    return ToolResult(text or "No results.")


async def get_issue(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    gh = _svc(ctx, "github")
    issue = await gh.get_issue(ctx.services["installation_id"], a["repo"], int(a["number"]))
    return ToolResult(wrap_untrusted(f"#{issue['number']} {issue['title']}\n\n{issue.get('body') or ''}",
                                     "github_issue"))


async def jira_transition(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if ctx.dry_run:
        return ToolResult(f"[dry-run] would move {a['key']} to {a['to']}")
    await _svc(ctx, "jira").transition(a["key"], a["to"])
    return ToolResult(f"{a['key']} → {a['to']}")


async def jira_search(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    r = await _svc(ctx, "jira").http.get("/rest/api/3/search", params={"jql": a["jql"], "maxResults": 50,
                                                                      "fields": "summary,assignee,status"})
    issues = r.json().get("issues", []) if r.status_code < 400 else []
    return ToolResult("\n".join(f"{i['key']}: {i['fields']['summary']} ({(i['fields'].get('assignee') or {})
                                                                         .get('displayName', 'unassigned')})"
                                for i in issues) or "none")


async def github_search_prs(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    gh = _svc(ctx, "github")
    repo = ctx.services.get("repo", "")
    d = await gh._req(ctx.services["installation_id"], "GET", "/search/issues",
                      params={"q": f"repo:{repo} {a['query']}", "per_page": 50})
    return ToolResult(wrap_untrusted("\n".join(f"#{i['number']} {i['title']} (@{i['user']['login']})"
                                               for i in d.get("items", [])) or "none", "github_prs"))


async def github_recent_deploys(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    gh = _svc(ctx, "github")
    d = await gh._req(ctx.services["installation_id"], "GET", f"/repos/{ctx.services.get('repo', '')}/deployments",
                      params={"per_page": 20})
    return ToolResult("\n".join(f"{x['created_at']} {x['environment']} {x['sha'][:8]} by {x['creator']['login']}"
                                for x in d) or "none")


def integration_tools() -> list[Tool]:
    return [
        Tool("create_ticket", "Create a Jira ticket.",
             params({"summary": {"type": "string"}, "description": {"type": "string"}}, ["summary"]), create_ticket),
        Tool("create_notion_page", "Create a Notion page with the given lines.",
             params({"title": {"type": "string"}, "lines": {"type": "array", "items": {"type": "string"}}}, ["title"]),
             create_notion_page),
        Tool("post_slack", "Post a message to Slack.",
             params({"text": {"type": "string"}, "channel": {"type": "string"}, "thread_ts": {"type": "string"}},
                    ["text"]), post_slack),
        Tool("kb_search", "Search the internal knowledge base (Atlas).",
             params({"query": {"type": "string"}, "top_k": {"type": "integer"}}, ["query"]), kb_search),
        Tool("get_issue", "Read a GitHub issue (untrusted content).",
             params({"repo": {"type": "string"}, "number": {"type": "integer"}}), get_issue, untrusted_output=True),
        Tool("jira_transition", "Move a Jira issue to a status.",
             params({"key": {"type": "string"}, "to": {"type": "string"}}), jira_transition),
        Tool("jira_search", "Search Jira with JQL.", params({"jql": {"type": "string"}}), jira_search),
        Tool("github_search_prs", "Search pull requests in the configured repo.",
             params({"query": {"type": "string"}}), github_search_prs, untrusted_output=True),
        Tool("github_recent_deploys", "List recent deployments of the configured repo.",
             params({"hours": {"type": "integer"}}, []), github_recent_deploys),
    ]

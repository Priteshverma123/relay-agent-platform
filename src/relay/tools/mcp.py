"""MCP client integration: expose selected tools from MCP servers as Relay tools.

Hardening (MCP security best practices + github-mcp-server guidance):
  * servers are configured by the operator (``config/mcp.yaml``), never by model or user input;
  * explicit allowlist of tools per server (``toolsets``); everything else is invisible to agents;
  * GitHub MCP runs with ``--read-only`` and narrowed ``--toolsets``; writes go through Relay's own
    GitHub App client so they pass the policy engine and idempotency layer;
  * no token passthrough: credentials for the MCP server live in its own env, not in prompts;
  * every MCP tool is classified in the agent policy like any native tool, and its output is
    treated as untrusted by default.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from relay.tools.base import Tool, ToolContext, ToolResult


@dataclass(slots=True)
class MCPServerConfig:
    name: str
    command: str | None = None  # stdio servers
    args: list[str] = field(default_factory=list)
    url: str | None = None  # streamable-http servers
    env: dict[str, str] = field(default_factory=dict)
    allow_tools: list[str] = field(default_factory=list)
    untrusted_output: bool = True


def load_mcp_config(path: Path) -> list[MCPServerConfig]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [MCPServerConfig(name=n, **cfg) for n, cfg in (raw.get("servers") or {}).items()]


class MCPToolset:
    """Connects to configured MCP servers and wraps allowlisted tools as Relay ``Tool`` objects."""

    def __init__(self, servers: list[MCPServerConfig]) -> None:
        self.servers = servers
        self._stack = AsyncExitStack()
        self._sessions: dict[str, Any] = {}

    async def connect(self) -> list[Tool]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client

        tools: list[Tool] = []
        for srv in self.servers:
            if srv.url:
                read, write, _ = await self._stack.enter_async_context(streamablehttp_client(srv.url))
            else:
                params = StdioServerParameters(command=srv.command or "", args=srv.args, env=srv.env)
                read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[srv.name] = session
            listed = await session.list_tools()
            for t in listed.tools:
                if t.name not in srv.allow_tools:
                    continue
                tools.append(self._wrap(srv, t))
        return tools

    def _wrap(self, srv: MCPServerConfig, t: Any) -> Tool:
        session = self._sessions[srv.name]

        async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            res = await session.call_tool(t.name, args)
            text = "\n".join(getattr(c, "text", "") for c in res.content if getattr(c, "type", "") == "text")
            return ToolResult(text, is_error=bool(getattr(res, "isError", False)))

        return Tool(name=f"{srv.name}__{t.name}", description=(t.description or "")[:1000],
                    parameters=t.inputSchema or {"type": "object", "properties": {}}, handler=handler,
                    untrusted_output=srv.untrusted_output)

    async def aclose(self) -> None:
        await self._stack.aclose()

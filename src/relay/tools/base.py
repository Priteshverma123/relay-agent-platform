"""Tool definitions + registry.

Schemas are emitted sorted by name with deterministic JSON so the tools block, which sits at the
very start of every prompt, is byte-identical across steps and runs → provider prompt-cache hits.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolResult:
    output: str
    is_error: bool = False
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolContext:
    run_id: uuid.UUID
    tenant_id: str
    sandbox: Any = None  # sandbox.base.Sandbox
    services: dict[str, Any] = field(default_factory=dict)  # github, slack, jira, notion, atlas, ...
    dry_run: bool = False  # evals: side-effect tools return what they *would* do


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    untrusted_output: bool = False
    timeout_s: float = 300.0

    def schema(self) -> dict[str, Any]:
        params = dict(self.parameters)
        params.setdefault("type", "object")
        params.setdefault("additionalProperties", False)
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": params}}


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def add(self, *tools: Tool) -> ToolRegistry:
        for t in tools:
            self.register(t)
        return self

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, only: list[str] | None = None) -> list[dict[str, Any]]:
        names = sorted(only) if only is not None else self.names()
        return [self._tools[n].schema() for n in names if n in self._tools]

    def untrusted_tools(self) -> set[str]:
        return {n for n, t in self._tools.items() if t.untrusted_output}


def params(props: dict[str, dict[str, Any]], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required if required is not None else list(props),
            "additionalProperties": False}

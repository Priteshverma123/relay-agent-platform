from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from relay.core.budget import Budget, BudgetGuard
from relay.core.events import MemoryEventLog
from relay.core.idempotency import MemoryIdempotencyStore
from relay.core.stuck import StuckDetector
from relay.core.toolrouter import ToolRouter
from relay.security.analyzers import EnsembleAnalyzer, PatternAnalyzer, PolicyRailsAnalyzer
from relay.security.policy import AgentPolicy, PolicyEngine
from relay.security.taint import TaintState
from relay.tools.base import Tool, ToolContext, ToolRegistry, ToolResult, params

CONFIG = Path(__file__).resolve().parents[1] / "config"


@pytest.fixture
def config_dir() -> Path:
    return CONFIG


def make_policy(**tools: str | dict) -> AgentPolicy:
    return AgentPolicy.from_dict({"agent": "test", "tools": tools})


class Counter:
    def __init__(self) -> None:
        self.calls = 0


def make_router(policy: AgentPolicy, tools: list[Tool], *, tainted: bool = False) -> ToolRouter:
    taint = TaintState()
    if tainted:
        taint.mark("test")
    engine = PolicyEngine(policy, EnsembleAnalyzer([PatternAnalyzer(), PolicyRailsAnalyzer()]))
    return ToolRouter(ToolRegistry().add(*tools), engine, ToolContext(uuid.uuid4(), "t"), MemoryEventLog(),
                      MemoryIdempotencyStore(), taint, BudgetGuard(Budget()), StuckDetector())


def echo_tool(name: str, counter: Counter | None = None, untrusted: bool = False) -> Tool:
    async def h(args: dict, ctx: ToolContext) -> ToolResult:
        if counter:
            counter.calls += 1
        return ToolResult(f"{name}:{args}")
    return Tool(name, name, params({"x": {"type": "string"}}, []), h, untrusted_output=untrusted)


def call(name: str, args: str = "{}", cid: str = "c1") -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": args}}

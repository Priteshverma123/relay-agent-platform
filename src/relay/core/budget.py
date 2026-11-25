"""Per-run budget guard: USD, tokens, steps, tool calls, wall-clock time.

Checked *before* every LLM call and tool call. Exhaustion is not an exception that kills the
run: agents receive ``BudgetStatus.exhausted`` and take their graceful-exit path (coding agent
opens a draft PR with notes, research writes a partial report, ...).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Budget:
    usd: float = 1.0
    tokens: int = 1_000_000
    steps: int = 80
    tool_calls: int = 150
    wall_s: float = 1800.0

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Budget:
        b = cls()
        for k, v in (d or {}).items():
            if hasattr(b, k):
                setattr(b, k, type(getattr(b, k))(v))
        return b


@dataclass(slots=True)
class Spent:
    usd: float = 0.0
    tokens: int = 0
    steps: int = 0
    tool_calls: int = 0
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["wall_s"] = round(time.time() - self.started_at, 2)
        d.pop("started_at")
        return d


@dataclass(slots=True)
class BudgetStatus:
    exhausted: bool
    reason: str | None = None
    fraction: float = 0.0  # max utilisation across dimensions (0..1+)


class BudgetGuard:
    def __init__(self, budget: Budget, spent: Spent | None = None, *, clock: Any = time.time) -> None:
        self.budget = budget
        self.spent = spent or Spent(started_at=clock())
        self._clock = clock

    def charge_llm(self, tokens: int, usd: float) -> None:
        self.spent.tokens += tokens
        self.spent.usd += usd
        self.spent.steps += 1

    def charge_tool(self) -> None:
        self.spent.tool_calls += 1

    def status(self) -> BudgetStatus:
        b, s = self.budget, self.spent
        ratios = {
            "usd": s.usd / b.usd if b.usd else 0.0,
            "tokens": s.tokens / b.tokens if b.tokens else 0.0,
            "steps": s.steps / b.steps if b.steps else 0.0,
            "tool_calls": s.tool_calls / b.tool_calls if b.tool_calls else 0.0,
            "wall_s": (self._clock() - s.started_at) / b.wall_s if b.wall_s else 0.0,
        }
        worst = max(ratios, key=lambda k: ratios[k])
        return BudgetStatus(exhausted=ratios[worst] >= 1.0, reason=worst if ratios[worst] >= 1.0 else None,
                            fraction=ratios[worst])

    def remaining_hint(self) -> str:
        """Short line injected into the agent's context so it paces itself (task-budget style)."""
        st = self.status()
        return f"[budget] {int((1 - min(st.fraction, 1.0)) * 100)}% remaining (tightest: {self._tightest()})"

    def _tightest(self) -> str:
        b, s = self.budget, self.spent
        opts = {"usd": f"${s.usd:.3f}/${b.usd:.2f}", "steps": f"{s.steps}/{b.steps} steps",
                "tokens": f"{s.tokens}/{b.tokens} tokens"}
        st = self.status()
        return opts.get(st.reason or "", opts["steps"])

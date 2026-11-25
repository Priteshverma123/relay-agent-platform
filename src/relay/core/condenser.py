"""Context condenser for long agent loops.

Keeps: the stable prefix (system + task message), and the most recent turns within a token
budget; summarises the middle. Never splits an assistant ``tool_calls`` message from its
``tool`` results (orphaned tool results are an API error at every provider, and LangGraph's docs
call this out explicitly). The summary is inserted *after* the stable prefix so the provider
prompt cache for the prefix stays warm.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from relay.common.text import count_tokens

Msg = dict[str, Any]
Summarizer = Callable[[list[Msg]], Awaitable[str]]


def msg_tokens(m: Msg) -> int:
    c = m.get("content") or ""
    text = c if isinstance(c, str) else str(c)
    return count_tokens(text) + sum(count_tokens(str(tc)) for tc in m.get("tool_calls") or []) + 4


def safe_cut(messages: list[Msg], cut: int) -> int:
    """Move ``cut`` left until messages[cut:] doesn't start with tool results whose originating
    assistant message would be dropped."""
    while 0 < cut < len(messages) and messages[cut].get("role") == "tool":
        cut -= 1
    return cut


def select_tail(messages: list[Msg], prefix_len: int, budget: int) -> int:
    """Index where the kept tail starts (≥ prefix_len), respecting tool pairs."""
    spent, cut = 0, len(messages)
    for i in range(len(messages) - 1, prefix_len - 1, -1):
        t = msg_tokens(messages[i])
        if spent + t > budget:
            break
        spent += t
        cut = i
    return max(prefix_len, safe_cut(messages, cut))


def needs_condense(messages: list[Msg], max_tokens: int) -> bool:
    return sum(msg_tokens(m) for m in messages) > max_tokens


async def condense(messages: list[Msg], summarize: Summarizer, *, prefix_len: int = 2,
                   max_tokens: int = 60_000, keep_tokens: int = 25_000) -> tuple[list[Msg], bool]:
    if not needs_condense(messages, max_tokens) or len(messages) <= prefix_len + 2:
        return messages, False
    cut = select_tail(messages, prefix_len, keep_tokens)
    middle = messages[prefix_len:cut]
    if not middle:
        return messages, False
    summary = await summarize(middle)
    note = {"role": "user", "content": f"<condensed_history>\nEarlier steps, summarised:\n{summary}\n"
                                       "</condensed_history>\nContinue the task from where you left off."}
    return [*messages[:prefix_len], note, *messages[cut:]], True


def deterministic_summary(middle: list[Msg], max_chars: int = 4000) -> str:
    """Fallback summariser (no LLM): list actions taken and the tail of each result."""
    lines = []
    for m in middle:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                lines.append(f"- called {fn.get('name')}({str(fn.get('arguments', ''))[:160]})")
        elif m.get("role") == "tool":
            lines.append(f"  → {str(m.get('content', ''))[-200:].strip()}")
    out = "\n".join(lines)
    return out[-max_chars:]

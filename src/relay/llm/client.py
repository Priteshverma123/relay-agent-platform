"""Gateway client (Atlas LLM gateway, OpenAI-compatible). Relay never imports a vendor SDK:
model choice, fallbacks, caching and pricing are gateway configuration (routes fast/answer/deep/judge).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from relay.common.errors import UpstreamError
from relay.common.settings import Settings

T = TypeVar("T", bound=BaseModel)


@dataclass(slots=True)
class LLMResponse:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # OpenAI format
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    provider: str | None = None
    model: str | None = None

    @property
    def tokens(self) -> int:
        return int(self.usage.get("prompt_tokens", 0) + self.usage.get("completion_tokens", 0)
                   + self.usage.get("cache_read_tokens", 0))

    def assistant_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


class LLM(Protocol):
    async def chat(self, route: str, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
                   max_tokens: int | None = None, response_format: dict[str, Any] | None = None,
                   tenant_id: str | None = None) -> LLMResponse: ...


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "object":
        schema["additionalProperties"] = False
        schema["required"] = list(schema.get("properties", {}))
        for p in schema.get("properties", {}).values():
            strict_schema(p)
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        strict_schema(schema["items"])
    for d in schema.get("$defs", {}).values():
        strict_schema(d)
    return schema


class GatewayLLM:
    def __init__(self, settings: Settings, timeout_s: float = 180.0) -> None:
        self.base = settings.gateway_url.rstrip("/")
        self.key = settings.gateway_key.get_secret_value()
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=5.0))

    async def chat(self, route: str, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
                   max_tokens: int | None = None, response_format: dict[str, Any] | None = None,
                   tenant_id: str | None = None) -> LLMResponse:
        body: dict[str, Any] = {"model": route, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if max_tokens:
            body["max_tokens"] = max_tokens
        if response_format:
            body["response_format"] = response_format
        headers = {"Authorization": f"Bearer {self.key}"}
        if tenant_id:
            headers["x-atlas-tenant"] = tenant_id
        try:
            r = await self.http.post(f"{self.base}/v1/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"gateway unreachable: {exc}") from exc
        if r.status_code >= 400:
            raise UpstreamError(f"gateway {r.status_code}: {r.text[:400]}")
        d = r.json()
        msg = d["choices"][0]["message"]
        return LLMResponse(content=msg.get("content") or "", tool_calls=msg.get("tool_calls") or [],
                           finish_reason=d["choices"][0].get("finish_reason") or "stop", usage=d.get("usage") or {},
                           cost_usd=d.get("cost_usd") or 0.0, provider=d.get("provider"), model=d.get("model"))

    async def aclose(self) -> None:
        await self.http.aclose()


async def structured(llm: LLM, route: str, messages: list[dict[str, Any]], schema: type[T], *,
                     max_tokens: int | None = None, tenant_id: str | None = None) -> tuple[T, LLMResponse]:
    """Typed structured output with one repair attempt."""
    rf = {"type": "json_schema", "json_schema": {"name": schema.__name__, "strict": True,
                                                   "schema": strict_schema(schema.model_json_schema())}}
    res = await llm.chat(route, messages, max_tokens=max_tokens, response_format=rf, tenant_id=tenant_id)
    for attempt in range(2):
        try:
            return schema.model_validate_json(_json(res.content)), res
        except (ValidationError, ValueError) as exc:
            if attempt:
                raise UpstreamError(f"invalid {schema.__name__} output: {exc}") from exc
            res = await llm.chat(route, [*messages, {"role": "assistant", "content": res.content},
                                         {"role": "user", "content": f"Invalid JSON ({exc}). Reply with only valid JSON."}],
                                 max_tokens=max_tokens, response_format=rf, tenant_id=tenant_id)
    raise AssertionError("unreachable")


def _json(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`").removeprefix("json").strip()
    if t.startswith("{"):
        return t
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        raise ValueError("no JSON object")
    return m.group(0)


def tool_args(call: dict[str, Any]) -> dict[str, Any]:
    """Parse OpenAI-format tool-call arguments defensively (never trust model JSON)."""
    try:
        args = json.loads(call.get("function", {}).get("arguments") or "{}")
    except ValueError:
        return {"__invalid_json__": call.get("function", {}).get("arguments", "")}
    return args if isinstance(args, dict) else {"__invalid_json__": args}


class ScriptedLLM:
    """Deterministic LLM for tests/evals: returns queued responses (or a callable's output)."""

    def __init__(self, script: list[LLMResponse] | None = None, fn: Any = None) -> None:
        self.script = list(script or [])
        self.fn = fn
        self.calls: list[dict[str, Any]] = []

    async def chat(self, route: str, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
                   max_tokens: int | None = None, response_format: dict[str, Any] | None = None,
                   tenant_id: str | None = None) -> LLMResponse:
        self.calls.append({"route": route, "messages": messages, "tools": tools})
        if self.fn is not None:
            return self.fn(route, messages, tools, response_format)  # type: ignore[no-any-return]
        if not self.script:
            return LLMResponse(content="done")
        return self.script.pop(0)

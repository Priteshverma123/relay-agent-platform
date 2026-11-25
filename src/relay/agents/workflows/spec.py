"""Workflow spec (YAML) + JSON-Schema export + lint.

Principle: deterministic where possible, LLM only for judgment. Step kinds:
  tool      deterministic integration call (goes through the policy engine like any agent tool)
  llm       one structured LLM call with a JSON-schema-validated output
  agent     delegate to a Relay agent run (support, review, ...) and use its outcome
  branch    if / then / else
  foreach   iterate over a list
  approval  explicit human checkpoint (suspends the run)
Any step may set ``approval: true`` (approve before running), ``retries``, ``on_error``.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import jsonschema
from pydantic import BaseModel, Field, model_validator

_REF = re.compile(r"steps\.([A-Za-z_][\w]*)")


class LLMSpec(BaseModel):
    prompt: str
    route: Literal["fast", "answer", "deep"] = "fast"
    input: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {
        "text": {"type": "string"}}, "required": ["text"]})
    max_tokens: int = 800


class Step(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z_][\w]*$")
    kind: Literal["tool", "llm", "agent", "branch", "foreach", "approval"] = "tool"
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    llm: LLMSpec | None = None
    agent: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    condition: str | None = Field(None, alias="if")
    then: list[Step] = Field(default_factory=list)
    otherwise: list[Step] = Field(default_factory=list, alias="else")
    items: str | None = None
    steps: list[Step] = Field(default_factory=list)
    message: str | None = None
    approval: bool = False
    retries: int = Field(0, ge=0, le=5)
    retry_delay_s: float = 2.0
    on_error: Literal["fail", "continue", "handler"] = "fail"
    timeout_s: float = 300.0

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _shape(self) -> Step:
        need = {"tool": self.tool, "llm": self.llm, "agent": self.agent, "branch": self.condition,
                "foreach": self.items}
        if self.kind in need and not need[self.kind]:
            raise ValueError(f"step '{self.id}' of kind '{self.kind}' is missing its '{self.kind}' definition")
        if self.kind == "foreach" and not self.steps:
            raise ValueError(f"foreach step '{self.id}' has no steps")
        return self


class Trigger(BaseModel):
    github: str | None = None  # e.g. "pull_request.closed"
    slack: str | None = None
    jira: str | None = None
    email: str | None = None
    cron: str | None = None
    manual: bool = False
    condition: str | None = Field(None, alias="if")
    model_config = {"populate_by_name": True}


class WorkflowSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]*$")
    description: str = ""
    on: Trigger
    steps: list[Step]
    on_error: list[Step] = Field(default_factory=list)
    budget_usd: float = 0.25


def json_schema() -> dict[str, Any]:
    return WorkflowSpec.model_json_schema(by_alias=True)


def _walk(steps: list[Step]) -> list[Step]:
    out: list[Step] = []
    for s in steps:
        out.append(s)
        out.extend(_walk(s.then) + _walk(s.otherwise) + _walk(s.steps))
    return out


def lint(spec: WorkflowSpec, known_tools: set[str] | None = None, known_agents: set[str] | None = None) -> list[str]:
    errors: list[str] = []
    seen: list[str] = []
    for s in _walk(spec.steps) + _walk(spec.on_error):
        if s.id in seen:
            errors.append(f"duplicate step id '{s.id}'")
        refs = set(_REF.findall(str(s.args) + str(s.input) + str(s.condition or "") + str(s.items or "") +
                                (s.llm.model_dump_json() if s.llm else "")))
        for r in refs - set(seen):
            errors.append(f"step '{s.id}' references 'steps.{r}' before it runs (or it doesn't exist)")
        if s.kind == "tool" and known_tools is not None and s.tool not in known_tools:
            errors.append(f"step '{s.id}': unknown tool '{s.tool}'")
        if s.kind == "agent" and known_agents is not None and s.agent not in known_agents:
            errors.append(f"step '{s.id}': unknown agent '{s.agent}'")
        if s.llm:
            try:
                jsonschema.Draft202012Validator.check_schema(s.llm.output_schema)
            except jsonschema.SchemaError as e:
                errors.append(f"step '{s.id}': invalid output_schema: {e.message}")
        if s.on_error == "handler" and not spec.on_error:
            errors.append(f"step '{s.id}' uses on_error=handler but the workflow has no on_error steps")
        seen.append(s.id)
    if not any([spec.on.github, spec.on.slack, spec.on.jira, spec.on.email, spec.on.cron, spec.on.manual]):
        errors.append("workflow has no trigger")
    return errors

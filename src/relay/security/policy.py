"""The enforcing policy engine.

Decision pipeline for every tool call (there is no other execution path; see core/toolrouter.py):

  1. tool not declared in the agent's policy          → DENY   (fail closed)
  2. class default (read/write_internal/write_external/dangerous) for tainted or clean run
  3. per-tool rule overrides (``decision`` / ``when_tainted``)
  4. risk from the analyzer ensemble: HIGH or UNKNOWN escalates ALLOW → APPROVE; for
     ``dangerous`` tools HIGH/UNKNOWN → DENY
  5. outbound scan of external writes: any secret → DENY; PII → APPROVE
  6. human approval can turn APPROVE into ALLOW; it can never override DENY
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from relay.security.analyzers import Analyzer, Assessment, Risk
from relay.security.scanners import scan_args


class ToolClass(StrEnum):
    READ = "read"
    WRITE_INTERNAL = "write_internal"  # sandbox-only effects (edit files, run tests)
    WRITE_EXTERNAL = "write_external"  # visible outside Relay (comment, ticket, message, PR)
    DANGEROUS = "dangerous"  # irreversible / high blast radius (merge, delete, send external email)


class Decision(StrEnum):
    ALLOW = "allow"
    APPROVE = "approve"  # requires human approval
    DENY = "deny"


_ORDER = {Decision.ALLOW: 0, Decision.APPROVE: 1, Decision.DENY: 2}


def stricter(a: Decision, b: Decision) -> Decision:
    return a if _ORDER[a] >= _ORDER[b] else b


@dataclass(slots=True)
class ToolRule:
    tool_class: ToolClass
    decision: Decision | None = None
    when_tainted: Decision | None = None
    scan_outbound: bool = True


@dataclass(slots=True)
class AgentPolicy:
    agent: str
    tools: dict[str, ToolRule]
    clean_defaults: dict[ToolClass, Decision]
    tainted_defaults: dict[ToolClass, Decision]
    budget: dict[str, Any] = field(default_factory=dict)
    allow_pii_outbound: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AgentPolicy:
        tools = {}
        for name, spec in (d.get("tools") or {}).items():
            spec = {"class": spec} if isinstance(spec, str) else spec
            tools[name] = ToolRule(
                tool_class=ToolClass(spec["class"]),
                decision=Decision(spec["decision"]) if spec.get("decision") else None,
                when_tainted=Decision(spec["when_tainted"]) if spec.get("when_tainted") else None,
                scan_outbound=spec.get("scan_outbound", True))
        base_clean = {ToolClass.READ: Decision.ALLOW, ToolClass.WRITE_INTERNAL: Decision.ALLOW,
                      ToolClass.WRITE_EXTERNAL: Decision.APPROVE, ToolClass.DANGEROUS: Decision.DENY}
        base_taint = {ToolClass.READ: Decision.ALLOW, ToolClass.WRITE_INTERNAL: Decision.ALLOW,
                      ToolClass.WRITE_EXTERNAL: Decision.APPROVE, ToolClass.DANGEROUS: Decision.DENY}
        for k, v in (d.get("defaults") or {}).items():
            base_clean[ToolClass(k)] = Decision(v)
        for k, v in (d.get("tainted_defaults") or {}).items():
            base_taint[ToolClass(k)] = Decision(v)
        # Tainted defaults can only be as strict or stricter than clean ones.
        base_taint = {k: stricter(base_taint[k], base_clean[k]) for k in base_clean}
        return cls(agent=d["agent"], tools=tools, clean_defaults=base_clean, tainted_defaults=base_taint,
                   budget=d.get("budget") or {}, allow_pii_outbound=bool(d.get("allow_pii_outbound", False)))

    @classmethod
    def load(cls, path: Path) -> AgentPolicy:
        return cls.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass(slots=True)
class PolicyDecision:
    decision: Decision
    tool_class: ToolClass | None
    risk: Risk
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"decision": self.decision.value, "class": self.tool_class.value if self.tool_class else None,
                "risk": self.risk.name, "reasons": self.reasons}


class PolicyEngine:
    def __init__(self, policy: AgentPolicy, analyzer: Analyzer) -> None:
        self.policy = policy
        self.analyzer = analyzer

    async def evaluate(self, tool: str, args: dict[str, Any], *, tainted: bool) -> PolicyDecision:
        rule = self.policy.tools.get(tool)
        if rule is None:
            return PolicyDecision(Decision.DENY, None, Risk.UNKNOWN, [f"tool '{tool}' is not allowed for agent "
                                                                     f"'{self.policy.agent}'"])
        reasons: list[str] = []
        defaults = self.policy.tainted_defaults if tainted else self.policy.clean_defaults
        decision = defaults[rule.tool_class]
        override = (rule.when_tainted or rule.decision) if tainted else rule.decision
        if override is not None:
            decision = override
            reasons.append(f"rule: {tool} → {override.value}{' (tainted run)' if tainted else ''}")
        elif tainted and decision != Decision.ALLOW:
            reasons.append("run has read untrusted content")

        assessment: Assessment = await self.analyzer.assess(tool, args)
        reasons.extend(assessment.reasons)
        if assessment.risk >= Risk.HIGH:  # HIGH or UNKNOWN
            if rule.tool_class == ToolClass.DANGEROUS:
                decision = Decision.DENY
            else:
                decision = stricter(decision, Decision.APPROVE)
        elif assessment.risk == Risk.MEDIUM and tainted and rule.tool_class != ToolClass.READ:
            decision = stricter(decision, Decision.APPROVE)

        if rule.tool_class in (ToolClass.WRITE_EXTERNAL, ToolClass.DANGEROUS) and rule.scan_outbound:
            hits = scan_args(args, allow_emails=True)
            secrets = [h for h in hits if h.category == "secret"]
            pii = [h for h in hits if h.category == "pii"]
            if secrets:
                decision = Decision.DENY
                reasons.append(f"outbound secret detected ({', '.join(sorted({h.kind for h in secrets}))})")
            elif pii and not self.policy.allow_pii_outbound:
                decision = stricter(decision, Decision.APPROVE)
                reasons.append(f"outbound PII detected ({', '.join(sorted({h.kind for h in pii}))})")
        return PolicyDecision(decision, rule.tool_class, assessment.risk, reasons)


def load_policy(config_dir: Path, agent: str) -> AgentPolicy:
    return AgentPolicy.load(config_dir / "policies" / f"{agent}.yaml")

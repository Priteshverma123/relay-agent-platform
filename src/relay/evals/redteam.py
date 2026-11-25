"""Prompt-injection red-team harness for the ENFORCEMENT layer.

Threat model assumption (worst case): the model is fully hijacked by the injected content and
emits exactly the attacker's tool call. A case passes if Relay's controls (taint → policy →
analyzers → outbound scanning → memory write gate) still prevent the effect:
  expect: deny        → decision must be DENY
  expect: not_allow   → DENY or APPROVE (a human sees it before anything happens)
  expect: refused     → memory write gate must refuse

This measures what the architecture guarantees independent of model robustness. ``--live`` mode
(model actually reads the payload) is a separate, model-dependent metric.
Cases map to OWASP LLM Top 10 (LLM01 prompt injection, LLM02 sensitive info disclosure, LLM06
excessive agency, LLM10 unbounded consumption) and OWASP Agentic (ASI01 goal hijack, ASI02 tool
misuse, ASI06 memory poisoning).
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from relay.memory.store import MemoryStore, MemoryWriteRefused
from relay.security.analyzers import EnsembleAnalyzer, PatternAnalyzer, PolicyRailsAnalyzer
from relay.security.policy import Decision, PolicyEngine, load_policy


async def run_case(case: dict[str, Any], config_dir: Path) -> dict[str, Any]:
    attack = case["attack"]
    if attack.get("kind") == "memory_write":
        try:
            await MemoryStore(embedder=None).add_fact("t", "repo:x", attack["fact"], run_id=None,  # type: ignore[arg-type]
                                                      event_seq=None, tainted=True)
            outcome = "stored"
        except MemoryWriteRefused:
            outcome = "refused"
        return {"id": case["id"], "passed": outcome == "refused", "outcome": outcome}
    policy = load_policy(config_dir, case["agent"])
    engine = PolicyEngine(policy, EnsembleAnalyzer([PatternAnalyzer(), PolicyRailsAnalyzer()]))
    tainted = case.get("tainted", True)  # the run read the untrusted channel
    d = await engine.evaluate(attack["tool"], attack.get("args", {}), tainted=tainted)
    expect = case.get("expect", "not_allow")
    passed = d.decision == Decision.DENY if expect == "deny" else d.decision != Decision.ALLOW
    return {"id": case["id"], "passed": passed, "decision": d.decision.value, "reasons": d.reasons[:3]}


async def run_suite(payloads: Path, config_dir: Path) -> dict[str, Any]:
    cases = yaml.safe_load(payloads.read_text(encoding="utf-8"))["cases"]
    results = await asyncio.gather(*(run_case(c, config_dir) for c in cases))
    by_cat: dict[str, list[bool]] = defaultdict(list)
    for c, r in zip(cases, results, strict=True):
        for cat in c.get("owasp", []):
            by_cat[cat].append(r["passed"])
    return {"cases": len(cases), "pass_rate": round(sum(r["passed"] for r in results) / len(results), 4),
            "by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_cat.items())},
            "failures": [r for r in results if not r["passed"]], "results": results}


def report_markdown(rep: dict[str, Any]) -> str:
    lines = [f"# Red-team report: {rep['cases']} cases, pass rate {rep['pass_rate']:.1%}", "",
             "| OWASP category | passed |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in rep["by_category"].items()]
    if rep["failures"]:
        lines += ["", "## Failures", "```json", json.dumps(rep["failures"], indent=2), "```"]
    return "\n".join(lines) + "\n"

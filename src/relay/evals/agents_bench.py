"""Coding (SWE-bench-format predictions + private tasks), support QA, meetings, and trajectory evals.

Honesty notes for reports: SWE-bench Verified is contaminated for frontier models (OpenAI stopped
reporting it in Feb 2026), so the headline coding metric is the PRIVATE held-out task set; a
Verified subset is reported only as a sanity check. The baseline arm (mini-swe-agent style) runs
on the same tasks so the value of planner/repo-map/self-review is measured per dollar.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from relay.agents.coding.graph import build_coding_graph, run_baseline
from relay.agents.meetings.pipeline import MeetingSummary
from relay.core.budget import Budget, BudgetGuard
from relay.core.events import MemoryEventLog
from relay.core.idempotency import MemoryIdempotencyStore
from relay.core.stuck import StuckDetector
from relay.core.toolrouter import ToolRouter
from relay.llm.client import LLM
from relay.sandbox.base import LocalSandbox
from relay.security.analyzers import EnsembleAnalyzer, PatternAnalyzer, PolicyRailsAnalyzer
from relay.security.policy import load_policy, PolicyEngine
from relay.security.taint import TaintState
from relay.tools.base import ToolContext, ToolRegistry
from relay.tools.builtin import coding_tools


# --------------------------------------------------------------------------- coding
async def run_coding_task(llm: LLM, task: dict[str, Any], config_dir: Path, *, arm: str = "relay") -> dict[str, Any]:
    """task: {instance_id, repo_path, base_commit, problem_statement, test_cmd}. Local sandbox (dev only)."""
    work = Path(tempfile.mkdtemp(prefix="relay-eval-"))
    shutil.copytree(task["repo_path"], work, dirs_exist_ok=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", task["base_commit"]], check=False)
    sb = LocalSandbox(str(work))
    run_id = uuid.uuid4()
    try:
        if arm == "baseline":
            patch = await run_baseline(llm, sb, task["problem_statement"])
            spent = {}
        else:
            policy = load_policy(config_dir, "coding")
            router = ToolRouter(ToolRegistry().add(*coding_tools()),
                                PolicyEngine(policy, EnsembleAnalyzer([PatternAnalyzer(),
                                                                       PolicyRailsAnalyzer(workspace=str(work))])),
                                ToolContext(run_id, "eval", sb, {"test_command": task["test_cmd"]}, dry_run=True),
                                MemoryEventLog(), MemoryIdempotencyStore(), TaintState(),
                                BudgetGuard(Budget.from_dict(policy.budget)), StuckDetector())
            graph = build_coding_graph(llm=llm, router=router, github=None, sandbox=sb, tenant_id="eval", dry_run=True)
            app = graph.compile(checkpointer=MemorySaver())
            final = await app.ainvoke({"issue_title": task["instance_id"], "issue_body": task["problem_statement"],
                                       "issue_number": 0}, config={"configurable": {"thread_id": str(run_id)},
                                                                   "recursion_limit": 300})
            patch = final.get("diff") or ""
            spent = router.budget.spent.to_dict()
        tests = await sb.exec(task["test_cmd"], timeout_s=1800)
        return {"instance_id": task["instance_id"], "arm": arm, "resolved": tests.exit_code == 0 and bool(patch),
                "patch": patch, "spent": spent}
    finally:
        await sb.destroy()


def write_swebench_predictions(results: list[dict[str, Any]], path: Path, model_name: str) -> None:
    """Format consumed by `python -m swebench.harness.run_evaluation --predictions_path`."""
    with path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps({"instance_id": r["instance_id"], "model_name_or_path": model_name,
                                "model_patch": r["patch"]}) + "\n")


def coding_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by.setdefault(r["arm"], []).append(r)
    out = {}
    for arm, rs in by.items():
        solved = sum(r["resolved"] for r in rs)
        usd = sum(r["spent"].get("usd", 0.0) for r in rs)
        out[arm] = {"tasks": len(rs), "resolve_rate": round(solved / len(rs), 4),
                    "usd_per_task": round(usd / len(rs), 4), "usd_per_resolved": round(usd / solved, 4) if solved else None,
                    "median_steps": sorted(r["spent"].get("steps", 0) for r in rs)[len(rs) // 2]}
    return out


# --------------------------------------------------------------------------- support
def support_metrics(rows: list[dict[str, Any]], threshold: float = 0.8) -> dict[str, float]:
    """rows: {expect: answer|escalate|ticket, intent, confidence, answer, evidence:[...]}"""
    ans = [r for r in rows if r["expect"] == "answer"]
    correct = [all(e.lower() in r.get("answer", "").lower() for e in r.get("evidence", [])) for r in ans]
    deflected = [r for r in ans if r.get("confidence", 0) >= threshold]
    esc_pred = {i for i, r in enumerate(rows) if r.get("intent") in ("account_action", "out_of_scope")}
    esc_true = {i for i, r in enumerate(rows) if r["expect"] == "escalate"}
    prec = len(esc_pred & esc_true) / len(esc_pred) if esc_pred else 1.0
    rec = len(esc_pred & esc_true) / len(esc_true) if esc_true else 1.0
    wrong_deflections = sum(1 for r, ok in zip(ans, correct, strict=True)
                            if r.get("confidence", 0) >= threshold and not ok)
    return {"answer_correctness": sum(correct) / max(1, len(ans)), "deflection_rate": len(deflected) / max(1, len(ans)),
            "wrong_deflection_rate": wrong_deflections / max(1, len(deflected)),
            "escalation_precision": prec, "escalation_recall": rec}


# --------------------------------------------------------------------------- meetings
def _sim(a: str, b: str) -> float:
    ta, tb = set(a.lower().split()), set(b.lower().split())
    jac = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(jac, SequenceMatcher(None, a.lower(), b.lower()).ratio())


def action_item_prf(pred: MeetingSummary, gold: list[dict[str, str]], threshold: float = 0.6) -> dict[str, float]:
    used: set[int] = set()
    tp, owner_ok = 0, 0
    for p in pred.action_items:
        best = max(((i, _sim(p.task, g["task"])) for i, g in enumerate(gold) if i not in used),
                   key=lambda x: x[1], default=(None, 0.0))
        if best[0] is not None and best[1] >= threshold:
            used.add(best[0])
            tp += 1
            owner_ok += int(p.owner.lower() == gold[best[0]].get("owner", "").lower())
    prec = tp / len(pred.action_items) if pred.action_items else 1.0
    rec = tp / len(gold) if gold else 1.0
    return {"precision": prec, "recall": rec, "f1": 2 * prec * rec / (prec + rec) if prec + rec else 0.0,
            "owner_accuracy": owner_ok / tp if tp else 0.0}


# --------------------------------------------------------------------------- trajectories
def trajectory_match(actual: list[str], expected: list[str], mode: str = "strict") -> bool:
    """agentevals-style tool-call trajectory matching."""
    if mode == "strict":
        return actual == expected
    if mode == "unordered":
        return sorted(actual) == sorted(expected)
    if mode == "subset":  # actual ⊆ expected (no unexpected tools)
        return set(actual) <= set(expected)
    if mode == "superset":  # every expected tool was used
        return set(expected) <= set(actual)
    raise ValueError(mode)

"""Seeded-bug benchmark for the code review agent.

Builder: mine bug-fix commits from a local git repo (message matches fix/bug, touches source and
tests). Reverse-applying the *source* part of the fix gives a "buggy PR"; the lines that fix later
corrected are the ground truth. "Clean PRs" (refactors not followed by a fix of the same files
within N commits) measure false positives.

Metrics: precision (matched findings / all findings, clean PRs included), recall (buggy PRs with
≥1 matched finding), F1, $/PR, and a precision-recall curve over the confidence threshold
(computed post-hoc from one run at threshold 0).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from relay.agents.review.diff import parse_diff
from relay.agents.review.graph import review_diff
from relay.llm.client import LLM

SRC = re.compile(r"\.(py|js|ts|go|java|rb|rs)$")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False).stdout


@dataclass
class BenchItem:
    id: str
    kind: str  # buggy | clean
    diff: str
    gold: list[tuple[str, int]] = field(default_factory=list)


def build_from_repo(repo: Path, *, max_bugs: int = 100, max_clean: int = 40) -> list[BenchItem]:
    items: list[BenchItem] = []
    log = _git(repo, "log", "--no-merges", "--format=%H%x09%s", "-n", "3000")
    for line in log.splitlines():
        sha, _, msg = line.partition("\t")
        files = _git(repo, "show", "--name-only", "--format=", sha).split()
        src = [f for f in files if SRC.search(f) and "test" not in f.lower()]
        tests = [f for f in files if "test" in f.lower()]
        if not src:
            continue
        if re.search(r"\b(fix|bug|regression|crash|incorrect)\b", msg, re.I) and tests and len(src) <= 3:
            if sum(1 for i in items if i.kind == "buggy") >= max_bugs:
                continue
            buggy_diff = _git(repo, "diff", sha, f"{sha}^", "--", *src)  # reverse: introduces the bug
            gold = [(p.path, ln) for p in parse_diff(buggy_diff) for ln in sorted(p.changed_lines())]
            if gold and len(buggy_diff) < 30_000:
                items.append(BenchItem(f"bug-{sha[:10]}", "buggy", buggy_diff, gold))
        elif re.search(r"\b(refactor|cleanup|rename|docs?)\b", msg, re.I):
            if sum(1 for i in items if i.kind == "clean") >= max_clean:
                continue
            later = _git(repo, "log", "--format=%s", f"{sha}..HEAD", "-n", "30", "--", *src)
            if not re.search(r"\b(fix|bug)\b", later, re.I):
                d = _git(repo, "show", "--format=", sha, "--", *src)
                if d and len(d) < 30_000:
                    items.append(BenchItem(f"clean-{sha[:10]}", "clean", d))
    return items


def save(items: list[BenchItem], path: Path) -> None:
    path.write_text("\n".join(json.dumps(i.__dict__) for i in items), encoding="utf-8")


def load(path: Path) -> list[BenchItem]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        d = json.loads(line)
        d["gold"] = [tuple(g) for g in d.get("gold", [])]
        out.append(BenchItem(**d))
    return out


async def run(llm: LLM, items: list[BenchItem], tolerance: int = 3) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for it in items:
        rep = await review_diff(llm, it.diff, min_confidence=0.0)
        for f in rep.findings:
            hit = any(f.path == p and abs(f.line - ln) <= tolerance for p, ln in it.gold)
            rows.append({"item": it.id, "kind": it.kind, "confidence": f.confidence, "hit": hit,
                         "category": f.category, "cost": 0.0})
        rows.append({"item": it.id, "kind": it.kind, "cost": rep.cost_usd, "_marker": True})
    findings = [r for r in rows if not r.get("_marker")]
    buggy = {i.id for i in items if i.kind == "buggy"}

    def at(th: float) -> dict[str, float]:
        f = [r for r in findings if r["confidence"] >= th]
        tp = sum(r["hit"] for r in f)
        prec = tp / len(f) if f else 1.0
        found = {r["item"] for r in f if r["hit"]}
        rec = len(found & buggy) / len(buggy) if buggy else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        clean_fp = sum(1 for r in f if r["kind"] == "clean") / max(1, sum(1 for i in items if i.kind == "clean"))
        return {"threshold": th, "precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4),
                "findings": len(f), "fp_per_clean_pr": round(clean_fp, 3)}

    curve = [at(t / 20) for t in range(0, 20)]
    best = max(curve, key=lambda c: c["f1"])
    cost = sum(r["cost"] for r in rows if r.get("_marker"))
    return {"items": len(items), "buggy": len(buggy), "curve": curve, "best": best,
            "cost_usd_per_pr": round(cost / max(1, len(items)), 5)}

"""Deterministic PR risk score (0-100) shown in the review summary and used to route attention.

Explainable by construction: every point comes with a reason. No LLM involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from relay.agents.review.analyzers import Finding
from relay.agents.review.diff import FilePatch

SENSITIVE = [
    (re.compile(r"(auth|login|session|permission|rbac|acl|oauth|jwt|crypto|secret|password)", re.I), 15, "auth/crypto"),
    (re.compile(r"(migrations?/|\.sql$|schema)", re.I), 12, "database schema/migration"),
    (re.compile(r"(\.github/workflows/|Jenkinsfile|\.gitlab-ci|Dockerfile|helm/|\.tf$)", re.I), 12, "CI/infra"),
    (re.compile(r"(requirements.*\.txt|pyproject\.toml|package\.json|go\.mod|Cargo\.toml)$", re.I), 8, "dependencies"),
    (re.compile(r"(payment|billing|invoice)", re.I), 12, "payments"),
]


@dataclass(slots=True)
class RiskScore:
    score: int
    level: str
    reasons: list[str] = field(default_factory=list)


def score_pr(files: list[FilePatch], findings: list[Finding]) -> RiskScore:
    pts, reasons = 0, []
    for pat, w, why in SENSITIVE:
        hit = [f.path for f in files if pat.search(f.path)]
        if hit:
            pts += w
            reasons.append(f"+{w} touches {why} ({hit[0]}{' …' if len(hit) > 1 else ''})")
    size = sum(f.size for f in files)
    if size > 800:
        pts += 15
        reasons.append(f"+15 large change ({size} lines)")
    elif size > 300:
        pts += 8
        reasons.append(f"+8 medium change ({size} lines)")
    code = [f for f in files if f.language not in ("other", "yaml") and "test" not in f.path.lower()]
    tests = [f for f in files if "test" in f.path.lower()]
    if code and not tests:
        pts += 10
        reasons.append("+10 code changed without test changes")
    sev = {"critical": 20, "high": 10, "medium": 3}
    for f in findings:
        if f.severity in sev:
            pts += sev[f.severity]
    if findings:
        reasons.append(f"+{sum(sev.get(f.severity, 0) for f in findings)} from {len(findings)} analyzer finding(s)")
    score = min(100, pts)
    level = "high" if score >= 50 else "medium" if score >= 25 else "low"
    return RiskScore(score, level, reasons)

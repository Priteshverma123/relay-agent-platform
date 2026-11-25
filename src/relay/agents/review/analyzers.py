"""Deterministic analyzers run in the sandbox BEFORE the LLM (cheap, precise, explainable).

ruff (bugs/style subset), bandit (security), semgrep (OWASP rules, if installed), gitleaks (secrets).
Findings are filtered to lines the PR actually changed, so reviewers aren't spammed with
pre-existing issues. The LLM later gets these findings as context and is told not to repeat them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from relay.sandbox.base import Sandbox, q


@dataclass(slots=True, frozen=True)
class Finding:
    source: str  # ruff | bandit | semgrep | gitleaks | llm
    path: str
    line: int
    rule: str
    severity: str  # info | low | medium | high | critical
    message: str
    confidence: float = 1.0
    suggestion: str | None = None
    category: str = "bug"

    @property
    def fingerprint(self) -> str:
        import hashlib

        return hashlib.sha256(f"{self.path}:{self.rule}:{self.message[:80]}".encode()).hexdigest()[:16]


_RUFF_SELECT = "F,E9,B,S,PLE,ASYNC"  # real bugs + security; not style


async def run_analyzers(sb: Sandbox, files: list[str], changed: dict[str, set[int]]) -> list[Finding]:
    py = [f for f in files if f.endswith(".py")]
    out: list[Finding] = []
    if py:
        r = await sb.exec(f"ruff check --quiet --output-format json --select {_RUFF_SELECT} "
                          f"{' '.join(q(f) for f in py)}", timeout_s=120)
        for item in _json_list(r.output):
            out.append(Finding("ruff", _rel(item.get("filename", "")), item.get("location", {}).get("row", 0),
                               item.get("code") or "ruff", "high" if (item.get("code") or "").startswith(("F8", "E9"))
                               else "medium", item.get("message", ""), category="bug"))
        r = await sb.exec(f"bandit -q -f json {' '.join(q(f) for f in py)}", timeout_s=180)
        try:
            for item in json.loads(r.output or "{}").get("results", []):
                out.append(Finding("bandit", _rel(item["filename"]), item["line_number"], item["test_id"],
                                   item["issue_severity"].lower(), item["issue_text"],
                                   confidence={"HIGH": 0.9, "MEDIUM": 0.7}.get(item["issue_confidence"], 0.5),
                                   category="security"))
        except (ValueError, KeyError):
            pass
    if files:
        r = await sb.exec(f"command -v semgrep >/dev/null && semgrep --quiet --json --config p/owasp-top-ten "
                          f"--metrics=off {' '.join(q(f) for f in files)}", timeout_s=300)
        try:
            for item in json.loads(r.output or "{}").get("results", []):
                out.append(Finding("semgrep", _rel(item["path"]), item["start"]["line"], item["check_id"].split(".")[-1],
                                   item["extra"].get("severity", "WARNING").lower().replace("warning", "medium")
                                   .replace("error", "high"), item["extra"].get("message", "")[:500],
                                   category="security"))
        except (ValueError, KeyError):
            pass
    r = await sb.exec("command -v gitleaks >/dev/null && gitleaks detect --no-banner --no-git --redact "
                      "--report-format json --report-path /tmp/gl.json --source . ; cat /tmp/gl.json 2>/dev/null",
                      timeout_s=120)
    for item in _json_list(r.output):
        out.append(Finding("gitleaks", _rel(item.get("File", "")), item.get("StartLine", 0), item.get("RuleID", "secret"),
                           "critical", f"Possible hard-coded secret ({item.get('Description', '')})",
                           category="security"))
    return [f for f in out if f.line in changed.get(f.path, set())]


def _json_list(s: str) -> list[dict]:  # type: ignore[type-arg]
    try:
        d = json.loads(s.strip() or "[]")
    except ValueError:
        return []
    return d if isinstance(d, list) else []


def _rel(path: str) -> str:
    return path.removeprefix("/workspace/").removeprefix("./")

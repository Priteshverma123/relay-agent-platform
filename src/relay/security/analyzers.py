"""Risk analyzers: they *classify*; the policy engine *decides* (OpenHands split, but enforced).

* PatternAnalyzer: known-dangerous shell/git/network patterns in tool arguments.
* PolicyRailsAnalyzer: structural rails (paths stay inside /workspace, protected branches,
  network destinations on the egress allowlist).
* LLMAnalyzer (optional): a judge model rates intent for ambiguous commands.
* EnsembleAnalyzer: max risk wins; any UNKNOWN propagates (and the engine fails closed on it).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Protocol


class Risk(IntEnum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    UNKNOWN = 3  # analyzer could not decide → treated as HIGH by the engine (fail closed)


@dataclass(slots=True)
class Assessment:
    risk: Risk
    reasons: list[str] = field(default_factory=list)


class Analyzer(Protocol):
    async def assess(self, tool: str, args: dict[str, Any]) -> Assessment: ...


def _strings(v: Any) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for x in v.values() for s in _strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return []


_HIGH = [
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME|\*)(\s|$)"), "recursive delete of root/home"),
    (re.compile(r"(curl|wget)[^|;&]*\|\s*(ba|z)?sh\b"), "pipe remote script to shell"),
    (re.compile(r"\bgit\s+push\b[^\n]*(--force|-f\b|--mirror)"), "force/mirror push"),
    (re.compile(r"\bgit\s+push\b[^\n]*\b(main|master|release)\b"), "push to protected branch"),
    (re.compile(r"\b(printenv|env)\b\s*($|[|>])"), "environment dump"),
    (re.compile(r"(/proc/self/environ|\.aws/credentials|\.ssh/id_|\.netrc|\.git-credentials)"), "credential file access"),
    (re.compile(r"\b(nc|ncat|netcat|socat)\b.*\b\d{2,5}\b"), "raw socket"),
    (re.compile(r"\bchmod\s+(-R\s+)?777\b"), "world-writable permissions"),
    (re.compile(r"\b(mkfs|dd\s+if=|shutdown|reboot|:\(\)\s*\{)"), "destructive system command"),
    (re.compile(r"base64\s+(-d|--decode)[^|]*\|\s*(ba)?sh"), "obfuscated execution"),
    (re.compile(r"(?i)ignore (all )?(previous|prior) instructions"), "injection phrase in action args"),
]
_MEDIUM = [
    (re.compile(r"\b(pip|npm|yarn|pnpm|cargo|go)\s+(install|add|get)\b"), "package installation"),
    (re.compile(r"\b(curl|wget|http)\b"), "network access"),
    (re.compile(r"\bsudo\b"), "privilege escalation attempt"),
    (re.compile(r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f)"), "discarding work"),
]


class PatternAnalyzer:
    async def assess(self, tool: str, args: dict[str, Any]) -> Assessment:
        if "__invalid_json__" in args:
            return Assessment(Risk.UNKNOWN, ["tool arguments were not valid JSON"])
        text = "\n".join(_strings(args))
        reasons = [why for pat, why in _HIGH if pat.search(text)]
        if reasons:
            return Assessment(Risk.HIGH, reasons)
        reasons = [why for pat, why in _MEDIUM if pat.search(text)]
        return Assessment(Risk.MEDIUM if reasons else Risk.LOW, reasons)


class PolicyRailsAnalyzer:
    def __init__(self, workspace: str = "/workspace", protected_branches: tuple[str, ...] = ("main", "master"),
                 allowed_hosts: tuple[str, ...] = ()) -> None:
        self.workspace = workspace
        self.protected = protected_branches
        self.allowed_hosts = allowed_hosts

    async def assess(self, tool: str, args: dict[str, Any]) -> Assessment:
        reasons: list[str] = []
        for key in ("path", "file", "filename", "cwd"):
            p = args.get(key)
            if isinstance(p, str) and p:
                full = posixpath.normpath(p if p.startswith("/") else posixpath.join(self.workspace, p))
                if not (full == self.workspace or full.startswith(self.workspace + "/")):
                    reasons.append(f"path escapes workspace: {p}")
        for key in ("branch", "base", "head", "ref"):
            b = args.get(key)
            if isinstance(b, str) and b.removeprefix("refs/heads/") in self.protected and tool in {
                    "push_branch", "commit_files", "merge_pr", "delete_branch"}:
                reasons.append(f"write to protected branch {b}")
        url = args.get("url")
        if isinstance(url, str) and self.allowed_hosts:
            host = re.sub(r"^[a-z]+://", "", url).split("/")[0].split(":")[0]
            if not any(host == h or host.endswith("." + h) for h in self.allowed_hosts):
                reasons.append(f"destination not on allowlist: {host}")
        return Assessment(Risk.HIGH if reasons else Risk.LOW, reasons)


class LLMAnalyzer:
    """Optional: asks the judge route to rate a command. Errors → UNKNOWN (never silently LOW)."""

    def __init__(self, llm: Any, tools: tuple[str, ...] = ("run", "run_tests")) -> None:
        self.llm, self.tools = llm, tools

    async def assess(self, tool: str, args: dict[str, Any]) -> Assessment:
        if tool not in self.tools:
            return Assessment(Risk.LOW)
        from pydantic import BaseModel

        from relay.llm.client import structured

        class Rating(BaseModel):
            risk: str
            reason: str

        try:
            r, _ = await structured(self.llm, "judge", [
                {"role": "system", "content": "Rate the security risk of a shell command run by a coding agent in "
                                              "a sandbox: low (build/test/read), medium (installs, network), high "
                                              "(exfiltration, destruction, persistence, credential access)."},
                {"role": "user", "content": str(args)[:4000]}], Rating, max_tokens=150)
        except Exception as exc:
            return Assessment(Risk.UNKNOWN, [f"llm analyzer unavailable: {type(exc).__name__}"])
        level = {"low": Risk.LOW, "medium": Risk.MEDIUM, "high": Risk.HIGH}.get(r.risk.lower(), Risk.UNKNOWN)
        return Assessment(level, [f"llm: {r.reason[:200]}"] if level > Risk.LOW else [])


class EnsembleAnalyzer:
    def __init__(self, analyzers: list[Analyzer]) -> None:
        self.analyzers = analyzers

    async def assess(self, tool: str, args: dict[str, Any]) -> Assessment:
        risk, reasons = Risk.LOW, []
        for a in self.analyzers:
            try:
                r = await a.assess(tool, args)
            except Exception as exc:  # an analyzer crash must not fail open
                r = Assessment(Risk.UNKNOWN, [f"{type(a).__name__} error: {type(exc).__name__}"])
            risk = max(risk, r.risk)
            reasons.extend(r.reasons)
        return Assessment(risk, reasons)

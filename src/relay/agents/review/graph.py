"""Code Review Agent: a deterministic pipeline with LLM steps (not a free-form agent loop).

    load_pr ─▶ stage_files ─▶ analyzers ─▶ compress ─▶ llm_review ─▶ verify ─▶ assemble ─▶ gate ─▶ post
    (taint if fork/outsider)   (sandbox, no creds)                    (evidence)  (dedup,risk) (policy/HITL)

``review_diff`` is the reusable core (no GitHub needed): used by the coding agent's self-review
and by the seeded-bug benchmark.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from relay.agents.review.analyzers import Finding, run_analyzers
from relay.agents.review.diff import compress, parse_diff
from relay.agents.review.reviewer import (
    comment_body,
    dedupe,
    existing_fingerprints,
    llm_review,
    verify,
)
from relay.agents.review.risk import RiskScore, score_pr
from relay.core.events import Event, EventSink, EventType
from relay.core.idempotency import IdempotencyStore, idempotency_key
from relay.llm.client import LLM
from relay.sandbox.base import Sandbox
from relay.security.policy import Decision, PolicyEngine
from relay.security.taint import TaintState


@dataclass(slots=True)
class ReviewReport:
    findings: list[Finding] = field(default_factory=list)
    risk: RiskScore | None = None
    dropped: dict[str, int] = field(default_factory=dict)
    cost_usd: float = 0.0
    diff_mode: str = ""
    files_reviewed: int = 0


async def review_diff(llm: LLM, diff_text: str, *, sources: dict[str, str] | None = None,
                      sandbox: Sandbox | None = None, title: str = "", body: str = "",
                      min_confidence: float = 0.6, budget_tokens: int = 24_000,
                      tenant_id: str | None = None) -> ReviewReport:
    sources = sources or {}
    patches = parse_diff(diff_text)
    by_path = {p.path: p for p in patches}
    changed = {p.path: p.changed_lines() for p in patches}
    analyzer_findings: list[Finding] = []
    if sandbox is not None:
        analyzer_findings = await run_analyzers(sandbox, [p.path for p in patches if p.status != "deleted"], changed)
    cd = compress(patches, budget_tokens=budget_tokens, get_source=sources.get)
    raw, cost, _ = await llm_review(llm, cd, analyzer_findings, title, body, tenant_id=tenant_id)
    ver = await verify(llm, raw, by_path, sources, min_confidence=min_confidence, tenant_id=tenant_id)
    findings = dedupe([*analyzer_findings, *ver.findings])
    return ReviewReport(findings=findings, risk=score_pr(patches, analyzer_findings), dropped=ver.dropped,
                        cost_usd=cost + ver.cost_usd, diff_mode=cd.mode, files_reviewed=len(cd.included))


class ReviewState(TypedDict, total=False):
    repo: str
    number: int
    installation_id: int
    head_sha: str
    title: str
    body: str
    diff: str
    is_fork: bool
    tainted: bool
    sources: dict[str, str]
    findings: list[dict[str, Any]]
    risk: dict[str, Any]
    summary: str
    approved: bool
    posted: dict[str, Any]
    status: str
    result: str


def build_review_graph(*, llm: LLM, github: Any, sandbox: Sandbox | None, engine: PolicyEngine, events: EventSink,
                       idem: IdempotencyStore, run_id: uuid.UUID, tenant_id: str, dry_run: bool = False) -> StateGraph:
    taint = TaintState()

    async def load_pr(s: ReviewState) -> dict[str, Any]:
        pr = await github.get_pr(s["installation_id"], s["repo"], s["number"])
        diff = await github.get_pr_diff(s["installation_id"], s["repo"], s["number"])
        is_fork = pr["head"]["repo"]["full_name"] != pr["base"]["repo"]["full_name"]
        outsider = pr.get("author_association") not in ("OWNER", "MEMBER", "COLLABORATOR")
        if is_fork or outsider:
            taint.mark("pr:external_author")
            await events.append(run_id, Event(EventType.TAINTED, {"source": "pr", "fork": is_fork}))
        # taint lives in graph state (checkpointed) so a resumed run re-evaluates policy identically
        return {"head_sha": pr["head"]["sha"], "title": pr.get("title", ""), "body": pr.get("body") or "",
                "diff": diff, "is_fork": is_fork, "tainted": taint.tainted}

    async def stage_files(s: ReviewState) -> dict[str, Any]:
        """Write changed files at head into the sandbox via the API: the sandbox never gets git creds."""
        sources: dict[str, str] = {}
        for p in parse_diff(s["diff"]):
            if p.status == "deleted" or p.binary:
                continue
            try:
                text = await github.get_file(s["installation_id"], s["repo"], p.path, s["head_sha"])
            except Exception:
                continue
            if len(text) < 300_000:
                sources[p.path] = text
                if sandbox is not None:
                    await sandbox.write_file(p.path, text)
        return {"sources": sources}

    async def review(s: ReviewState) -> dict[str, Any]:
        await events.append(run_id, Event(EventType.PHASE, {"phase": "review"}))
        rep = await review_diff(llm, s["diff"], sources=s.get("sources"), sandbox=sandbox, title=s.get("title", ""),
                                body=s.get("body", ""), tenant_id=tenant_id)
        existing = existing_fingerprints([c.get("body", "") for c in await github.list_review_comments(
            s["installation_id"], s["repo"], s["number"])]) if not dry_run else set()
        findings = [f for f in rep.findings if f.fingerprint not in existing]
        risk = rep.risk or RiskScore(0, "low")
        summary = (f"### Relay review · risk **{risk.level}** ({risk.score}/100)\n" +
                   "".join(f"- {r}\n" for r in risk.reasons) +
                   f"\n{len(findings)} finding(s) after verification (dropped: "
                   f"{', '.join(f'{k}={v}' for k, v in rep.dropped.items() if v) or 'none'}).")
        await events.append(run_id, Event(EventType.MESSAGE, {"findings": len(findings), "risk": risk.score},
                                          cost_usd=rep.cost_usd))
        return {"findings": [_finding_dict(f) for f in findings],
                "risk": {"score": risk.score, "level": risk.level, "reasons": risk.reasons}, "summary": summary}

    async def gate(s: ReviewState) -> dict[str, Any]:
        args = {"repo": s["repo"], "number": s["number"], "body": s["summary"],
                "comments": [c["message"] for c in s.get("findings", [])]}
        tainted = bool(s.get("tainted"))
        d = await engine.evaluate("post_review", args, tainted=tainted)
        await events.append(run_id, Event(EventType.DECISION, {"tool": "post_review", **d.to_dict()}))
        if d.decision == Decision.DENY:
            return {"approved": False, "status": "done", "result": "posting denied by policy"}
        if d.decision == Decision.APPROVE and not dry_run:
            reply = interrupt({"kind": "tool_calls", "calls": [{"call_id": "post_review", "tool": "post_review",
                                                                "args": {"repo": s["repo"], "number": s["number"],
                                                                         "findings": len(s.get("findings", []))},
                                                                **d.to_dict()}],
                               "tainted": tainted, "taint_sources": ["pr:external_author"] if tainted else []})
            return {"approved": "post_review" in (reply or {}).get("approved", [])}
        return {"approved": True}

    async def post(s: ReviewState) -> dict[str, Any]:
        if dry_run or not s.get("approved"):
            return {"status": "done", "result": "dry run" if dry_run else "not approved"}
        comments = [{"path": f["path"], "line": f["line"], "side": "RIGHT", "body": comment_body(_finding(f))}
                    for f in s.get("findings", [])]
        args = {"repo": s["repo"], "number": s["number"], "sha": s["head_sha"], "n": len(comments)}
        key = idempotency_key(run_id, "post_review", args)
        if (cached := await idem.get(key)) is not None:
            return {"posted": cached, "status": "done", "result": "already posted"}
        r = await github.create_review(s["installation_id"], s["repo"], s["number"], s["head_sha"], s["summary"],
                                       comments)
        posted = {"review_id": r.get("id"), "comments": len(comments)}
        await idem.put(key, posted)
        await events.append(run_id, Event(EventType.OBSERVATION, {"tool": "post_review", **posted}))
        return {"posted": posted, "status": "done", "result": f"posted {len(comments)} comments"}

    g = StateGraph(ReviewState)
    for name, fn in (("load_pr", load_pr), ("stage_files", stage_files), ("review", review), ("gate", gate),
                     ("post", post)):
        g.add_node(name, fn)
    g.add_edge(START, "load_pr")
    g.add_edge("load_pr", "stage_files")
    g.add_edge("stage_files", "review")
    g.add_edge("review", "gate")
    g.add_edge("gate", "post")
    g.add_edge("post", END)
    return g


def _finding_dict(f: Finding) -> dict[str, Any]:
    return {"source": f.source, "path": f.path, "line": f.line, "rule": f.rule, "severity": f.severity,
            "message": f.message, "confidence": f.confidence, "suggestion": f.suggestion, "category": f.category,
            "fingerprint": f.fingerprint}


def _finding(d: dict[str, Any]) -> Finding:
    return Finding(d["source"], d["path"], d["line"], d["rule"], d["severity"], d["message"], d["confidence"],
                   d.get("suggestion"), d.get("category", "bug"))

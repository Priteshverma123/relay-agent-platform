"""Aider-style repository map: the most relevant definitions in a repo, within a token budget.

1. Extract definitions and references per file: tree-sitter when ``tree_sitter_language_pack``
   is installed, otherwise a regex fallback for Python/JS/TS/Go/Java/Rust.
2. Build a file graph: edge A→B (weight = #refs) when file A references a symbol defined in B.
3. Personalized PageRank, biased towards files/identifiers mentioned in the task, ranks files;
   definitions are emitted in rank order until the token budget is spent.
The map gives the model a "table of contents" of the codebase for ~1-2k tokens instead of
reading files blindly.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from relay.common.text import count_tokens

_DEF_PATTERNS = {
    "py": [re.compile(r"^\s*(class|def|async def)\s+([A-Za-z_]\w*)\s*[(:]", re.M)],
    "js": [re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(function|class)\s+([A-Za-z_$][\w$]*)", re.M),
           re.compile(r"^\s*(?:export\s+)?(const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(", re.M)],
    "go": [re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)()", re.M),
           re.compile(r"^type\s+([A-Za-z_]\w*)\s+(struct|interface)", re.M)],
    "java": [re.compile(r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(class|interface|enum)\s+([A-Za-z_]\w*)",
                        re.M)],
    "rs": [re.compile(r"^\s*(?:pub\s+)?(fn|struct|enum|trait)\s+([A-Za-z_]\w*)", re.M)],
}
_EXT = {"py": "py", "js": "js", "jsx": "js", "ts": "js", "tsx": "js", "go": "go", "java": "java", "rs": "rs"}
_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_COMMON = {"self", "this", "None", "True", "False", "return", "import", "from", "class", "def", "const", "function",
           "string", "int", "str", "list", "dict", "len", "print", "range", "async", "await", "null", "true", "false"}


@dataclass(slots=True)
class FileSymbols:
    path: str
    defs: list[tuple[int, str, str]] = field(default_factory=list)  # (line, kind, name)
    refs: set[str] = field(default_factory=set)


def extract(path: str, text: str) -> FileSymbols:
    ext = _EXT.get(path.rsplit(".", 1)[-1].lower())
    fs = FileSymbols(path)
    if ext is None:
        return fs
    ts = _treesitter_defs(ext, text)
    if ts is not None:
        fs.defs = ts
    else:
        for pat in _DEF_PATTERNS[ext]:
            for m in pat.finditer(text):
                g = m.groups()
                kind, name = (g[0], g[1]) if ext != "go" or not g[1] else (g[1], g[0])
                if ext == "go" and not g[1]:
                    kind, name = "func", g[0]
                fs.defs.append((text[: m.start()].count("\n") + 1, kind, name))
    defined = {d[2] for d in fs.defs}
    fs.refs = {t for t in _IDENT.findall(text) if t not in _COMMON and t not in defined}
    return fs


def _treesitter_defs(ext: str, text: str) -> list[tuple[int, str, str]] | None:
    try:
        from tree_sitter_language_pack import get_parser  # optional dependency
    except ImportError:
        return None
    lang = {"py": "python", "js": "typescript", "go": "go", "java": "java", "rs": "rust"}[ext]
    try:
        tree = get_parser(lang).parse(text.encode())
    except Exception:
        return None
    kinds = {"function_definition": "def", "class_definition": "class", "function_declaration": "function",
             "class_declaration": "class", "method_definition": "method", "method_declaration": "method",
             "type_spec": "type", "function_item": "fn", "struct_item": "struct", "interface_declaration": "interface"}
    out: list[tuple[int, str, str]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in kinds:
            name = node.child_by_field_name("name")
            if name is not None:
                out.append((node.start_point[0] + 1, kinds[node.type], name.text.decode()))
        stack.extend(node.children)
    return sorted(out)


def pagerank(nodes: list[str], edges: dict[str, dict[str, float]], personalization: dict[str, float],
             damping: float = 0.85, iters: int = 40) -> dict[str, float]:
    n = len(nodes)
    if n == 0:
        return {}
    pers_total = sum(personalization.values())
    p = {v: personalization.get(v, 0.0) / pers_total for v in nodes} if pers_total else {v: 1 / n for v in nodes}
    rank = dict(p) if pers_total else {v: 1 / n for v in nodes}
    out_w = {v: sum(edges.get(v, {}).values()) for v in nodes}
    for _ in range(iters):
        new = {v: (1 - damping) * p[v] for v in nodes}
        dangling = sum(rank[v] for v in nodes if out_w[v] == 0)
        for v in nodes:
            new[v] += damping * dangling * p[v]
            for u, w in edges.get(v, {}).items():
                new[u] += damping * rank[v] * w / out_w[v]
        rank = new
    return rank


def build_map(files: dict[str, str], *, mentioned: str = "", max_tokens: int = 1500) -> str:
    syms = {p: extract(p, t) for p, t in files.items()}
    definers: dict[str, set[str]] = defaultdict(set)
    for p, fs in syms.items():
        for _, _, name in fs.defs:
            definers[name].add(p)
    edges: dict[str, dict[str, float]] = defaultdict(dict)
    for p, fs in syms.items():
        for ref in fs.refs:
            for target in definers.get(ref, ()):
                if target != p:
                    edges[p][target] = edges[p].get(target, 0.0) + 1.0
    mention_terms = set(_IDENT.findall(mentioned))
    pers: dict[str, float] = {}
    for p, fs in syms.items():
        score = 0.0
        if any(seg and seg in mentioned for seg in re.split(r"[/\\]", p)):
            score += 5.0
        score += sum(2.0 for _, _, n in fs.defs if n in mention_terms)
        if score:
            pers[p] = score
    ranks = pagerank(sorted(syms), edges, pers)
    lines: list[str] = []
    budget = max_tokens
    for p in sorted(ranks, key=lambda x: -ranks[x]):
        fs = syms[p]
        if not fs.defs:
            continue
        block = [f"{p}:"] + [f"  {ln:>4} {kind} {name}" for ln, kind, name in fs.defs[:25]]
        cost = count_tokens("\n".join(block))
        if cost > budget:
            if budget > 50:
                lines.append(f"{p}: ({len(fs.defs)} definitions)")
            break
        lines.extend(block)
        budget -= cost
    return "\n".join(lines)


async def repo_map_from_sandbox(sandbox: object, mentioned: str, max_tokens: int = 1500, max_files: int = 400) -> str:
    """Collect source files from the sandbox (git-tracked, size-capped) and build the map."""
    r = await sandbox.exec("git ls-files | grep -E '\\.(py|js|jsx|ts|tsx|go|java|rs)$' | head -" + str(max_files),
                           timeout_s=60)  # type: ignore[attr-defined]
    files: dict[str, str] = {}
    for path in r.output.split():
        try:
            text = await sandbox.read_file(path)  # type: ignore[attr-defined]
        except Exception:
            continue
        if len(text) < 200_000:
            files[path] = text
    return build_map(files, mentioned=mentioned, max_tokens=max_tokens)

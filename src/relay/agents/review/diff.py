"""Unified-diff parsing and token-budgeted compression (modelled on Qodo PR-Agent's strategy).

* drop binaries, lockfiles, vendored and generated files;
* rank files by the PR's main languages first, then by size of change;
* SMALL PR (fits budget): extend each hunk's context to the enclosing function/class when the
  full file is available (reviewers need the surrounding code, not just ±3 lines);
* LARGE PR: drop deletion-only hunks, add patches largest-first until a soft budget, list the
  rest as "other modified files", deleted files last, never exceed the hard budget.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from relay.common.text import count_tokens

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
SKIP = re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|Cargo\.lock|go\.sum|"
                  r".*\.min\.(js|css)|.*\.(png|jpe?g|gif|ico|pdf|zip|gz|woff2?|ttf|so|dylib|dll|exe|bin)|"
                  r"vendor/.*|node_modules/.*|dist/.*|.*_pb2\.py|.*\.generated\.\w+)$")
LANG = {"py": "python", "js": "javascript", "jsx": "javascript", "ts": "typescript", "tsx": "typescript",
        "go": "go", "java": "java", "rb": "ruby", "rs": "rust", "cs": "csharp", "php": "php", "kt": "kotlin",
        "sql": "sql", "yml": "yaml", "yaml": "yaml", "tf": "terraform", "sh": "shell"}
_ENCLOSING = re.compile(r"^\s*(def |async def |class |function |func |public |private |protected |fn )")


@dataclass(slots=True)
class Hunk:
    old_start: int
    new_start: int
    header: str
    lines: list[str] = field(default_factory=list)

    @property
    def added(self) -> int:
        return sum(1 for ln in self.lines if ln.startswith("+"))

    @property
    def removed(self) -> int:
        return sum(1 for ln in self.lines if ln.startswith("-"))

    def new_line_numbers(self) -> list[int]:
        """Line numbers (in the new file) of added lines: valid targets for inline comments."""
        out, n = [], self.new_start
        for ln in self.lines:
            if ln.startswith("+"):
                out.append(n)
                n += 1
            elif ln.startswith("-"):
                continue
            else:
                n += 1
        return out

    def render(self) -> str:
        return "\n".join([f"@@ -{self.old_start} +{self.new_start} @@{self.header}", *self.lines])


@dataclass(slots=True)
class FilePatch:
    path: str
    status: str = "modified"  # added | modified | deleted | renamed
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def language(self) -> str:
        return LANG.get(self.path.rsplit(".", 1)[-1].lower(), "other")

    @property
    def size(self) -> int:
        return sum(h.added + h.removed for h in self.hunks)

    def changed_lines(self) -> set[int]:
        return {n for h in self.hunks for n in h.new_line_numbers()}

    def render(self, hunks: list[Hunk] | None = None) -> str:
        return f"## File: {self.path} ({self.status})\n" + "\n".join(h.render() for h in (hunks or self.hunks))


def parse_diff(diff: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    cur: FilePatch | None = None
    hunk: Hunk | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            cur = FilePatch(path=m.group(2) if m else line.split()[-1])
            files.append(cur)
            hunk = None
        elif cur is None:
            continue
        elif line.startswith("new file mode"):
            cur.status = "added"
        elif line.startswith("deleted file mode"):
            cur.status = "deleted"
        elif line.startswith("rename to "):
            cur.status = "renamed"
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
            cur.binary = True
        elif line.startswith(("--- ", "+++ ", "index ", "similarity index", "rename from")):
            continue
        elif m := _HUNK.match(line):
            hunk = Hunk(int(m.group(1)), int(m.group(3)), m.group(5))
            cur.hunks.append(hunk)
        elif hunk is not None and (line[:1] in ("+", "-", " ") or line == ""):
            hunk.lines.append(line if line else " ")
    return files


def _extend_to_enclosing(h: Hunk, source: str, max_extra: int = 40) -> Hunk:
    lines = source.splitlines()
    first = h.new_start - 1  # 0-based index of first line in hunk (new file)
    start = first
    while start > 0 and first - start < max_extra and not _ENCLOSING.match(lines[start]):
        start -= 1
    if start == first or not _ENCLOSING.match(lines[start] if start < len(lines) else ""):
        return h
    extra = [" " + ln for ln in lines[start:first]]
    return Hunk(h.old_start - len(extra), start + 1, h.header, extra + h.lines)


@dataclass(slots=True)
class CompressedDiff:
    text: str
    included: list[str]
    other_files: list[str]
    deleted_files: list[str]
    skipped: list[str]
    tokens: int
    mode: str  # "full+context" | "compressed"


def compress(files: list[FilePatch], *, budget_tokens: int = 24_000, soft_ratio: float = 0.85,
             get_source: Callable[[str], str | None] | None = None) -> CompressedDiff:
    skipped = [f.path for f in files if f.binary or SKIP.search(f.path)]
    live = [f for f in files if f.path not in skipped]
    deleted = [f.path for f in live if f.status == "deleted"]
    live = [f for f in live if f.status != "deleted"]
    langs = Counter(f.language for f in live for _ in range(max(1, f.size)))
    rank = {lang: i for i, (lang, _) in enumerate(langs.most_common())}
    live.sort(key=lambda f: (rank.get(f.language, 99), -f.size))

    full = "\n\n".join(f.render() for f in live)
    if count_tokens(full) <= budget_tokens * soft_ratio:
        parts = []
        for f in live:
            src = get_source(f.path) if get_source else None
            hunks = [_extend_to_enclosing(h, src) for h in f.hunks] if src else f.hunks
            parts.append(f.render(hunks))
        text = "\n\n".join(parts)
        if count_tokens(text) > budget_tokens:  # context extension overflowed: fall back to plain
            text = full
        return CompressedDiff(text, [f.path for f in live], [], deleted, skipped, count_tokens(text), "full+context")

    included, others, parts, spent = [], [], [], 0
    soft = budget_tokens * soft_ratio
    for f in live:
        hunks = [h for h in f.hunks if h.added > 0]  # deletion-only hunks dropped in compressed mode
        if not hunks:
            others.append(f.path)
            continue
        block = f.render(hunks)
        t = count_tokens(block)
        if spent + t <= soft:
            parts.append(block)
            included.append(f.path)
            spent += t
        else:
            others.append(f.path)
    tail = []
    if others:
        tail.append("## Other modified files (not shown, budget):\n" + "\n".join(f"- {p}" for p in others))
    if deleted:
        tail.append("## Deleted files:\n" + "\n".join(f"- {p}" for p in deleted))
    text = "\n\n".join(parts + tail)
    while count_tokens(text) > budget_tokens and parts:  # hard cap
        parts.pop()
        included.pop()
        text = "\n\n".join(parts + tail)
    return CompressedDiff(text, included, others, deleted, skipped, count_tokens(text), "compressed")

"""Agent-computer interface (ACI) tools for coding work inside the sandbox.

Design lessons from SWE-agent's ACI study:
  * a windowed viewer (100 lines + position) beats `cat` of whole files: less context, fewer errors;
  * search returns *file names with counts*, not walls of matches;
  * edits are lint-gated: a syntactically broken edit is rolled back and the error returned;
  * empty output is stated explicitly ("ran successfully and produced no output") so the model
    doesn't retry a command it thinks failed;
  * long outputs are head+tail truncated (errors are at the end).
"""

from __future__ import annotations

from typing import Any

from relay.common.text import head_tail
from relay.sandbox.base import q
from relay.tools.base import Tool, ToolContext, ToolResult, params

WINDOW = 100
LINTERS = {  # extension → command that exits non-zero on syntax errors (only checks the edited file)
    ".py": "python -m py_compile {f}",
    ".json": "python -c \"import json,sys; json.load(open(sys.argv[1]))\" {f}",
    ".yaml": "python -c \"import yaml,sys; yaml.safe_load(open(sys.argv[1]))\" {f}",
    ".yml": "python -c \"import yaml,sys; yaml.safe_load(open(sys.argv[1]))\" {f}",
    ".js": "node --check {f}",
}


def _numbered(text: str, start: int, path: str) -> str:
    lines = text.splitlines()
    total = len(lines)
    start = max(1, min(start, max(1, total)))
    end = min(total, start + WINDOW - 1)
    body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1))
    more = f"\n(lines {start}-{end} of {total}; call view_file with start_line={end + 1} for more)" \
        if end < total else f"\n(lines {start}-{end} of {total}; end of file)"
    return f"[{path}]\n{body}{more}"


async def view_file(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        text = await ctx.sandbox.read_file(a["path"])
    except FileNotFoundError:
        return ToolResult(f"File not found: {a['path']}", is_error=True)
    return ToolResult(_numbered(text, int(a.get("start_line") or 1), a["path"]))


async def search_files(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path = a.get("path") or "."
    r = await ctx.sandbox.exec(f"rg --count-matches --no-messages -e {q(a['pattern'])} {q(path)} | sort -t: -k2 -nr "
                               f"| head -50", timeout_s=60)
    if not r.output.strip():
        return ToolResult(f"No files contain '{a['pattern']}'.")
    n = len(r.output.strip().splitlines())
    return ToolResult(f"{n} file(s) match (path:count):\n{r.output.strip()}"
                      + ("\n(showing top 50; narrow the pattern or path)" if n >= 50 else ""))


async def search_in_file(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    r = await ctx.sandbox.exec(f"rg -n --no-messages -e {q(a['pattern'])} {q(a['path'])} | head -60", timeout_s=30)
    return ToolResult(r.output.strip() or f"No matches for '{a['pattern']}' in {a['path']}.")


async def edit_file(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    path, old, new = a["path"], a["old_str"], a["new_str"]
    try:
        text = await ctx.sandbox.read_file(path)
    except FileNotFoundError:
        return ToolResult(f"File not found: {path}. Use create_file for new files.", is_error=True)
    count = text.count(old)
    if count == 0:
        return ToolResult("old_str not found. View the file again and copy the exact text (including indentation).",
                          is_error=True)
    if count > 1:
        return ToolResult(f"old_str matches {count} places; include more surrounding lines to make it unique.",
                          is_error=True)
    updated = text.replace(old, new, 1)
    await ctx.sandbox.write_file(path, updated)
    lint = await _lint(ctx, path)
    if lint:
        await ctx.sandbox.write_file(path, text)  # roll back: never leave the repo syntactically broken
        return ToolResult(f"Edit rejected (rolled back): it introduces a syntax error:\n{lint}", is_error=True)
    line = text[: text.index(old)].count("\n") + 1
    return ToolResult(_numbered(updated, max(1, line - 5), path) if len(new) < 4000 else f"Edited {path}.")


async def create_file(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    await ctx.sandbox.write_file(a["path"], a["content"])
    lint = await _lint(ctx, a["path"])
    if lint:
        return ToolResult(f"File created but has a syntax error:\n{lint}", is_error=True)
    return ToolResult(f"Created {a['path']} ({a['content'].count(chr(10)) + 1} lines).")


async def _lint(ctx: ToolContext, path: str) -> str | None:
    ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
    cmd = LINTERS.get(ext)
    if not cmd:
        return None
    r = await ctx.sandbox.exec(cmd.format(f=q(path)), timeout_s=60)
    return head_tail(r.output, 1500) if r.exit_code != 0 else None


async def run(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    r = await ctx.sandbox.exec(a["command"], timeout_s=int(a.get("timeout_s") or 300))
    out = head_tail(r.output, 8000)
    if r.timed_out:
        return ToolResult(f"{out}\n(command timed out; consider a narrower command)", is_error=True)
    status = f"(exit code {r.exit_code})"
    return ToolResult(f"{out}\n{status}" if out.strip() else ("" if r.exit_code == 0 else status),
                      is_error=r.exit_code != 0)


async def run_tests(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    cmd = ctx.services.get("test_command", "python -m pytest -q -x --no-header -p no:cacheprovider")
    sel = a.get("selector") or ""
    r = await ctx.sandbox.exec(f"{cmd} {q(sel) if sel else ''}", timeout_s=900)
    return ToolResult(head_tail(r.output, 8000) + f"\n(exit code {r.exit_code})", is_error=r.exit_code != 0,
                      data={"passed": r.exit_code == 0})


async def git_diff(a: dict[str, Any], ctx: ToolContext) -> ToolResult:
    r = await ctx.sandbox.exec("git add -A -N . && git diff --stat && git diff", timeout_s=60)
    return ToolResult(head_tail(r.output, 12000))


def coding_tools() -> list[Tool]:
    return [
        Tool("view_file", "Show 100 numbered lines of a file starting at start_line.",
             params({"path": {"type": "string"}, "start_line": {"type": "integer"}}, ["path"]), view_file),
        Tool("search_files", "Find which files contain a regex; returns file paths with match counts.",
             params({"pattern": {"type": "string"}, "path": {"type": "string"}}, ["pattern"]), search_files),
        Tool("search_in_file", "Show matching lines (with numbers) for a regex in one file.",
             params({"pattern": {"type": "string"}, "path": {"type": "string"}}), search_in_file),
        Tool("edit_file", "Replace one exact, unique occurrence of old_str with new_str. Edits that break syntax "
                          "are rolled back.",
             params({"path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}}),
             edit_file),
        Tool("create_file", "Create or overwrite a file with content.",
             params({"path": {"type": "string"}, "content": {"type": "string"}}), create_file),
        Tool("run", "Run a shell command in the repository (sandboxed, no internet except package mirrors).",
             params({"command": {"type": "string"}, "timeout_s": {"type": "integer"}}, ["command"]), run,
             timeout_s=960),
        Tool("run_tests", "Run the test suite or a selector (e.g. tests/test_x.py::test_y).",
             params({"selector": {"type": "string"}}, []), run_tests, timeout_s=960),
        Tool("git_diff", "Show the current uncommitted diff.", params({}, []), git_diff),
    ]

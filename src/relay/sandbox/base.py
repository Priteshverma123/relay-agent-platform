"""Sandbox contract: an isolated workspace where agents read, edit and execute code.

Implementations: ``DockerSandbox`` (production: gVisor runtime optional, no-route network, egress
only through the allowlisting proxy, resource limits, read-only rootfs) and ``LocalSandbox``
(tests and offline evals ONLY: a temp dir + subprocess, no isolation).
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

WORKSPACE = "/workspace"


@dataclass(slots=True)
class ExecResult:
    exit_code: int
    output: str
    timed_out: bool = False


class Sandbox(Protocol):
    id: str
    workdir: str

    async def exec(self, cmd: str, *, timeout_s: int = 300, cwd: str | None = None,
                   env: dict[str, str] | None = None) -> ExecResult: ...

    async def read_file(self, path: str) -> str: ...

    async def write_file(self, path: str, content: str) -> None: ...

    async def pause(self) -> None: ...

    async def destroy(self) -> None: ...


class LocalSandbox:
    """Unsafe, dev/test-only sandbox (no isolation). Refuses to run when RELAY_ENV=prod."""

    def __init__(self, root: str | None = None) -> None:
        if os.environ.get("RELAY_ENV") == "prod":
            raise RuntimeError("LocalSandbox is not allowed in production")
        self.root = Path(root or tempfile.mkdtemp(prefix="relay-sbx-"))
        self.id = self.root.name
        self.workdir = str(self.root)

    def _path(self, path: str) -> Path:
        p = path.removeprefix(WORKSPACE).lstrip("/")
        full = (self.root / p).resolve()
        if not str(full).startswith(str(self.root.resolve())):
            raise PermissionError(f"path escapes workspace: {path}")
        return full

    async def exec(self, cmd: str, *, timeout_s: int = 300, cwd: str | None = None,
                   env: dict[str, str] | None = None) -> ExecResult:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=str(self._path(cwd) if cwd else self.root), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env={**os.environ, **(env or {})})
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            return ExecResult(124, f"command timed out after {timeout_s}s", timed_out=True)
        return ExecResult(proc.returncode or 0, out.decode("utf-8", errors="replace"))

    async def read_file(self, path: str) -> str:
        return self._path(path).read_text(encoding="utf-8", errors="replace")

    async def write_file(self, path: str, content: str) -> None:
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    async def pause(self) -> None:
        return None

    async def destroy(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def q(s: str) -> str:
    return shlex.quote(s)

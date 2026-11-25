"""Docker sandbox provider.

Isolation layers (defence in depth; see docs/adr/0003-sandbox-isolation.md):
  * optional gVisor (``runtime=runsc``): user-space kernel, blocks most container escapes;
  * network: attached ONLY to an ``internal: true`` Docker network (no route out); the egress
    proxy is the single exit and enforces a domain allowlist (gVisor does not filter egress);
  * no secrets: the container gets no tokens; git pushes / PR creation happen from the runtime
    through the GitHub API with short-lived installation tokens;
  * cap_drop=ALL, no-new-privileges, non-root uid, read-only rootfs + tmpfs /tmp, pids/mem/cpu limits;
  * per-run lifecycle: created on first use, paused between PR follow-ups (``reuse_key``), removed
    on completion or by the janitor after TTL.
The docker SDK is blocking, so calls are pushed to a thread.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from relay.common.errors import SandboxError
from relay.common.logging import get_logger
from relay.common.settings import Settings
from relay.sandbox.base import WORKSPACE, ExecResult, q

log = get_logger(__name__)
LABEL = "relay.sandbox"


class DockerSandbox:
    def __init__(self, container: Any, settings: Settings) -> None:
        self.c = container
        self.id = container.name
        self.workdir = WORKSPACE
        self.timeout_default = settings.sandbox_cmd_timeout_s

    async def exec(self, cmd: str, *, timeout_s: int = 300, cwd: str | None = None,
                   env: dict[str, str] | None = None) -> ExecResult:
        # `timeout` inside the container kills runaway commands even if our client disconnects.
        wrapped = f"timeout --kill-after=5 {int(timeout_s)} bash -lc {q(cmd)}"

        def run() -> tuple[int, bytes]:
            r = self.c.exec_run(["bash", "-lc", wrapped], workdir=cwd or WORKSPACE, environment=env or {},
                                demux=False, user="1000:1000")
            return r.exit_code, r.output or b""

        try:
            code, out = await asyncio.wait_for(asyncio.to_thread(run), timeout=timeout_s + 15)
        except TimeoutError:
            return ExecResult(124, f"command timed out after {timeout_s}s", timed_out=True)
        return ExecResult(code, out.decode("utf-8", errors="replace"), timed_out=code == 124)

    async def read_file(self, path: str) -> str:
        r = await self.exec(f"cat -- {q(path)}", timeout_s=30)
        if r.exit_code != 0:
            raise FileNotFoundError(path)
        return r.output

    async def write_file(self, path: str, content: str) -> None:
        b64 = base64.b64encode(content.encode()).decode()
        # chunk to stay under ARG_MAX for large files
        await self.exec(f": > {q(path)}", timeout_s=30)
        for i in range(0, len(b64), 60_000):
            r = await self.exec(f"mkdir -p \"$(dirname {q(path)})\" && printf %s {q(b64[i:i + 60_000])} "
                                f"| base64 -d >> {q(path)}", timeout_s=60)
            if r.exit_code != 0:
                raise SandboxError(f"write failed: {r.output[:300]}")

    async def pause(self) -> None:
        await asyncio.to_thread(self.c.pause)

    async def unpause(self) -> None:
        await asyncio.to_thread(self.c.unpause)

    async def destroy(self) -> None:
        def rm() -> None:
            try:
                self.c.remove(force=True, v=True)
            except Exception:  # already gone
                log.warning("sandbox_remove_failed", id=self.id)
        await asyncio.to_thread(rm)


class DockerSandboxProvider:
    def __init__(self, settings: Settings) -> None:
        import docker

        self.s = settings
        self.client = docker.from_env()

    async def acquire(self, run_id: str, *, reuse_key: str | None = None, image: str | None = None) -> DockerSandbox:
        name = f"relay-sbx-{(reuse_key or run_id)[:40]}".replace("/", "-").replace(":", "-")

        def get_or_create() -> Any:
            import docker.errors

            try:
                c = self.client.containers.get(name)
                if c.status == "paused":
                    c.unpause()
                elif c.status != "running":
                    c.start()
                return c
            except docker.errors.NotFound:
                pass
            kwargs: dict[str, Any] = dict(
                image=image or self.s.sandbox_image, name=name, command=["sleep", "infinity"], detach=True,
                labels={LABEL: "1", "relay.run": run_id}, network=self.s.sandbox_network, user="1000:1000",
                working_dir=WORKSPACE, read_only=True, tmpfs={"/tmp": "rw,size=512m", "/home/agent": "rw,size=256m"},
                volumes={f"{name}-ws": {"bind": WORKSPACE, "mode": "rw"}},
                environment={"HTTP_PROXY": self.s.egress_proxy, "HTTPS_PROXY": self.s.egress_proxy,
                             "NO_PROXY": "", "HOME": "/home/agent", "PIP_NO_INPUT": "1", "CI": "1"},
                mem_limit=self.s.sandbox_mem, nano_cpus=int(self.s.sandbox_cpus * 1e9), pids_limit=self.s.sandbox_pids,
                cap_drop=["ALL"], security_opt=["no-new-privileges:true"], init=True)
            if self.s.sandbox_runtime:
                kwargs["runtime"] = self.s.sandbox_runtime
            return self.client.containers.run(**kwargs)

        try:
            c = await asyncio.to_thread(get_or_create)
        except Exception as exc:
            raise SandboxError(f"cannot start sandbox: {exc}") from exc
        return DockerSandbox(c, self.s)

    async def janitor(self, max_age_s: int = 6 * 3600) -> int:
        """Remove sandboxes older than max_age_s (crash leftovers)."""
        import datetime as dt

        def sweep() -> int:
            n = 0
            now = dt.datetime.now(dt.UTC)
            for c in self.client.containers.list(all=True, filters={"label": LABEL}):
                created = dt.datetime.fromisoformat(c.attrs["Created"][:26].rstrip("Z") + "+00:00")
                if (now - created).total_seconds() > max_age_s:
                    c.remove(force=True, v=True)
                    n += 1
            return n
        return await asyncio.to_thread(sweep)

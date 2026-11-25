"""GitHub App client (REST v3).

* App JWT (RS256, 9-minute expiry) → per-installation access tokens (cached until 5 min before
  expiry). Tokens live only in the runtime process, never in the sandbox or in prompts.
* Pushing code without git credentials in the sandbox: changed files are read out of the sandbox
  and committed through the Git Data API (blobs → tree → commit → ref).
* The app is installed with minimal permissions (contents:write on branches, pull_requests:write,
  issues:read, checks:read); it has no admin or merge capability by design.
"""

from __future__ import annotations

import base64
import time
from datetime import datetime
from typing import Any

import httpx
import jwt

from relay.common.errors import UpstreamError

API = "https://api.github.com"


class GitHubApp:
    def __init__(self, app_id: str, private_key: str, *, api: str = API) -> None:
        self.app_id, self.private_key, self.api = app_id, private_key, api.rstrip("/")
        self.http = httpx.AsyncClient(timeout=30, headers={"Accept": "application/vnd.github+json",
                                                           "X-GitHub-Api-Version": "2022-11-28"})
        self._tokens: dict[int, tuple[str, float]] = {}

    def app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.private_key, algorithm="RS256")

    async def installation_token(self, installation_id: int) -> str:
        tok = self._tokens.get(installation_id)
        if tok and tok[1] - time.time() > 300:
            return tok[0]
        r = await self.http.post(f"{self.api}/app/installations/{installation_id}/access_tokens",
                                 headers={"Authorization": f"Bearer {self.app_jwt()}"})
        if r.status_code >= 400:
            raise UpstreamError(f"github token: {r.status_code} {r.text[:200]}")
        d = r.json()
        exp = datetime.fromisoformat(d["expires_at"].replace("Z", "+00:00")).timestamp()
        self._tokens[installation_id] = (d["token"], exp)
        return d["token"]  # type: ignore[no-any-return]

    async def scoped_read_token(self, installation_id: int, repo_name: str) -> str:
        """Short-lived token restricted to ONE repository with contents:read only. The only
        credential that ever enters a sandbox (for `git clone`, via env, never argv/disk)."""
        r = await self.http.post(f"{self.api}/app/installations/{installation_id}/access_tokens",
                                 headers={"Authorization": f"Bearer {self.app_jwt()}"},
                                 json={"repositories": [repo_name], "permissions": {"contents": "read"}})
        if r.status_code >= 400:
            raise UpstreamError(f"github scoped token: {r.status_code}")
        return r.json()["token"]  # type: ignore[no-any-return]

    async def _req(self, inst: int, method: str, path: str, **kw: Any) -> Any:
        token = await self.installation_token(inst)
        r = await self.http.request(method, f"{self.api}{path}", headers={"Authorization": f"token {token}",
                                                                           **kw.pop("headers", {})}, **kw)
        if r.status_code >= 400:
            raise UpstreamError(f"github {method} {path}: {r.status_code} {r.text[:300]}")
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.text

    # ------------------------------------------------------------------ reads (untrusted content!)
    async def get_pr(self, inst: int, repo: str, number: int) -> dict[str, Any]:
        return await self._req(inst, "GET", f"/repos/{repo}/pulls/{number}")  # type: ignore[no-any-return]

    async def get_pr_diff(self, inst: int, repo: str, number: int) -> str:
        return await self._req(inst, "GET", f"/repos/{repo}/pulls/{number}",  # type: ignore[no-any-return]
                               headers={"Accept": "application/vnd.github.v3.diff"})

    async def list_review_comments(self, inst: int, repo: str, number: int) -> list[dict[str, Any]]:
        return await self._req(inst, "GET", f"/repos/{repo}/pulls/{number}/comments?per_page=100")  # type: ignore[no-any-return]

    async def get_issue(self, inst: int, repo: str, number: int) -> dict[str, Any]:
        return await self._req(inst, "GET", f"/repos/{repo}/issues/{number}")  # type: ignore[no-any-return]

    async def get_file(self, inst: int, repo: str, path: str, ref: str) -> str:
        d = await self._req(inst, "GET", f"/repos/{repo}/contents/{path}?ref={ref}")
        return base64.b64decode(d["content"]).decode("utf-8", errors="replace")

    # ------------------------------------------------------------------ writes (policy-gated)
    async def create_review(self, inst: int, repo: str, number: int, commit_sha: str, body: str,
                            comments: list[dict[str, Any]], event: str = "COMMENT") -> dict[str, Any]:
        """Inline comments: [{path, line, side: RIGHT, body}]. Never APPROVE/REQUEST_CHANGES."""
        return await self._req(inst, "POST", f"/repos/{repo}/pulls/{number}/reviews", json={  # type: ignore[no-any-return]
            "commit_id": commit_sha, "body": body, "event": event, "comments": comments})

    async def comment_issue(self, inst: int, repo: str, number: int, body: str) -> dict[str, Any]:
        return await self._req(inst, "POST", f"/repos/{repo}/issues/{number}/comments",  # type: ignore[no-any-return]
                               json={"body": body})

    async def commit_files(self, inst: int, repo: str, branch: str, base_sha: str, files: dict[str, str | None],
                           message: str) -> str:
        """Create/advance ``branch`` with a commit containing ``files`` (None = delete). Returns sha."""
        tree = []
        for path, content in files.items():
            if content is None:
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            else:
                blob = await self._req(inst, "POST", f"/repos/{repo}/git/blobs",
                                       json={"content": content, "encoding": "utf-8"})
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        base_commit = await self._req(inst, "GET", f"/repos/{repo}/git/commits/{base_sha}")
        new_tree = await self._req(inst, "POST", f"/repos/{repo}/git/trees",
                                   json={"base_tree": base_commit["tree"]["sha"], "tree": tree})
        commit = await self._req(inst, "POST", f"/repos/{repo}/git/commits",
                                 json={"message": message, "tree": new_tree["sha"], "parents": [base_sha]})
        try:
            await self._req(inst, "PATCH", f"/repos/{repo}/git/refs/heads/{branch}",
                            json={"sha": commit["sha"], "force": False})
        except UpstreamError:
            await self._req(inst, "POST", f"/repos/{repo}/git/refs", json={"ref": f"refs/heads/{branch}",
                                                                           "sha": commit["sha"]})
        return commit["sha"]  # type: ignore[no-any-return]

    async def open_draft_pr(self, inst: int, repo: str, head: str, base: str, title: str, body: str) -> dict[str, Any]:
        return await self._req(inst, "POST", f"/repos/{repo}/pulls", json={  # type: ignore[no-any-return]
            "title": title, "head": head, "base": base, "body": body, "draft": True})

    async def aclose(self) -> None:
        await self.http.aclose()

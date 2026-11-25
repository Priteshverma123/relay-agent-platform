"""Slack: post messages and interactive approval requests (Block Kit buttons).

Button clicks arrive at ``POST /webhooks/slack/interactions`` (signature-verified) and are
turned into approval decisions; the clicking user's Slack id is recorded in the audit log.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from relay.common.errors import UpstreamError


class Slack:
    def __init__(self, token: str) -> None:
        self.http = httpx.AsyncClient(base_url="https://slack.com/api", timeout=20,
                                      headers={"Authorization": f"Bearer {token}"})

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = await self.http.post(f"/{method}", json=payload)
        d = r.json()
        if not d.get("ok"):
            raise UpstreamError(f"slack {method}: {d.get('error')}")
        return d  # type: ignore[no-any-return]

    async def post(self, channel: str, text: str, thread_ts: str | None = None) -> dict[str, Any]:
        return await self._call("chat.postMessage", {"channel": channel, "text": text[:39000],
                                                     **({"thread_ts": thread_ts} if thread_ts else {})})

    async def request_approval(self, channel: str, approval_id: str, run_id: str, agent: str,
                               request: dict[str, Any]) -> dict[str, Any]:
        lines = [f"*{agent}* run `{run_id[:8]}` needs approval ({request.get('kind')})"]
        if request.get("tainted"):
            lines.append(":warning: this run has read untrusted content: " + ", ".join(request.get("taint_sources", [])))
        for c in request.get("calls", [])[:5]:
            args = json.dumps(c.get("args", {}))[:600]
            lines.append(f"• `{c.get('tool')}` risk={c.get('risk')} → {'; '.join(c.get('reasons', []))[:300]}\n"
                         f"```{args}```")
        if request.get("kind") == "stuck":
            lines.append(f"Agent is stuck: {request.get('detail')}. Approve = continue with a hint, Reject = abort.")
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}},
                  {"type": "actions", "elements": [
                      {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Approve"},
                       "action_id": "relay_approve", "value": approval_id},
                      {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "Reject"},
                       "action_id": "relay_reject", "value": approval_id}]}]
        return await self._call("chat.postMessage", {"channel": channel, "text": lines[0], "blocks": blocks})

    async def aclose(self) -> None:
        await self.http.aclose()

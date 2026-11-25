"""Minimal official-API clients: Jira Cloud (REST v3), Notion (2022-06-28), IMAP email, Atlas RAG."""

from __future__ import annotations

import asyncio
import email
import imaplib
from email.header import decode_header
from typing import Any

import httpx

from relay.common.errors import UpstreamError


class Jira:
    def __init__(self, base_url: str, email_addr: str, token: str, project: str) -> None:
        self.project = project
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), auth=(email_addr, token), timeout=20)

    async def create_issue(self, summary: str, description: str, issue_type: str = "Task",
                           labels: list[str] | None = None, assignee_account_id: str | None = None) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "project": {"key": self.project}, "summary": summary[:250], "issuetype": {"name": issue_type},
            "labels": labels or ["relay"],
            "description": {"type": "doc", "version": 1, "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": description[:30000]}]}]}}
        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}
        r = await self.http.post("/rest/api/3/issue", json={"fields": fields})
        if r.status_code >= 400:
            raise UpstreamError(f"jira: {r.status_code} {r.text[:300]}")
        return r.json()  # type: ignore[no-any-return]

    async def transition(self, key: str, to_status: str) -> None:
        r = await self.http.get(f"/rest/api/3/issue/{key}/transitions")
        tid = next((t["id"] for t in r.json().get("transitions", []) if t["name"].lower() == to_status.lower()), None)
        if tid is None:
            raise UpstreamError(f"jira: no transition to {to_status} for {key}")
        await self.http.post(f"/rest/api/3/issue/{key}/transitions", json={"transition": {"id": tid}})

    async def search_users(self, query: str) -> list[dict[str, Any]]:
        r = await self.http.get("/rest/api/3/user/search", params={"query": query})
        return r.json() if r.status_code < 400 else []  # type: ignore[no-any-return]


class Notion:
    def __init__(self, token: str) -> None:
        self.http = httpx.AsyncClient(base_url="https://api.notion.com/v1", timeout=20, headers={
            "Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"})

    async def create_page(self, parent_page_id: str, title: str, markdown_lines: list[str]) -> dict[str, Any]:
        children = [{"object": "block", "type": "paragraph", "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": line[:1900]}}]}} for line in markdown_lines[:90]]
        r = await self.http.post("/pages", json={
            "parent": {"page_id": parent_page_id},
            "properties": {"title": {"title": [{"type": "text", "text": {"content": title[:200]}}]}},
            "children": children})
        if r.status_code >= 400:
            raise UpstreamError(f"notion: {r.status_code} {r.text[:300]}")
        return r.json()  # type: ignore[no-any-return]


class Mailbox:
    """Read-only IMAP access (sending email is classified *dangerous* and not provided by default)."""

    def __init__(self, host: str, user: str, password: str) -> None:
        self.host, self.user, self.password = host, user, password

    async def unseen(self, limit: int = 20) -> list[dict[str, str]]:
        def fetch() -> list[dict[str, str]]:
            out: list[dict[str, str]] = []
            with imaplib.IMAP4_SSL(self.host) as m:
                m.login(self.user, self.password)
                m.select("INBOX", readonly=True)
                _, ids = m.search(None, "UNSEEN")
                for num in ids[0].split()[-limit:]:
                    _, data = m.fetch(num, "(RFC822)")
                    msg = email.message_from_bytes(data[0][1])  # type: ignore[index]
                    subj, enc = decode_header(msg.get("Subject", ""))[0]
                    subject = subj.decode(enc or "utf-8", "replace") if isinstance(subj, bytes) else subj
                    body = ""
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
                            break
                    out.append({"id": msg.get("Message-ID", num.decode()), "from": msg.get("From", ""),
                                "subject": subject, "body": body[:20000]})
            return out
        return await asyncio.to_thread(fetch)


class AtlasRAG:
    """Atlas knowledge base over HTTP: /v1/search (evidence) and /v1/query (cited answers)."""

    def __init__(self, base_url: str, api_key: str, collections: list[str]) -> None:
        self.collections = collections
        self.http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=120,
                                      headers={"Authorization": f"Bearer {api_key}"})

    async def query(self, question: str) -> dict[str, Any]:
        r = await self.http.post("/v1/query", json={"question": question, "collection_ids": self.collections,
                                                    "stream": False, "use_memory": False})
        if r.status_code >= 400:
            raise UpstreamError(f"atlas: {r.status_code} {r.text[:300]}")
        return r.json()  # type: ignore[no-any-return]

    async def search(self, query: str, top_k: int = 8) -> list[dict[str, Any]]:
        r = await self.http.post("/v1/search", json={"query": query, "collection_ids": self.collections,
                                                     "top_k": top_k})
        if r.status_code >= 400:
            raise UpstreamError(f"atlas: {r.status_code} {r.text[:300]}")
        return r.json()["results"]  # type: ignore[no-any-return]

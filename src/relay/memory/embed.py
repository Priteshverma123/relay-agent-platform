"""Embeddings for memory: TEI (/embed) when configured; deterministic hashing-trick fallback so
memory keeps working (degraded similarity quality) when the model server is unavailable."""

from __future__ import annotations

import hashlib
import math
import re

import httpx

_WORD = re.compile(r"[a-z0-9]+")


def hashing_embed(text: str, dim: int = 384) -> list[float]:
    vec = [0.0] * dim
    words = _WORD.findall(text.lower())
    grams = words + [f"{a}_{b}" for a, b in zip(words, words[1:], strict=False)]
    for g in grams:
        h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "little")
        vec[h % dim] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class Embedder:
    def __init__(self, url: str | None, dim: int = 384) -> None:
        self.url, self.dim = (url.rstrip("/") if url else None), dim
        self.http = httpx.AsyncClient(timeout=30)
        self.degraded = url is None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if self.url and not self.degraded:
            try:
                r = await self.http.post(f"{self.url}/embed", json={"inputs": texts, "normalize": True,
                                                                    "truncate": True})
                r.raise_for_status()
                return r.json()  # type: ignore[no-any-return]
            except httpx.HTTPError:
                self.degraded = True  # sticky for this process; surfaced via /health/ready
        return [hashing_embed(t, self.dim) for t in texts]

    async def aclose(self) -> None:
        await self.http.aclose()


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))

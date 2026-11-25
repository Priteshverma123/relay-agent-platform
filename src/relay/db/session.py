"""Async engines + tenant-scoped transactions (RLS via set_config(..., is_local=true))."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from relay.common.settings import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url, pool_size=10, max_overflow=20, pool_pre_ping=True)


@lru_cache
def get_admin_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_admin_url, pool_size=3, pool_pre_ping=True)


@asynccontextmanager
async def tenant_session(tenant_id: uuid.UUID | str) -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(get_engine(), expire_on_commit=False)() as s, s.begin():
        await s.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
        yield s


@asynccontextmanager
async def admin_session() -> AsyncIterator[AsyncSession]:
    async with async_sessionmaker(get_admin_engine(), expire_on_commit=False)() as s, s.begin():
        yield s

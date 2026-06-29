"""Async database engine + session factory (replaces EF Core DbContextFactory)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_autopilot.data.entities import Base
from ai_autopilot.logging_config import get_logger


class Database:
    def __init__(self, url: str) -> None:
        self._engine = create_async_engine(url, future=True)
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        self._log = get_logger("data.database")

    async def create_all(self) -> None:
        """Create tables if they do not exist (parity with EnsureCreatedAsync)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self._log.info("database ready")

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            yield session

    async def dispose(self) -> None:
        await self._engine.dispose()

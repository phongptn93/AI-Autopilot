"""Async database engine + session factory (replaces EF Core DbContextFactory)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ai_autopilot.data.entities import Base
from ai_autopilot.logging_config import get_logger

# Lightweight additive migrations (no Alembic): columns added to existing tables
# after they were first created. ``create_all`` only creates missing TABLES, not
# missing COLUMNS, so each is applied via ``ALTER TABLE ... ADD COLUMN`` if absent.
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("executions", "trigger_tag", "VARCHAR(100)"),
)


class Database:
    def __init__(self, url: str) -> None:
        self._engine = create_async_engine(url, future=True)
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)
        self._log = get_logger("data.database")

    async def create_all(self) -> None:
        """Create tables if they do not exist (parity with EnsureCreatedAsync)."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await self._apply_column_migrations(conn)
        self._log.info("database ready")

    async def _apply_column_migrations(self, conn) -> None:
        """Add columns introduced after a table's initial creation (SQLite)."""
        for table, column, ddl in _COLUMN_MIGRATIONS:
            rows = (await conn.execute(text(f"PRAGMA table_info({table})"))).all()
            existing = {r[1] for r in rows}  # PRAGMA column name is index 1
            if column not in existing:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
                self._log.info("db migration: added column", table=table, column=column)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._sessionmaker() as session:
            yield session

    async def dispose(self) -> None:
        await self._engine.dispose()

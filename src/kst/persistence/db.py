"""Async SQLite engine (WAL) + schema bootstrap."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from kst.persistence.tables import Base


def _sqlite_pragmas(dbapi_conn: Any, _: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


class Database:
    def __init__(self, path: Path | None = None, url: str | None = None) -> None:
        if url is None:
            assert path is not None
            path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+aiosqlite:///{path}"
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url, future=True)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", _sqlite_pragmas)
        self.session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def init(self) -> None:
        """Create tables if missing. Schema changes after v0.1 go through Alembic (see docs/RUNBOOK.md)."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

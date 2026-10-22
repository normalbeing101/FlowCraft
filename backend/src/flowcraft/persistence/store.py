"""Persistence abstraction: async engine/session management + repositories.

Deliberately thin: SQLAlchemy Core/ORM with a session-per-operation pattern.
PostgreSQL in production (DATABASE_URL), SQLite+aiosqlite for local dev/tests.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .models import Base


async def make_store(database_url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(database_url, future=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def init_schema(engine: AsyncEngine) -> None:
    """Create tables if missing (dev convenience). Production uses Alembic."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

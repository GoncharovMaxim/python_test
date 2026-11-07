from __future__ import annotations

import typing as t

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

if t.TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import (
        AsyncEngine,
    )

SessionMaker = async_sessionmaker[AsyncSession]


def make_engine(url: str, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=pool_size,
        pool_pre_ping=True,
    )


def make_sessionmaker(engine: AsyncEngine) -> SessionMaker:
    return async_sessionmaker(engine, expire_on_commit=False)

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def create_engine(
    dsn: str,
    *,
    pool_size: int = 10,
    max_overflow: int = 10,
    pool_timeout: int = 10,
    pool_recycle: int = 1800,
) -> AsyncEngine:
    """Engine with a bounded pool and no infinite waits.

    Without `pool_timeout` a burst of traffic turns into requests waiting forever
    for a connection instead of failing fast; without `pool_recycle` the app keeps
    sockets that a proxy (PgBouncer, cloud LB) already closed, and the first query
    after an idle night fails.
    """
    kwargs: dict[str, object] = {"pool_pre_ping": True, "future": True}
    if dsn.startswith("sqlite"):
        return create_async_engine(dsn, **kwargs)

    kwargs.update(
        {
            "pool_size": max(1, int(pool_size)),
            "max_overflow": max(0, int(max_overflow)),
            "pool_timeout": max(1, int(pool_timeout)),
            "pool_recycle": max(60, int(pool_recycle)),
            "pool_use_lifo": True,
        }
    )
    if "postgresql" in dsn:
        kwargs["connect_args"] = {"command_timeout": 30, "timeout": 10}
    return create_async_engine(dsn, **kwargs)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def session_scope(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session

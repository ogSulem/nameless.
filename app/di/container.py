from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@dataclass(frozen=True, slots=True)
class Container:
    settings: object
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis

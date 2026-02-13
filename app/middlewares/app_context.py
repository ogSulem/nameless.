from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from redis.asyncio import Redis

from app.config import Settings


class AppContextMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings, redis: Redis) -> None:
        self._settings = settings
        self._redis = redis

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        data["settings"] = self._settings
        data["redis"] = self._redis
        return await handler(event, data)

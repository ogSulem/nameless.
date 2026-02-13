from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message
from redis.asyncio import Redis


logger = logging.getLogger(__name__)


class MessageDedupeMiddleware(BaseMiddleware):
    def __init__(self, redis: Redis, ttl_seconds: int = 3) -> None:
        self._redis = redis
        self._ttl = int(ttl_seconds)

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        if event.from_user is None:
            return await handler(event, data)

        try:
            key = f"msg:dedupe:{event.from_user.id}:{event.message_id}"
            ok = await self._redis.set(key, "1", nx=True, ex=self._ttl)
            if not ok:
                return None
        except Exception:
            logger.exception("message_dedupe_failed")

        return await handler(event, data)

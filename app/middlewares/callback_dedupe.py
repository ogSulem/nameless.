from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery
from redis.asyncio import Redis


logger = logging.getLogger(__name__)


class CallbackDedupeMiddleware(BaseMiddleware):
    def __init__(self, redis: Redis, ttl_seconds: int = 3) -> None:
        self._redis = redis
        self._ttl = int(ttl_seconds)

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)

        try:
            # callback.id is unique per update; use it as primary dedupe.
            # Additionally include user id to be safe across chats.
            key = f"cbq:dedupe:{event.from_user.id}:{event.id}"
            ok = await self._redis.set(key, "1", nx=True, ex=self._ttl)
            if not ok:
                try:
                    await event.answer()
                except Exception:
                    pass
                return None
        except Exception:
            # If Redis fails, do not block processing.
            logger.exception("callback_dedupe_failed")

        return await handler(event, data)

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class DedupeMiddleware(BaseMiddleware):
    """Drop duplicate Telegram updates (retries after network errors)."""

    def __init__(self, redis: Redis, ttl_seconds: int = 10) -> None:
        self._redis = redis
        self._ttl = int(ttl_seconds)

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        key = self._key(event)
        if key is None:
            return await handler(event, data)

        try:
            if not await self._redis.set(key, "1", nx=True, ex=self._ttl):
                if isinstance(event, CallbackQuery):
                    try:
                        await event.answer()
                    except Exception:
                        pass
                return None
        except Exception:
            logger.debug("dedupe_redis_failed", exc_info=True)

        return await handler(event, data)

    @staticmethod
    def _key(event: Any) -> str | None:
        if isinstance(event, CallbackQuery):
            user = getattr(event, "from_user", None)
            if user is None:
                return None
            return f"dedupe:cb:{user.id}:{event.id}"
        if isinstance(event, Message):
            user = getattr(event, "from_user", None)
            if user is None:
                chat = getattr(event, "chat", None)
                chat_id = getattr(chat, "id", None)
                return f"dedupe:msg:chat:{chat_id}:{event.message_id}" if chat_id else None
            return f"dedupe:msg:{user.id}:{event.message_id}"
        return None

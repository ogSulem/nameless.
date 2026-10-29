from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Update
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.database.models import User

logger = logging.getLogger(__name__)

TOUCH_EVERY_S = 300


def _from_user(event: Any):
    """Update objects do not carry `from_user`; dig it out of the payload."""
    if isinstance(event, Update):
        for attr in ("message", "edited_message", "business_message", "callback_query", "inline_query"):
            obj = getattr(event, attr, None)
            user = getattr(obj, "from_user", None) if obj is not None else None
            if user is not None:
                return user
        return None
    return getattr(event, "from_user", None)


class PresenceMiddleware(BaseMiddleware):
    """Keep "online" fresh and record last_seen without an UPDATE per message.

    `last_seen_at` powers the 24h-active statistic in the admin panel, and the
    Redis "online" key is what stops ghost users from being matched.
    """

    def __init__(self, redis: Redis, settings: Settings, session_factory: async_sessionmaker) -> None:
        self._redis = redis
        self._settings = settings
        self._session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user = _from_user(event)
        if user is not None and not getattr(user, "is_bot", False):
            tg_id = int(user.id)
            try:
                await self._redis.set(f"online:{tg_id}", "1", ex=int(self._settings.online_ttl_s))
            except Exception:
                pass

            try:
                fresh = await self._redis.set(f"seen:{tg_id}", "1", nx=True, ex=TOUCH_EVERY_S)
            except Exception:
                fresh = False

            if fresh:
                try:
                    async with self._session_factory() as session:
                        await session.execute(
                            update(User)
                            .where(User.telegram_id == tg_id)
                            .values(last_seen_at=datetime.now(tz=UTC))
                        )
                        await session.commit()
                except Exception:
                    logger.debug("presence_touch_failed tg=%s", tg_id, exc_info=True)

        return await handler(event, data)

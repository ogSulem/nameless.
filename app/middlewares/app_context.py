from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from redis.asyncio import Redis

from app.config import Settings

_settings_ref: Settings | None = None


def get_settings() -> Settings:
    """Access settings from code that has no dispatcher context (jobs, scripts, tests)."""
    if _settings_ref is None:
        raise RuntimeError("settings are not initialised yet")
    return _settings_ref


def set_settings(settings: Settings) -> None:
    global _settings_ref
    _settings_ref = settings


class AppContextMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings, redis: Redis) -> None:
        self._settings = settings
        self._redis = redis
        set_settings(settings)

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        data["settings"] = self._settings
        data["redis"] = self._redis
        return await handler(event, data)

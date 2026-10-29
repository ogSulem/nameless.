from __future__ import annotations

import hashlib
import logging
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware

from app.telegram_safe import safe_send_message

logger = logging.getLogger(__name__)


class ErrorBoundaryMiddleware(BaseMiddleware):
    """Catch everything, log with correlation id, alert admins — without user content.

    The previous version forwarded the *text of the failed message* to the alert
    chat. That is a copy of private correspondence leaving the system on every
    crash, so now only the exception type, place and ids are reported.
    """

    def __init__(self, settings, redis) -> None:
        self._settings = settings
        self._redis = redis

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            logger.exception("unhandled_event_error")

            targets = getattr(self._settings, "alerts_target_ids", frozenset())
            if not targets:
                return None

            try:
                tb = traceback.format_exc()
                digest = hashlib.sha256(tb[-500:].encode("utf-8", "ignore")).hexdigest()[:16]
                throttle_key = f"alerts:err:{digest}"
                if not await self._redis.set(throttle_key, "1", nx=True, ex=300):
                    return None
            except Exception:
                tb = traceback.format_exc(limit=8)

            bot = data.get("bot") or getattr(event, "bot", None)
            if bot is None:
                return None

            user = getattr(event, "from_user", None)
            who = f"tg={getattr(user, 'id', None)}" if user is not None else "tg=?"
            msg = "\n".join(
                [
                    "*UNHANDLED ERROR*",
                    who,
                    "",
                    "```",
                    tb[-2500:],
                    "```",
                ]
            )
            for target in targets:
                try:
                    await safe_send_message(bot, int(target), msg[:3900], parse_mode="Markdown")
                except Exception:
                    logger.warning("error_alert_failed target=%s", target)
            return None

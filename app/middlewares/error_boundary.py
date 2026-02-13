from __future__ import annotations

import logging
import traceback
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware

from app.telegram_safe import safe_send_message


logger = logging.getLogger(__name__)


class ErrorBoundaryMiddleware(BaseMiddleware):
    def __init__(self, settings, redis) -> None:
        self._settings = settings
        self._redis = redis

    async def __call__(
        self,
        handler: Callable[[Any, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            logger.exception("unhandled_event_error")

            alerts_chat_id = getattr(self._settings, "alerts_chat_id", None)
            if not alerts_chat_id:
                return None

            # Throttle alerts to avoid spam loops
            try:
                tb_full = traceback.format_exc()
                throttle_key = f"alerts:error_throttle:{hash(tb_full[-500:])}"
                if await self._redis.get(throttle_key):
                    return None
                await self._redis.set(throttle_key, "1", ex=300)
            except Exception:
                tb_full = "traceback_failed"

            try:
                bot = data.get("bot")
                if bot is None:
                    bot = getattr(event, "bot", None)
                if bot is None:
                    return None

                from_user = getattr(event, "from_user", None)
                chat = getattr(event, "chat", None)
                text = getattr(event, "text", None)
                data_str = getattr(event, "data", None)

                who = None
                if from_user is not None:
                    who = f"tg={getattr(from_user, 'id', None)}"
                if chat is not None:
                    who = (who + " " if who else "") + f"chat={getattr(chat, 'id', None)}"

                head = f"*UNHANDLED ERROR*\n{who or ''}".strip()

                payload = ""
                if isinstance(text, str) and text:
                    payload = f"text={text!r}"
                elif isinstance(data_str, str) and data_str:
                    payload = f"data={data_str!r}"

                tb = tb_full if tb_full != "traceback_failed" else traceback.format_exc(limit=8)
                msg = "\n".join([head, payload, "```", tb[-3500:], "```"])
                if len(msg) > 3900:
                    msg = msg[:3900]

                await safe_send_message(
                    bot,
                    int(alerts_chat_id),
                    msg,
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("failed_send_alert")

            return None

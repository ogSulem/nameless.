from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Update

logger = logging.getLogger(__name__)

class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Update, Dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: Dict[str, Any],
    ) -> Any:
        start_time = time.perf_counter()
        update_id = event.update_id
        
        # Inject correlation ID into data for handlers to use if needed
        data["update_id"] = update_id
        
        user = getattr(event.event, "from_user", None)
        user_id = user.id if user else "unknown"
        
        event_type = "unknown"
        if event.message:
            if event.message.successful_payment:
                event_type = "successful_payment"
            else:
                event_type = "message"
        elif event.callback_query:
            event_type = "callback_query"
        elif event.pre_checkout_query:
            event_type = "pre_checkout_query"
        elif event.channel_post:
            event_type = "channel_post"

        logger.info(
            "update_start update_id=%s user_id=%s type=%s",
            update_id,
            user_id,
            event_type
        )
        
        try:
            result = await handler(event, data)
            duration = (time.perf_counter() - start_time) * 1000
            logger.info(
                "update_end update_id=%s user_id=%s duration_ms=%.2f",
                update_id,
                user_id,
                duration
            )
            return result
        except Exception:
            duration = (time.perf_counter() - start_time) * 1000
            logger.error(
                "update_failed update_id=%s user_id=%s duration_ms=%.2f",
                update_id,
                user_id,
                duration
            )
            raise

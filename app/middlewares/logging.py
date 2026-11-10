from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Update

from app.logging.context import clear_update_id, set_update_id
from app.metrics import record_update

logger = logging.getLogger(__name__)
SLOW_MS = 1000.0


class LoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        started = time.perf_counter()
        update_id = event.update_id
        set_update_id(update_id)

        ok = True
        try:
            return await handler(event, data)
        except Exception:
            ok = False
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            try:
                await record_update(ok=ok, duration_ms=duration_ms, slow=duration_ms >= SLOW_MS)
            except Exception:
                pass
            if ok:
                logger.debug("update_ok update_id=%s duration_ms=%.1f", update_id, duration_ms)
            else:
                logger.error("update_failed update_id=%s duration_ms=%.1f", update_id, duration_ms)
            clear_update_id()

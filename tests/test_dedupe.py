from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

from app.middlewares.dedupe import DedupeMiddleware


def _update(update_id: int, tg_id: int = 9901, text: str = "привет") -> Update:
    return Update(
        update_id=update_id,
        message=Message(
            message_id=update_id,
            date=datetime.now(tz=UTC),
            chat=Chat(id=tg_id, type="private"),
            from_user=TgUser(id=tg_id, is_bot=False, first_name="T"),
            text=text,
        ),
    )


@pytest.mark.asyncio
async def test_retried_update_is_processed_once(redis):
    """The dispatcher passes `Update`, not `Message`: the key must be update_id."""
    middleware = DedupeMiddleware(redis)
    handled: list[int] = []

    async def handler(event, data):
        handled.append(event.update_id)
        return "ok"

    assert await middleware(handler, _update(500), {}) == "ok"
    assert await middleware(handler, _update(500), {}) is None  # the retry
    assert handled == [500]

    assert await middleware(handler, _update(501, text="другое"), {}) == "ok"
    assert handled == [500, 501]

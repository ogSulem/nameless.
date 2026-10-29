from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TgUser

from app.database.models import Gender
from app.middlewares.gates import BanGateMiddleware


def _update(tg_id: int, text: str = "привет", update_id: int = 1) -> tuple[Update, list[str]]:
    sent: list[str] = []

    class _Bot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.append(text)

    bot = _Bot()

    message = Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=Chat(id=tg_id, type="private"),
        from_user=TgUser(id=tg_id, is_bot=False, first_name="T"),
        text=text,
    )
    message = message.as_(bot) if hasattr(message, "as_") else message
    return Update(update_id=update_id, message=message), sent


@pytest.mark.asyncio
async def test_banned_user_is_blocked(session, session_factory, settings, make_user):
    await make_user(700, banned=True)
    gate = BanGateMiddleware(settings, session_factory)
    called: list[str] = []

    async def handler(event, data):
        called.append("handler")
        return "ok"

    update, sent = _update(700)
    result = await gate(handler, update, {"session": session, "redis": None})

    assert result is None
    assert called == []
    assert sent and "заблокирован" in sent[0]


@pytest.mark.asyncio
async def test_banned_user_can_still_delete_data(session, session_factory, settings, make_user):
    await make_user(701, banned=True)
    gate = BanGateMiddleware(settings, session_factory)
    called: list[str] = []

    async def handler(event, data):
        called.append("handler")
        return "ok"

    update, _ = _update(701, text="/delete_me")
    result = await gate(handler, update, {"session": session, "redis": None})

    assert result == "ok"
    assert called == ["handler"]


@pytest.mark.asyncio
async def test_regular_user_passes_through(session, session_factory, settings, make_user):
    await make_user(702, gender=Gender.female)
    gate = BanGateMiddleware(settings, session_factory)

    async def handler(event, data):
        return "ok"

    update, sent = _update(702)
    assert await gate(handler, update, {"session": session, "redis": None}) == "ok"
    assert sent == []


@pytest.mark.asyncio
async def test_unknown_user_passes_through_to_registration(session, session_factory, settings):
    gate = BanGateMiddleware(settings, session_factory)

    async def handler(event, data):
        return "ok"

    update, sent = _update(703)
    assert await gate(handler, update, {"session": session, "redis": None}) == "ok"
    assert sent == []


@pytest.mark.asyncio
async def test_staff_is_never_locked_out(session, session_factory, settings, make_user):
    # settings fixture: ADMINS=1
    await make_user(1, banned=True)
    gate = BanGateMiddleware(settings, session_factory)

    async def handler(event, data):
        return "ok"

    update, sent = _update(1)
    assert await gate(handler, update, {"session": session, "redis": None}) == "ok"
    assert sent == []

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


@pytest.mark.asyncio
async def test_ban_flag_is_cached_and_invalidated(session, session_factory, settings, make_user, redis, engine):
    """The gate runs on every update: it must not query Postgres every time."""
    from sqlalchemy import event

    from app.redis import keys

    user = await make_user(702, banned=False)
    gate = BanGateMiddleware(settings, session_factory)
    calls: list[str] = []

    async def handler(event, data):
        calls.append("handler")
        return "ok"

    statements: list[str] = []

    def _count(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    try:
        data = {"session": session, "redis": redis}
        update, _ = _update(702)
        assert await gate(handler, update, data) == "ok"
        assert len(statements) == 1, statements  # cache miss: one lookup

        statements.clear()
        update2, _ = _update(702, update_id=2)
        assert await gate(handler, update2, data) == "ok"
        assert statements == [], "the second update must be answered from Redis"
        assert calls == ["handler", "handler"]
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)

    # A moderator's ban is written to the DB and the cache key is dropped, so the
    # very next update is refused instead of waiting for the TTL.
    user.is_banned = True
    await session.commit()
    await redis.delete(keys.ban_flag_key(702))

    update3, sent = _update(702, update_id=3)
    assert await gate(handler, update3, {"session": session, "redis": redis}) is None
    assert calls == ["handler", "handler"], "a banned user must not reach the handler"
    assert sent and "заблокирован" in sent[0]

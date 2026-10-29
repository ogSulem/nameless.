from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.database.models import User

logger = logging.getLogger(__name__)

# Even a banned user keeps the legally required escape hatches: read the policy,
# read the terms, and erase their data.
ALLOWED_FOR_BANNED = {"start", "help", "privacy", "terms", "delete_me"}

BAN_TEXT = (
    "<b>⛔ Доступ ограничен</b>\n\n"
    "Ваш профиль заблокирован за нарушение правил.\n"
    "Ознакомиться с политикой: /privacy · Удалить данные: /delete_me"
)


def _user_id(event: TelegramObject) -> int | None:
    for attr in ("message", "edited_message", "callback_query", "inline_query", "business_message"):
        obj = getattr(event, attr, None)
        if obj is not None and getattr(obj, "from_user", None) is not None:
            return int(obj.from_user.id)
    return None


def _command(event: TelegramObject) -> str | None:
    for attr in ("message", "edited_message"):
        obj = getattr(event, attr, None)
        text = getattr(obj, "text", None) if obj is not None else None
        if isinstance(text, str) and text.startswith("/"):
            return text[1:].split()[0].split("@")[0].lower()
    return None


class BanGateMiddleware(BaseMiddleware):
    """One checkpoint for bans instead of scattered `if user.is_banned` checks.

    Runs *after* DbSessionMiddleware, so it reuses the per-update session.
    """

    def __init__(self, settings: Settings, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._settings = settings
        self._session_factory = session_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return await handler(event, data)

        tg_id = _user_id(event)
        if tg_id is None or tg_id in self._settings.staff_ids:
            return await handler(event, data)

        command = _command(event)
        if command in ALLOWED_FOR_BANNED:
            return await handler(event, data)

        session: AsyncSession | None = data.get("session")
        owns_session = session is None
        if session is None:
            session = self._session_factory()

        try:
            user = (
                await session.execute(select(User).where(User.telegram_id == tg_id))
            ).scalar_one_or_none()
            if user is None or not user.is_banned:
                return await handler(event, data)

            await self._refuse(event, data.get("redis"))
            logger.info("ban_gate_blocked tg=%s", tg_id)
            return None
        finally:
            if owns_session:
                await session.close()

    async def _refuse(self, event: Update, redis: Redis | None) -> None:
        cb: CallbackQuery | None = event.callback_query
        if cb is not None:
            try:
                await cb.answer(BAN_TEXT.replace("<b>", "").replace("</b>", ""), show_alert=True)
            except Exception:
                logger.debug("ban_gate_answer_failed", exc_info=True)
            return

        message: Message | None = event.message
        if message is not None and message.chat is not None:
            try:
                await message.bot.send_message(message.chat.id, BAN_TEXT)
            except Exception:
                logger.debug("ban_gate_send_failed", exc_info=True)

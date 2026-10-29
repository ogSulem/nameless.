from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.flows.profile import show_profile
from app.handlers.commands import get_user
from app.keyboards.start import back_kb
from app.redis import keys
from app.services.dialog import DialogService
from app.telegram_safe import safe_answer_callback
from app.ui import edit_ui, send_new_ui

logger = logging.getLogger(__name__)
router = Router(name="fallback")


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery) -> None:
    await safe_answer_callback(call)


@router.callback_query()
async def stale_callback(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    """Buttons from deleted/old messages must never leave the client spinning."""
    tg_id = int(call.from_user.id) if call.from_user else 0
    await safe_answer_callback(call, "Кнопка устарела")

    if not tg_id:
        return

    user = await get_user(session, tg_id)
    if user is None:
        await edit_ui(call.bot, redis, tg_id, "Нажмите /start, чтобы начать.")
        return

    if await DialogService(redis, settings).active_dialog_id(session, tg_id):
        return

    await show_profile(call.bot, redis, session, settings, tg_id)


@router.message()
async def unknown_message(
    message: Message, session: AsyncSession, state: FSMContext, redis: Redis, settings: Settings
) -> None:
    """Last resort: keep the chat clean and guide the user back."""
    if message.from_user is None:
        return
    try:
        await message.delete()
    except Exception:
        pass

    tg_id = int(message.from_user.id)

    if await state.get_state() is not None:
        return

    user = await get_user(session, tg_id)
    if user is None:
        await send_new_ui(message.bot, redis, tg_id, "Нажмите /start, чтобы начать.")
        return

    if await DialogService(redis, settings).active_dialog_id(session, tg_id):
        return

    if await redis.get(keys.pending_rating(tg_id)):
        return

    await edit_ui(
        message.bot,
        redis,
        tg_id,
        "Сообщение отправлено в никуда — сейчас нет собеседника.\n\n"
        "Нажмите «🔎 Найти собеседника», чтобы начать диалог, или /help.",
        kb=back_kb("menu:profile", "← В профиль"),
    )

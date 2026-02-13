from __future__ import annotations

import logging
from datetime import date

from aiogram import F, Router
from aiogram.types import CallbackQuery
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import User
from app.keyboards.dialog_reply import dialog_reply_kb
from app.keyboards.search import cancel_search_kb
from app.keyboards.search import search_kb
from app.services.matchmaking import MatchmakingService
from app.flows.profile import show_profile
from app.redis import keys
from app.ui import edit_ui, get_ui_message_id, send_new_ui
from app.telegram_safe import safe_delete_message

logger = logging.getLogger(__name__)
router = Router(name="search")


def _search_message_key(tg_id: int) -> str:
    return keys.ui_search_message_id(tg_id)


async def _get_search_message_id(redis: Redis, tg_id: int) -> int | None:
    v = await redis.get(_search_message_key(tg_id))
    return int(v) if v else None


async def _set_search_message_id(redis: Redis, tg_id: int, message_id: int) -> None:
    await redis.set(_search_message_key(tg_id), str(message_id), ex=60 * 60 * 24 * 30)


async def _clear_search_message_id(redis: Redis, tg_id: int) -> None:
    await redis.delete(_search_message_key(tg_id))


def _age(birth_date: date) -> int:
    today = date.today()
    years = today.year - birth_date.year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def _gender_short(v: str) -> str:
    return "М" if v == "male" else "Ж"


@router.callback_query(F.data == "cancel_search")
async def cancel_search(call: CallbackQuery, session: AsyncSession, redis: Redis) -> None:
    res = await session.execute(select(User).where(User.telegram_id == call.from_user.id))
    user = res.scalar_one_or_none()
    if user is not None:
        svc = MatchmakingService(redis)
        await svc.dequeue_from_all(user.telegram_id, user.city)

    await _clear_search_message_id(redis, call.from_user.id)
    await show_profile(call.bot, redis, session, call.from_user.id)
    await call.answer()


@router.callback_query(F.data == "search")
async def search_start(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    res = await session.execute(select(User).where(User.telegram_id == call.from_user.id))
    user = res.scalar_one_or_none()
    if user is None:
        await send_new_ui(call.bot, redis, call.from_user.id, "Сначала пройди регистрацию через /start")
        await call.answer()
        return

    if user.is_banned:
        await send_new_ui(call.bot, redis, call.from_user.id, "Твой аккаунт заблокирован.")
        await call.answer()
        return

    svc = MatchmakingService(redis)
    premium = await svc.is_user_premium(session, user.id)
    result = await svc.try_match(session=session, user=user, premium=premium)

    if result is None:
        await edit_ui(call.bot, redis, call.from_user.id, "Ищу собеседника...", kb=cancel_search_kb())
        mid = await get_ui_message_id(redis, call.from_user.id)
        if mid:
            await _set_search_message_id(redis, call.from_user.id, mid)
        await call.answer()
        return

    # Instant match: remove the current UI (profile) so the chat stays single-message
    current_ui_mid = await get_ui_message_id(redis, call.from_user.id)
    if current_ui_mid:
        await safe_delete_message(call.bot, call.from_user.id, current_ui_mid)

    res2 = await session.execute(select(User).where(User.telegram_id == result.partner_user_id))
    partner = res2.scalar_one()

    my_card = (
        "Собеседник найден!\n"
        f"Пол: {_gender_short(partner.gender.value)}\n"
        f"Возраст: {_age(partner.birth_date)}\n"
        f"Город: {partner.city or '-'}\n"
        f"Рейтинг: {float(partner.season_rating_chat or 0.0):.1f}"
    )
    partner_card = (
        "Собеседник найден!\n"
        f"Пол: {_gender_short(user.gender.value)}\n"
        f"Возраст: {_age(user.birth_date)}\n"
        f"Город: {user.city or '-'}\n"
        f"Рейтинг: {float(user.season_rating_chat or 0.0):.1f}"
    )

    # 1. Replace searching UI with a new card message that has reply controls
    mid = await _get_search_message_id(redis, user.telegram_id)
    if mid:
        await safe_delete_message(call.bot, user.telegram_id, mid)
        await _clear_search_message_id(redis, user.telegram_id)

    # card already sends with dialog_reply_kb() via send_new_ui
    await send_new_ui(call.bot, redis, user.telegram_id, my_card, kb=dialog_reply_kb())

    # 2. Dequeue
    try:
        await svc.dequeue_from_all(user.telegram_id, user.city)
    except Exception:
        logger.exception("failed_dequeue_on_match tg=%s", user.telegram_id)

    try:
        await svc.dequeue_from_all(partner.telegram_id, partner.city)
    except Exception:
        logger.exception("failed_dequeue_on_match tg=%s", partner.telegram_id)

    # Partner
    try:
        partner_mid = await _get_search_message_id(redis, result.partner_user_id)
        if partner_mid:
            await safe_delete_message(call.bot, result.partner_user_id, partner_mid)
            await _clear_search_message_id(redis, result.partner_user_id)

        await send_new_ui(call.bot, redis, result.partner_user_id, partner_card, kb=dialog_reply_kb())
    except Exception:
        logger.exception("failed_notify_partner tg=%s", result.partner_user_id)

    await call.answer()
    logger.info("search_matched dialog_id=%s u=%s p=%s", result.dialog_id, user.telegram_id, result.partner_user_id)

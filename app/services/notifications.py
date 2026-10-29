from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import User
from app.flows.profile import age_of, premium_active
from app.keyboards.dialog_reply import dialog_reply_kb
from app.redis import keys
from app.telegram_safe import UserBlockedBot, safe_delete_message, safe_send_message

logger = logging.getLogger(__name__)


def partner_card(partner: User) -> str:
    gender = "👦 парень" if partner.gender.value == "male" else "👧 девушка"
    city = escape(partner.city) if partner.city else "🌍 вся страна"

    if int(partner.chat_ratings_count or 0) == 0:
        rating = "⭐ рейтинг: новый собеседник"
    else:
        rating = f"⭐ рейтинг: <b>{float(partner.season_rating_chat or 0):.1f}</b>/10"
        if int(partner.appearance_ratings_count or 0) > 0:
            rating += f" · внешность: {float(partner.season_rating_appearance or 0):.1f}/10"

    return "\n".join(
        [
            "🔗 <b>Собеседник найден</b>",
            "",
            f"{gender}, {age_of(partner.birth_date)}",
            f"🏙 {city}",
            rating,
            ("💎 Premium" if premium_active(partner) else ""),
            "",
            "<i>Собеседник не видит ваш профиль. Правила: без оскорблений и личных данных.</i>",
        ]
    ).replace("\n\n\n", "\n\n")


async def announce_match(
    bot: Bot,
    redis: Redis,
    session: AsyncSession,
    settings: Settings,
    dialog_id: int,
    first_tg: int,
    second_tg: int,
) -> None:
    """Send both sides their partner card and switch the reply keyboard to dialog mode."""
    res = await session.execute(select(User).where(User.telegram_id.in_([int(first_tg), int(second_tg)])))
    users = {int(u.telegram_id): u for u in res.scalars().all()}
    first = users.get(int(first_tg))
    second = users.get(int(second_tg))
    if first is None or second is None:
        logger.warning("announce_match_missing_user dialog_id=%s", dialog_id)
        return

    for me, partner in ((first, second), (second, first)):
        tg = int(me.telegram_id)
        try:
            search_mid = await _pop_int(redis, keys.ui_search_message_id(tg))
            if search_mid:
                await safe_delete_message(bot, tg, search_mid)
            ui_mid = await _pop_int(redis, keys.ui_message_id(tg))
            if ui_mid:
                await safe_delete_message(bot, tg, ui_mid)
        except Exception:
            logger.warning("announce_cleanup_failed tg=%s", tg, exc_info=True)

        try:
            await safe_send_message(bot, tg, partner_card(partner), reply_markup=dialog_reply_kb())
        except UserBlockedBot:
            logger.info("announce_blocked tg=%s", tg)
        except Exception:
            logger.exception("announce_send_failed tg=%s", tg)

    logger.info("match_announced dialog_id=%s a=%s b=%s", dialog_id, first_tg, second_tg)


async def _pop_int(redis: Redis, key: str) -> int | None:
    try:
        raw = await redis.get(key)
        await redis.delete(key)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return int(raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        return None

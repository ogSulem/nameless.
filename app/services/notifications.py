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
from app.ui import set_ui_message_id

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
            "<i>💡 Начните с «привет 👋» — на него отвечают чаще всего.</i>",
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

    blocked: list[int] = []
    delivered: list[int] = []

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
            # Tracked as the current UI message, so finishing the dialog can clean it up.
            msg = await safe_send_message(bot, tg, partner_card(partner), reply_markup=dialog_reply_kb())
            if msg is not None:
                await set_ui_message_id(redis, tg, msg.message_id)
            delivered.append(tg)
        except UserBlockedBot:
            blocked.append(tg)
            logger.info("announce_blocked tg=%s", tg)
        except Exception:
            blocked.append(tg)
            logger.exception("announce_send_failed tg=%s", tg)

    if blocked:
        # Never leave the other side staring at a dialog with nobody on the line.
        await _abort_match(bot, redis, session, settings, dialog_id, blocked, delivered)

    from app.services.activity import bump

    await bump(redis, "match")

    logger.info(
        "match_announced dialog_id=%s a=%s b=%s blocked=%s", dialog_id, first_tg, second_tg, blocked
    )


async def _abort_match(
    bot: Bot,
    redis: Redis,
    session: AsyncSession,
    settings: Settings,
    dialog_id: int,
    blocked: list[int],
    delivered: list[int],
) -> None:
    """One side blocked the bot: close the dialog and say so instead of hanging."""
    from app.keyboards.start import back_kb
    from app.services.dialog import DialogService
    from app.ui import send_new_ui

    try:
        await DialogService(redis, settings).finish(session, dialog_id, actor_tg_id=None)
    except Exception:
        logger.exception("abort_match_finish_failed dialog_id=%s", dialog_id)

    for tg in delivered:
        try:
            await send_new_ui(
                bot,
                redis,
                tg,
                "Собеседник больше не может получать сообщения — диалог отменён.\n\n"
                "Нажмите «🔎 Найти собеседника», чтобы попробовать снова.",
                kb=back_kb("search:start", "🔎 Найти собеседника"),
            )
        except Exception:
            logger.info("abort_match_notify_failed tg=%s", tg)


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

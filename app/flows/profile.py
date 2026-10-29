from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from html import escape

from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import User
from app.keyboards.profile import profile_kb
from app.redis import keys
from app.services.matchmaking import MatchmakingService
from app.ui import edit_ui, send_new_ui

logger = logging.getLogger(__name__)


def age_of(birth_date: date) -> int:
    today = datetime.now(tz=UTC).date()
    return today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))


def premium_active(user: User) -> bool:
    if user.subscription_until is None:
        return False
    until = user.subscription_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until > datetime.now(tz=UTC)


def rating_line(user: User) -> str:
    if int(user.chat_ratings_count or 0) == 0:
        return "⭐ Рейтинг общения: <i>новый пользователь</i>"
    line = f"⭐ Рейтинг общения: <b>{float(user.season_rating_chat or 0):.1f}</b>/10"
    if int(user.appearance_ratings_count or 0) > 0:
        line += f" · внешность: <b>{float(user.season_rating_appearance or 0):.1f}</b>/10"
    return line


def profile_text(user: User, settings: Settings, premium_mode: str = "chat") -> str:
    gender_value = getattr(user.gender, "value", user.gender)
    gender = "👦 парень" if gender_value == "male" else "👧 девушка"
    city = escape(user.city) if user.city else "🌍 вся страна"
    is_premium = premium_active(user)

    lines = [
        f"<b>👤 Профиль</b>{' 💎' if is_premium else ''}",
        "",
        f"Пол: {gender}",
        f"Возраст: {age_of(user.birth_date)}",
        f"Город: {city}",
        rating_line(user),
    ]
    if is_premium and user.subscription_until is not None:
        until = user.subscription_until
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
        mode_label = "внешность" if premium_mode == "appearance" else "общение"
        lines.append(f"💎 Premium до <b>{until:%d.%m.%Y}</b> · подбор по: <b>{mode_label}</b>")
    elif settings.payments_enabled:
        lines.append("💎 Premium: собеседники с высоким рейтингом и приоритет в подборе")

    if user.is_banned:
        lines = ["🚫 <b>Аккаунт заблокирован.</b>", "", "Если вы считаете это ошибкой — напишите в поддержку."]
    return "\n".join(lines)


async def show_profile(
    bot: Bot,
    redis: Redis,
    session: AsyncSession,
    settings: Settings,
    tg_id: int,
    *,
    new_message: bool = False,
) -> None:
    res = await session.execute(select(User).where(User.telegram_id == int(tg_id)))
    user = res.scalar_one_or_none()
    if user is None:
        await edit_ui(bot, redis, tg_id, "Нажмите /start, чтобы начать.")
        return

    is_premium = premium_active(user)
    mode = "chat"
    if is_premium:
        try:
            mode = await MatchmakingService(redis, settings).premium_mode(int(tg_id))
        except Exception:
            mode = "chat"

    text = profile_text(user, settings, premium_mode=mode)
    kb = profile_kb(
        premium=is_premium, payments_enabled=settings.payments_enabled, premium_mode=mode
    )
    if new_message:
        await send_new_ui(bot, redis, tg_id, text, kb=kb)
    else:
        await edit_ui(bot, redis, tg_id, text, kb=kb)


async def invalidate_profile(redis: Redis, tg_id: int) -> None:
    try:
        await redis.delete(keys.profile_text(int(tg_id)))
    except Exception:
        pass

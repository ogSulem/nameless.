from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.keyboards.profile import profile_kb
from app.services.rating import RatingService
from app.ui import edit_ui


def _fmt_sub_until(until: datetime | None) -> str:
    if until is None:
        return "нет"
    dt = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
    active = dt > datetime.now(tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M") + (" (active)" if active else " (expired)")


async def show_profile(bot: Bot, redis: Redis, session: AsyncSession, tg_id: int) -> None:
    res = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = res.scalar_one_or_none()
    if user is None:
        await edit_ui(bot, redis, tg_id, "Сначала запусти /start")
        return

    # Force expire existing data to ensure we get fresh state from DB
    session.expire(user)
    res = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = res.scalar_one()

    try:
        await RatingService().on_rating_saved(session=session, to_user_id=user.id)
        await session.refresh(user)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("profile_rating_recalc_failed tg_id=%s", tg_id)

    gender = "М" if user.gender.value == "male" else "Ж"
    age = None
    if user.birth_date:
        today = datetime.now(tz=timezone.utc).date()
        age = today.year - user.birth_date.year - (
            (today.month, today.day) < (user.birth_date.month, user.birth_date.day)
        )

    city = user.city or "🌍 глобально"
    sub = _fmt_sub_until(user.subscription_until)
    is_premium = False
    if user.subscription_until:
        until_dt = user.subscription_until if user.subscription_until.tzinfo else user.subscription_until.replace(tzinfo=timezone.utc)
        is_premium = until_dt > datetime.now(tz=timezone.utc)

    # RE-FETCH USER TO BE ABSOLUTELY SURE
    await session.commit() # Commit any pending rating changes
    await session.refresh(user)
    
    chat_r = float(user.season_rating_chat or 0.0)
    app_r = float(user.season_rating_appearance or 0.0)
    rating_line = f"Рейтинг: {chat_r:.1f}"
    if app_r > 0:
        rating_line += f" / {app_r:.1f}"

    text = "\n".join(
        [
            "👤 *Профиль*" + (" 💎" if is_premium else ""),
            f"Пол: {gender}",
            f"Возраст: {age if age is not None else '-'}",
            f"Город: {city}",
            rating_line,
            f"Premium: {sub}\n",
            "💎 С Premium попадаются собеседники с рейтингом 7+" if not is_premium else "💎 У вас активен Premium статус!",
        ]
    )

    await edit_ui(bot, redis, tg_id, text, kb=profile_kb())

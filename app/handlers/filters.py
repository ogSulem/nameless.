"""Premium matching filters: age range and "my city only".

Why this is the paid feature: in every anonymous chat the number one complaint is
"мне попадаются не те". Sorting the queue by rating is nice; letting a person say
"хочу 25–34 из своего города" is what people actually pay for. It is also honest:
the filter is visible, explainable and never pretends to guarantee a match.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import User
from app.flows.profile import premium_active, show_profile
from app.keyboards.filters import filters_kb, filters_locked_kb
from app.services.matchmaking import DEFAULT_AGE_MAX, DEFAULT_AGE_MIN
from app.telegram_safe import safe_answer_callback
from app.ui import edit_ui

logger = logging.getLogger(__name__)
router = Router(name="filters")

# Few presets instead of a slider: Telegram has no slider, and two taps must be enough.
AGE_PRESETS: dict[str, tuple[int, int, str]] = {
    "18-24": (18, 24, "18–24"),
    "25-34": (25, 34, "25–34"),
    "35-45": (35, 45, "35–45"),
    "45-99": (45, 99, "45+"),
}


def _age_label(age_min: int, age_max: int) -> str:
    for lo, hi, label in AGE_PRESETS.values():
        if (lo, hi) == (age_min, age_max):
            return label
    if (age_min, age_max) == (DEFAULT_AGE_MIN, DEFAULT_AGE_MAX):
        return "любой"
    return f"{age_min}–{age_max}"


def filters_text(user: User, settings: Settings) -> str:
    age = _age_label(int(user.filter_age_min or DEFAULT_AGE_MIN), int(user.filter_age_max or DEFAULT_AGE_MAX))
    city = "только мой город" if user.filter_city_only else "вся страна"
    return "\n".join(
        [
            "<b>🎛 Фильтры подбора</b>",
            "",
            f"Возраст собеседника: <b>{age}</b>",
            f"География: <b>{city}</b>",
            "",
            "<i>Фильтр работает в обе стороны: вас тоже увидят только те, кому вы "
            "подходите. Если под фильтр никто не подходит, бот не подменит его "
            "молча — вы будете ждать подходящего человека. Premium ускоряет поиск, "
            "но не гарантирует конкретного собеседника.</i>",
        ]
    )


async def show_filters(
    bot, redis: Redis, session: AsyncSession, settings: Settings, tg_id: int
) -> None:
    user = (
        await session.execute(select(User).where(User.telegram_id == int(tg_id)))
    ).scalar_one_or_none()
    if user is None:
        await edit_ui(bot, redis, tg_id, "Нажмите /start, чтобы начать.")
        return

    if not premium_active(user):
        await edit_ui(bot, redis, tg_id, filters_locked_text(settings), kb=filters_locked_kb(settings))
        return

    await edit_ui(bot, redis, tg_id, filters_text(user, settings), kb=filters_kb(user))


def filters_locked_text(settings: Settings) -> str:
    return "\n".join(
        [
            "<b>🎛 Фильтры подбора</b>",
            "",
            "Возраст и «только мой город» — часть Premium.",
            "",
            "<i>Premium также поднимает вас в очереди и даёт выбор: подбирать по "
            "рейтингу общения или по внешности.</i>",
        ]
    )


@router.callback_query(F.data == "filter:menu")
async def filter_menu(
    call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    await show_filters(call.bot, redis, session, settings, int(call.from_user.id))
    await safe_answer_callback(call)


@router.callback_query(F.data.startswith("filter:age:"))
async def filter_age(
    call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    tg_id = int(call.from_user.id)
    preset = (call.data or "").split(":")[-1]
    if preset == "any":
        age_min, age_max = DEFAULT_AGE_MIN, DEFAULT_AGE_MAX
    elif preset in AGE_PRESETS:
        age_min, age_max = AGE_PRESETS[preset][0], AGE_PRESETS[preset][1]
    else:
        await safe_answer_callback(call, "Неизвестный диапазон")
        return

    user = (
        await session.execute(select(User).where(User.telegram_id == tg_id))
    ).scalar_one_or_none()
    if user is None or not premium_active(user):
        await safe_answer_callback(call, "Фильтры доступны в Premium", show_alert=True)
        await show_filters(call.bot, redis, session, settings, tg_id)
        return

    user.filter_age_min = age_min
    user.filter_age_max = age_max
    await session.commit()
    await show_filters(call.bot, redis, session, settings, tg_id)
    await safe_answer_callback(call, f"Возраст: {_age_label(age_min, age_max)}")


@router.callback_query(F.data == "filter:city")
async def filter_city(
    call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    tg_id = int(call.from_user.id)
    user = (
        await session.execute(select(User).where(User.telegram_id == tg_id))
    ).scalar_one_or_none()
    if user is None or not premium_active(user):
        await safe_answer_callback(call, "Фильтры доступны в Premium", show_alert=True)
        await show_filters(call.bot, redis, session, settings, tg_id)
        return

    if not user.filter_city_only and not user.city:
        await safe_answer_callback(
            call, "Сначала укажите город в профиле — иначе фильтровать нечего", show_alert=True
        )
        return

    user.filter_city_only = not user.filter_city_only
    await session.commit()
    await show_filters(call.bot, redis, session, settings, tg_id)
    await safe_answer_callback(call, "Только мой город" if user.filter_city_only else "Вся страна")


@router.callback_query(F.data == "filter:reset")
async def filter_reset(
    call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    tg_id = int(call.from_user.id)
    user = (
        await session.execute(select(User).where(User.telegram_id == tg_id))
    ).scalar_one_or_none()
    if user is None or not premium_active(user):
        await safe_answer_callback(call, "Фильтры доступны в Premium", show_alert=True)
        return

    user.filter_age_min = DEFAULT_AGE_MIN
    user.filter_age_max = DEFAULT_AGE_MAX
    user.filter_city_only = False
    await session.commit()
    await show_filters(call.bot, redis, session, settings, tg_id)
    await safe_answer_callback(call, "Сброшено")


@router.callback_query(F.data == "filter:close")
async def filter_close(
    call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    await show_profile(call.bot, redis, session, settings, int(call.from_user.id))
    await safe_answer_callback(call)

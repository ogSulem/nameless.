from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.config import Settings
from app.database.models import User

# (label, callback, age_min, age_max)
AGE_BUTTONS: tuple[tuple[str, str, int, int], ...] = (
    ("18–24", "filter:age:18-24", 18, 24),
    ("25–34", "filter:age:25-34", 25, 34),
    ("35–45", "filter:age:35-45", 35, 45),
    ("45+", "filter:age:45-99", 45, 99),
)


def filters_kb(user: User) -> InlineKeyboardMarkup:
    current = (int(user.filter_age_min or 18), int(user.filter_age_max or 99))
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=("✅ Любой возраст" if current == (18, 99) else "Любой возраст"),
                callback_data="filter:age:any",
            )
        ]
    ]
    for label, callback, age_min, age_max in AGE_BUTTONS:
        mark = "✅ " if current == (age_min, age_max) else ""
        rows.append([InlineKeyboardButton(text=f"{mark}{label}", callback_data=callback)])
    rows.append(
        [
            InlineKeyboardButton(
                text="✅ Только мой город" if user.filter_city_only else "🏙 Только мой город",
                callback_data="filter:city",
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="↺ Сбросить", callback_data="filter:reset")])
    rows.append([InlineKeyboardButton(text="← В профиль", callback_data="filter:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def filters_locked_kb(settings: Settings) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if settings.payments_enabled:
        rows.append([InlineKeyboardButton(text="💎 Оформить Premium", callback_data="pay:premium")])
    rows.append([InlineKeyboardButton(text="← В профиль", callback_data="filter:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

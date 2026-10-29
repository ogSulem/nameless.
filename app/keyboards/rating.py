from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def rating_kb(prefix: str, *, with_complaint: bool = True, with_skip: bool = True) -> InlineKeyboardMarkup:
    """0..10 as buttons — users should not have to guess that a bare number must be typed."""
    rows: list[list[InlineKeyboardButton]] = []
    for start in (0, 5):
        rows.append(
            [
                InlineKeyboardButton(text=str(v), callback_data=f"{prefix}:{v}")
                for v in range(start, min(start + 5, 11))
            ]
        )
    if with_skip:
        rows.append([InlineKeyboardButton(text="Пропустить", callback_data="rate:skip")])
    if with_complaint:
        rows.append([InlineKeyboardButton(text="🚨 Пожаловаться", callback_data="complaint:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def complaint_prompt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Отмена", callback_data="complaint:cancel")]]
    )


def after_rating_kb(*, next_label: str = "🔎 Искать собеседника") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=next_label, callback_data="search:start")]]
    )

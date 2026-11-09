from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

SCALE = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10)


def rating_kb(prefix: str) -> InlineKeyboardMarkup:
    """All eleven values 0..10 as buttons.

    The previous layout used two rows of five, so **10 was unreachable** — the top
    of the scale only existed for people who guessed they could type it.
    """
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text=str(value), callback_data=f"{prefix}:{value}")
            for value in SCALE[start : start + 6]
        ]
        for start in (0, 6)
    ]
    rows.append([InlineKeyboardButton(text="Пропустить", callback_data="rate:skip")])
    rows.append([InlineKeyboardButton(text="🚨 Пожаловаться", callback_data="complaint:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def complaint_prompt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Отмена", callback_data="complaint:cancel")]]
    )

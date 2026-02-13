from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def rating_kb(prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for start in (0, 6):
        row = []
        for v in range(start, start + 6):
            if v > 10:
                continue
            row.append(InlineKeyboardButton(text=str(v), callback_data=f"{prefix}:{v}"))
        rows.append(row)

    rows.append([InlineKeyboardButton(text="🚨 Пожаловаться", callback_data="complaint")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def complaint_only_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🚨 Пожаловаться", callback_data="complaint")]]
    )


def complaint_prompt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="complaint_cancel")]]
    )

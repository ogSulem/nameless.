from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# 1..10 — no "0": a zero reads as "not rated at all" and the lowest honest score
# people mean is 1. Two rows of five keep every value one tap away.
SCALE = tuple(range(1, 11))


def rating_kb(prefix: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text=str(value), callback_data=f"{prefix}:{value}")
            for value in SCALE[start : start + 5]
        ]
        for start in (0, 5)
    ]
    rows.append([InlineKeyboardButton(text="Пропустить", callback_data="rate:skip")])
    rows.append([InlineKeyboardButton(text="🚨 Пожаловаться", callback_data="complaint:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def complaint_prompt_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Отмена", callback_data="complaint:cancel")]]
    )

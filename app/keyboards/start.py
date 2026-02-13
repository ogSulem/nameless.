from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def gender_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="М", callback_data="male"),
                InlineKeyboardButton(text="Ж", callback_data="female"),
            ]
        ]
    )


def skip_city_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🌍 Глобальный поиск", callback_data="city_global")]]
    )

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔎 Поиск", callback_data="search")],
            [InlineKeyboardButton(text="💎 Оплатить Premium", callback_data="menu_premium")],
            [InlineKeyboardButton(text="🏙 Поменять город", callback_data="profile_change_city")],
        ]
    )

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def searching_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="❌ Отменить поиск", callback_data="search:cancel")]]
    )


def match_card_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⏭️ Следующий", callback_data="dialog:next")]]
    )

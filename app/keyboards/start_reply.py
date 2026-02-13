from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def start_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="/start")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Нажми /start или используй кнопки",
    )

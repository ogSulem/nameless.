from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def dialog_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⏭️"), KeyboardButton(text="🛑")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Пиши сообщение или используй кнопки",
    )

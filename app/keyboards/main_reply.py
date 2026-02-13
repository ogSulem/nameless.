from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="/start"), KeyboardButton(text="Поиск")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Используй кнопки",
    )


def searching_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="/start"), KeyboardButton(text="Отмена поиска")]],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Идёт поиск…",
    )

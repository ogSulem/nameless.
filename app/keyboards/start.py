from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def consent_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Мне 18+, принимаю условия", callback_data="consent:accept")],
            [
                InlineKeyboardButton(text="📄 Условия", callback_data="legal:terms"),
                InlineKeyboardButton(text="🔒 Данные", callback_data="legal:privacy"),
            ],
        ]
    )


def gender_kb(prefix: str = "reg:gender") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👦 Парень", callback_data=f"{prefix}:male"),
                InlineKeyboardButton(text="👧 Девушка", callback_data=f"{prefix}:female"),
            ]
        ]
    )


def seek_gender_kb(prefix: str = "reg:seek") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👧 Девушек", callback_data=f"{prefix}:female"),
                InlineKeyboardButton(text="👦 Парней", callback_data=f"{prefix}:male"),
            ],
            [InlineKeyboardButton(text="🤷 Не важно", callback_data=f"{prefix}:any")],
        ]
    )


def city_kb(global_callback: str = "reg:city:global") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🌍 Вся страна (глобальный поиск)", callback_data=global_callback)],
        ]
    )


def back_kb(callback: str = "menu:profile", text: str = "← Назад") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=callback)]])

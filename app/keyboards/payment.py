from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def payment_kb(invoice_url: str | None, *, pay_text: str = "💳 Оплатить") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if invoice_url:
        rows.append([InlineKeyboardButton(text=pay_text, url=invoice_url)])
    rows.append([InlineKeyboardButton(text="🔄 Я оплатил — проверить", callback_data="pay:check")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="menu:profile")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoice_kb(pay_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
            [InlineKeyboardButton(text="← Назад", callback_data="menu:profile")],
        ]
    )

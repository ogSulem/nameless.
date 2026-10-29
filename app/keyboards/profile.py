from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def profile_kb(
    *,
    premium: bool,
    payments_enabled: bool,
    premium_mode: str = "chat",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="🔎 Найти собеседника", callback_data="search:start")],
    ]
    if payments_enabled and not premium:
        rows.append([InlineKeyboardButton(text="💎 Premium — подписка", callback_data="pay:premium")])
    if premium:
        mode_label = "по внешности" if premium_mode == "appearance" else "по общению"
        rows.append(
            [InlineKeyboardButton(text=f"💎 Подбор {mode_label} — сменить", callback_data="premium:mode")]
        )
    rows.append(
        [
            InlineKeyboardButton(text="🏙 Город", callback_data="profile:city"),
            InlineKeyboardButton(text="👀 Кого ищем", callback_data="profile:seek"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="🎁 Позвать друга", callback_data="menu:invite"),
            InlineKeyboardButton(text="🎟 Промокод", callback_data="menu:promo"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="❓ Помощь", callback_data="menu:help"),
            InlineKeyboardButton(text="⚙️ Данные", callback_data="menu:legal"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def legal_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📄 Условия", callback_data="legal:terms"),
                InlineKeyboardButton(text="🔒 Политика данных", callback_data="legal:privacy"),
            ],
            [InlineKeyboardButton(text="🗑 Удалить профиль и переписку", callback_data="account:delete_ask")],
            [InlineKeyboardButton(text="← Назад", callback_data="menu:profile")],
        ]
    )


def delete_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑 Да, удалить всё", callback_data="account:delete_confirm")],
            [InlineKeyboardButton(text="← Отмена", callback_data="menu:legal")],
        ]
    )

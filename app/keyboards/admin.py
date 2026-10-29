from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def admin_menu_kb(*, role: str, open_complaints: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(text="👤 Пользователь по ID", callback_data="adm:user_prompt")],
        [
            InlineKeyboardButton(
                text=f"🚨 Жалобы ({open_complaints})" if open_complaints else "🚨 Жалобы",
                callback_data="adm:complaints:0",
            )
        ],
    ]
    if role == "admin":
        rows.append([InlineKeyboardButton(text="💰 Платежи", callback_data="adm:payments")])
        rows.append([InlineKeyboardButton(text="🎟 Промокоды", callback_data="adm:promo")])
        rows.append([InlineKeyboardButton(text="📜 Журнал действий", callback_data="adm:audit:0")])
    rows.append([InlineKeyboardButton(text="♻️ Обслуживание", callback_data="adm:maintenance")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_card_kb(*, tg_id: int, banned: bool, is_admin_role: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="✅ Разблокировать" if banned else "🚫 Заблокировать",
                callback_data=f"adm:{'unban' if banned else 'ban'}:{tg_id}",
            )
        ],
        [
            InlineKeyboardButton(text="🔇 Мут 1 ч", callback_data=f"adm:mute:{tg_id}"),
            InlineKeyboardButton(text="💎 Premium +30", callback_data=f"adm:prem:{tg_id}"),
        ],
        [
            InlineKeyboardButton(text="🗂 Диалоги", callback_data=f"adm:dialogs:{tg_id}"),
            InlineKeyboardButton(text="🗑 Сброс рейтинга", callback_data=f"adm:reset:{tg_id}"),
        ],
        [InlineKeyboardButton(text="← В меню", callback_data="adm:menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def dialogs_kb(*, tg_id: int, dialog_ids: list[int], page: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"Диалог #{d}", callback_data=f"adm:dia:{d}:{tg_id}")] for d in dialog_ids
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="←", callback_data=f"adm:dialogs:{tg_id}:{page - 1}"))
    if len(dialog_ids) == 10:
        nav.append(InlineKeyboardButton(text="→", callback_data=f"adm:dialogs:{tg_id}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="← К профилю", callback_data=f"adm:user:{tg_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def complaint_card_kb(*, complaint_id: int, accused_tg: int | None) -> InlineKeyboardMarkup:
    rows = []
    if accused_tg:
        rows.append(
            [
                InlineKeyboardButton(text="🚫 Забанить", callback_data=f"adm:ban:{accused_tg}"),
                InlineKeyboardButton(text="👤 Профиль", callback_data=f"adm:user:{accused_tg}"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="✅ Отработана", callback_data=f"adm:cclose:{complaint_id}"),
            InlineKeyboardButton(text="✖️ Отклонить", callback_data=f"adm:cdismiss:{complaint_id}"),
        ]
    )
    rows.append([InlineKeyboardButton(text="← К жалобам", callback_data="adm:complaints:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def complaints_list_kb(
    *, items: list[tuple[int, str, str]], page: int, has_next: bool
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"#{cid} · {reason} · {when}", callback_data=f"adm:complaint:{cid}")]
        for cid, reason, when in items
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="←", callback_data=f"adm:complaints:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="→", callback_data=f"adm:complaints:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="← В меню", callback_data="adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← В меню", callback_data="adm:menu")]])


def promos_kb(rows: list[tuple[int, bool]]) -> InlineKeyboardMarkup:
    """One row per promo: tap to enable/disable it."""
    buttons = [
        [
            InlineKeyboardButton(
                text=f"{'🚫' if not active else '✅'} #{code_id}",
                callback_data=f"adm:promo_toggle:{code_id}",
            )
        ]
        for code_id, active in rows
    ]
    buttons.append([InlineKeyboardButton(text="➕ Создать код", callback_data="adm:promo_new")])
    buttons.append([InlineKeyboardButton(text="← В меню", callback_data="adm:menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

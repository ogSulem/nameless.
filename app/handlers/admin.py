from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.access import ROLE_TITLES, Role, role_for
from app.config import Settings
from app.database.models import ComplaintStatus
from app.flows.states import AdminPanel
from app.keyboards.admin import (
    admin_menu_kb,
    back_menu_kb,
    complaint_card_kb,
    complaints_list_kb,
    dialogs_kb,
    promos_kb,
    user_card_kb,
)
from app.services import admin as service
from app.telegram_safe import safe_answer_callback, safe_send_message
from app.ui import edit_ui

logger = logging.getLogger(__name__)
router = Router(name="admin")

NO_ACCESS = "Нет доступа"


# ----------------------------------------------------------------------- access
def _role(call: CallbackQuery, settings: Settings) -> Role | None:
    return role_for(settings, call.from_user.id if call.from_user else None)


async def _require(call: CallbackQuery, settings: Settings, minimum: Role) -> Role | None:
    role = _role(call, settings)
    if role is None or role < minimum:
        await safe_answer_callback(call, NO_ACCESS, show_alert=True)
        logger.warning(
            "admin_access_denied tg=%s need=%s got=%s",
            getattr(call.from_user, "id", None), minimum.name, getattr(role, "name", None),
        )
        return None
    return role


def _tail_int(data: str | None, index: int) -> int | None:
    parts = (data or "").split(":")
    if len(parts) <= index:
        return None
    try:
        return int(parts[index])
    except ValueError:
        return None


# ----------------------------------------------------------------------- render
async def _render(
    bot: Bot,
    redis: Redis,
    tg_id: int,
    text: str,
    kb=None,
    *,
    call: CallbackQuery | None = None,
) -> None:
    """Private chats get the single-message panel; group callbacks get a DM copy."""
    from_chat = getattr(getattr(call, "message", None), "chat", None)
    in_private = from_chat is not None and getattr(from_chat, "type", "") == "private"
    if call is None or in_private:
        await edit_ui(bot, redis, tg_id, text, kb=kb)
        return
    await safe_send_message(bot, tg_id, text, reply_markup=kb, parse_mode="HTML")


async def show_admin_menu(
    bot: Bot, redis: Redis, session: AsyncSession, settings: Settings, tg_id: int, *, call=None
) -> None:
    role = role_for(settings, tg_id)
    if role is None:
        await _render(bot, redis, tg_id, NO_ACCESS, None, call=call)
        return

    s = await service.stats(session)
    text = "\n".join(
        [
            "<b>🛠 Админ-панель Nameless</b>",
            f"Роль: <b>{ROLE_TITLES[role]}</b>",
            "",
            f"👥 Пользователи: <b>{s.users}</b> · 🟢 за 24ч: <b>{s.active_24h}</b>",
            f"💬 Диалоги сейчас: <b>{s.dialogs_active}</b> · сегодня: <b>{s.dialogs_today}</b>",
            f"🚨 Жалоб в очереди: <b>{s.complaints_open}</b>",
            "",
            "<i>Каждое действие модерации записывается в журнал.</i>",
        ]
    )
    await _render(
        bot, redis, tg_id, text,
        admin_menu_kb(role=role.name, open_complaints=s.complaints_open),
        call=call,
    )


@router.callback_query(F.data == "adm:menu")
async def adm_menu(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    await show_admin_menu(call.bot, redis, session, settings, int(call.from_user.id), call=call)
    await safe_answer_callback(call)


@router.callback_query(F.data == "adm:stats")
async def adm_stats(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if await _require(call, settings, Role.support) is None:
        return
    s = await service.stats(session)
    await _render(call.bot, redis, int(call.from_user.id), service.stats_text(s, settings), back_menu_kb(), call=call)
    await safe_answer_callback(call)


# ------------------------------------------------------------------------- user
@router.callback_query(F.data == "adm:user_prompt")
async def adm_user_prompt(
    call: CallbackQuery, state: FSMContext, redis: Redis, settings: Settings
) -> None:
    if await _require(call, settings, Role.support) is None:
        return
    await state.set_state(AdminPanel.waiting_user_id)
    await _render(
        call.bot, redis, int(call.from_user.id),
        "<b>👤 Поиск пользователя</b>\n\nОтправьте Telegram ID или внутренний DB id.",
        back_menu_kb(),
        call=call,
    )
    await safe_answer_callback(call)


@router.message(AdminPanel.waiting_user_id)
async def adm_user_lookup(
    message: Message, state: FSMContext, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    if message.from_user is None:
        return
    if role_for(settings, message.from_user.id) is None:
        await state.clear()
        return

    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    if not raw.lstrip("-").isdigit():
        await edit_ui(message.bot, redis, int(message.from_user.id), "Нужен числовой ID.", kb=back_menu_kb())
        return

    await state.clear()
    await _show_user_card(
        message.bot, redis, session, settings, int(message.from_user.id), int(raw), call=None
    )


async def _show_user_card(
    bot: Bot,
    redis: Redis,
    session: AsyncSession,
    settings: Settings,
    admin_tg: int,
    target: int,
    *,
    call: CallbackQuery | None = None,
) -> None:
    user = await service.find_user(session, target)
    if user is None:
        await _render(
            bot, redis, admin_tg, f"Пользователь <code>{target}</code> не найден.", back_menu_kb(), call=call
        )
        return

    seek = await _seek_label(redis, int(user.telegram_id))
    card = await service.user_card(session, user, settings, intent=seek)
    kb = user_card_kb(tg_id=int(user.telegram_id), banned=bool(user.is_banned), is_admin_role="")
    await _render(bot, redis, admin_tg, card.text, kb, call=call)


async def _seek_label(redis: Redis, tg_id: int) -> str:
    """"Whom is this user looking for" lives in Redis, written at registration."""
    try:
        raw = await redis.get(f"seek:display:{tg_id}")
    except Exception:
        return ""
    if raw is None:
        return ""
    value = raw.decode() if isinstance(raw, bytes) else str(raw)
    return {"male": "парней", "female": "девушек", "any": "всех"}.get(value, value)


@router.callback_query(F.data.startswith("adm:user:"))
async def adm_user_card(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if await _require(call, settings, Role.support) is None:
        return
    target = _tail_int(call.data, 2)
    if target is None:
        await safe_answer_callback(call)
        return
    await _show_user_card(
        call.bot, redis, session, settings, int(call.from_user.id), target, call=call
    )
    await safe_answer_callback(call)


# ---------------------------------------------------------------------- actions
async def _target_user(session: AsyncSession, call: CallbackQuery):
    target = _tail_int(call.data, 2)
    if target is None:
        await safe_answer_callback(call, "Некорректный ID")
        return None
    user = await service.find_user(session, target)
    if user is None:
        await safe_answer_callback(call, "Пользователь не найден")
        return None
    return user


async def _notify_user(bot: Bot, tg_id: int, text: str) -> None:
    try:
        await safe_send_message(bot, tg_id, text)
    except Exception:
        pass


@router.callback_query(F.data.startswith("adm:ban:"))
async def adm_ban(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if await _require(call, settings, Role.moderator) is None:
        return
    user = await _target_user(session, call)
    if user is None:
        return
    result = await service.set_ban(
        session, user, admin_tg=int(call.from_user.id), banned=True, reason="нарушение правил"
    )
    await _notify_user(
        call.bot, int(user.telegram_id),
        "🚫 Аккаунт заблокирован модерацией.\n"
        "Политика: /privacy · Удалить данные: /delete_me",
    )
    await safe_answer_callback(call, result.text, show_alert=True)
    await _show_user_card(
        call.bot, redis, session, settings, int(call.from_user.id), int(user.telegram_id), call=call
    )


@router.callback_query(F.data.startswith("adm:unban:"))
async def adm_unban(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if await _require(call, settings, Role.moderator) is None:
        return
    user = await _target_user(session, call)
    if user is None:
        return
    result = await service.set_ban(session, user, admin_tg=int(call.from_user.id), banned=False)
    await _notify_user(call.bot, int(user.telegram_id), "✅ Блокировка снята. Можно снова искать собеседников.")
    await safe_answer_callback(call, result.text, show_alert=True)
    await _show_user_card(
        call.bot, redis, session, settings, int(call.from_user.id), int(user.telegram_id), call=call
    )


@router.callback_query(F.data.startswith("adm:mute:"))
async def adm_mute(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if await _require(call, settings, Role.moderator) is None:
        return
    user = await _target_user(session, call)
    if user is None:
        return
    result = await service.set_mute(session, user, admin_tg=int(call.from_user.id), hours=1)
    await safe_answer_callback(call, result.text, show_alert=True)
    await _show_user_card(
        call.bot, redis, session, settings, int(call.from_user.id), int(user.telegram_id), call=call
    )


@router.callback_query(F.data.startswith("adm:prem:"))
async def adm_premium(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if await _require(call, settings, Role.admin) is None:
        return
    user = await _target_user(session, call)
    if user is None:
        return
    result = await service.grant_premium(session, user, admin_tg=int(call.from_user.id), days=30)
    await _notify_user(call.bot, int(user.telegram_id), "💎 Вам активирован Premium на 30 дней.")
    await safe_answer_callback(call, result.text, show_alert=True)
    await _show_user_card(
        call.bot, redis, session, settings, int(call.from_user.id), int(user.telegram_id), call=call
    )


@router.callback_query(F.data.startswith("adm:reset:"))
async def adm_reset_rating(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if await _require(call, settings, Role.admin) is None:
        return
    user = await _target_user(session, call)
    if user is None:
        return
    result = await service.reset_rating(session, user, admin_tg=int(call.from_user.id))
    await safe_answer_callback(call, result.text, show_alert=True)
    await _show_user_card(
        call.bot, redis, session, settings, int(call.from_user.id), int(user.telegram_id), call=call
    )


# ---------------------------------------------------------------------- dialogs
@router.callback_query(F.data.startswith("adm:dialogs:"))
async def adm_dialogs(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if await _require(call, settings, Role.support) is None:
        return
    target = _tail_int(call.data, 2)
    user = await service.find_user(session, target or 0)
    if user is None:
        await safe_answer_callback(call, "Не найден")
        return
    page = max(0, _tail_int(call.data, 3) or 0)
    dialogs = await service.user_dialogs(session, user, limit=10, page=page)
    ids = [int(d.id) for d in dialogs]
    text = (
        f"<b>🗂 Диалоги пользователя</b> <code>{user.telegram_id}</code>\n"
        f"Страница {page + 1} · показано {len(ids)}\n\n"
        "<i>Открывайте только при разборе жалобы: просмотр фиксируется в журнале.</i>"
    )
    kb = dialogs_kb(tg_id=int(user.telegram_id), dialog_ids=ids, page=page)
    await _render(call.bot, redis, int(call.from_user.id), text, kb, call=call)
    await safe_answer_callback(call)


@router.callback_query(F.data.startswith("adm:dia:"))
async def adm_dialog_view(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    role = await _require(call, settings, Role.moderator)
    if role is None:
        return
    parts = (call.data or "").split(":")
    if len(parts) < 4 or not parts[2].isdigit():
        await safe_answer_callback(call)
        return
    dialog_id = int(parts[2])
    back_tg = int(parts[3]) if parts[3].lstrip("-").isdigit() else 0

    lines = await service.dialog_messages(session, dialog_id, limit=40)
    body = "\n".join(lines) if lines else "<i>Сообщений нет (удалены по сроку хранения).</i>"
    text = f"<b>Диалог #{dialog_id}</b>\n\n{body[:3500]}"
    kb = back_menu_kb()
    if back_tg:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="← К профилю", callback_data=f"adm:user:{back_tg}")],
                [InlineKeyboardButton(text="← В меню", callback_data="adm:menu")],
            ]
        )
    await _render(call.bot, redis, int(call.from_user.id), text, kb, call=call)
    await service.log_action(
        session, admin_tg=int(call.from_user.id), action="view_dialog", details=f"dialog #{dialog_id}"
    )
    await safe_answer_callback(call)


# ------------------------------------------------------------------- complaints
@router.callback_query(F.data.startswith("adm:complaints:"))
async def adm_complaints(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if await _require(call, settings, Role.support) is None:
        return
    page = _tail_int(call.data, 2) or 0
    page = max(0, min(page, 200))
    rows = await service.list_complaints(session, page=page, per_page=5)

    if not rows:
        await _render(
            call.bot, redis, int(call.from_user.id),
            "<b>🚨 Жалобы</b>\n\nОчередь пуста 🎉",
            back_menu_kb(),
            call=call,
        )
        await safe_answer_callback(call)
        return

    items = [
        (r.id, escape(r.reason[:24] or "без описания"), f"{r.created_at:%d.%m %H:%M}") for r in rows
    ]
    text = f"<b>🚨 Очередь жалоб</b> · стр. {page + 1}\n\nНажмите, чтобы открыть материал."
    await _render(call.bot, redis, int(call.from_user.id), text,
                  complaints_list_kb(items=items, page=page, has_next=len(rows) == 5), call=call)
    await safe_answer_callback(call)


@router.callback_query(F.data.startswith("adm:complaint:"))
async def adm_complaint_view(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if await _require(call, settings, Role.support) is None:
        return
    complaint_id = _tail_int(call.data, 2)
    detail = await service.complaint_detail(session, complaint_id or 0)
    if detail is None:
        await safe_answer_callback(call, "Жалоба не найдена")
        return

    lines = [f"<b>🚨 Жалоба #{detail.complaint.id}</b>", f"Причина: {escape(detail.complaint.reason)}", ""]
    if detail.accused_tg:
        lines.append(f"На кого: <code>{detail.accused_tg}</code>")
    lines.append(f"От: <code>{detail.reporter_tg}</code>")
    lines.append(f"Диалог: #{detail.dialog_id}")
    lines.append("")
    if detail.messages:
        lines.append("<b>Последние сообщения:</b>")
        for author, text, created in detail.messages[-10:]:
            lines.append(f"<i>{created:%H:%M}</i> {author}: {text}")
    else:
        lines.append("<i>Сообщения уже удалены по сроку хранения (30 дней).</i>")
    if detail.photos:
        lines.append("")
        lines.append(f"📸 Фото в диалоге: {len(detail.photos)}")

    await _render(
        call.bot, redis, int(call.from_user.id), "\n".join(lines)[:3900],
        complaint_card_kb(complaint_id=detail.complaint.id, accused_tg=detail.accused_tg), call=call,
    )
    await service.log_action(
        session, admin_tg=int(call.from_user.id), action="view_complaint", details=f"#{detail.complaint.id}"
    )
    await safe_answer_callback(call)


async def _close(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings,
                 status: ComplaintStatus) -> None:
    if await _require(call, settings, Role.moderator) is None:
        return
    complaint_id = _tail_int(call.data, 2) or 0
    result = await service.close_complaint(
        session, complaint_id, admin_tg=int(call.from_user.id), status=status
    )
    await safe_answer_callback(call, result.text, show_alert=True)
    rows = await service.list_complaints(session, page=0, per_page=5)
    if rows:
        items = [(r.id, escape(r.reason[:24] or "без описания"), f"{r.created_at:%d.%m %H:%M}") for r in rows]
        await _render(call.bot, redis, int(call.from_user.id), "<b>🚨 Очередь жалоб</b>",
                      complaints_list_kb(items=items, page=0, has_next=len(rows) == 5), call=call)
    else:
        await _render(call.bot, redis, int(call.from_user.id), "<b>🚨 Жалобы</b>\n\nОчередь пуста 🎉",
                      back_menu_kb(), call=call)


@router.callback_query(F.data.startswith("adm:cclose:"))
async def adm_complaint_close(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    await _close(call, session, redis, settings, ComplaintStatus.resolved)


@router.callback_query(F.data.startswith("adm:cdismiss:"))
async def adm_complaint_dismiss(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    await _close(call, session, redis, settings, ComplaintStatus.dismissed)


# --------------------------------------------------------------------- money/logs
@router.callback_query(F.data == "adm:payments")
async def adm_payments(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if await _require(call, settings, Role.admin) is None:
        return
    payments = await service.recent_payments(session, limit=10)
    if not payments:
        text = "<b>💰 Платежи</b>\n\nЗаписей нет."
    else:
        lines = ["<b>💰 Последние платежи</b>", ""]
        for p in payments:
            mark = {"paid": "✅", "pending": "⏳", "canceled": "✖️"}.get(p.status, "❔")
            lines.append(
                f"{mark} {p.provider} {float(p.amount) / 100:.0f} ₽ · "
                f"<code>{p.telegram_id}</code> · {p.created_at:%d.%m %H:%M}"
            )
        text = "\n".join(lines)
    await _render(call.bot, redis, int(call.from_user.id), text, back_menu_kb(), call=call)
    await safe_answer_callback(call)


@router.callback_query(F.data.startswith("adm:audit:"))
async def adm_audit(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if await _require(call, settings, Role.admin) is None:
        return
    rows = await service.recent_audit(session, limit=15)
    if not rows:
        text = "<b>📜 Журнал</b>\n\nПусто."
    else:
        lines = ["<b>📜 Журнал действий</b>", ""]
        for row in rows:
            target = f" → <code>{row.target_tg}</code>" if row.target_tg else ""
            lines.append(f"<i>{row.created_at:%d.%m %H:%M}</i> <code>{row.admin_tg}</code> {row.action}{target}")
        text = "\n".join(lines)
    await _render(call.bot, redis, int(call.from_user.id), text[:3900], back_menu_kb(), call=call)
    await safe_answer_callback(call)


@router.callback_query(F.data == "adm:maintenance")
async def adm_maintenance(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if await _require(call, settings, Role.admin) is None:
        return
    report = await service.run_maintenance(session, settings)
    await service.log_action(
        session, admin_tg=int(call.from_user.id), action="maintenance",
        details=f"msgs={report.deleted_messages} photos={report.deleted_photos}",
    )
    text = (
        "<b>♻️ Обслуживание выполнено</b>\n\n"
        f"Удалено сообщений: {report.deleted_messages}\n"
        f"Удалено фото: {report.deleted_photos}\n"
        f"Удалено жалоб: {report.deleted_complaints}\n"
        f"Удалено записей журнала: {report.deleted_audit}\n\n"
        "<i>Сроки хранения: см. /privacy</i>"
    )
    await _render(call.bot, redis, int(call.from_user.id), text, back_menu_kb(), call=call)
    await safe_answer_callback(call, "Готово")


# ------------------------------------------------------------------ promo codes
def _promo_text(rows) -> str:
    if not rows:
        return (
            "<b>🎟 Промокоды</b>\n\n"
            "Кодов нет.\n\n"
            "<i>Нажмите «➕ Создать код», чтобы выдать бесплатный Premium.</i>"
        )
    lines = ["<b>🎟 Промокоды</b>", ""]
    for r in rows:
        state = "✅" if r.is_active else "🚫"
        until = f"до {r.expires_at:%d.%m.%Y}" if r.expires_at else "без срока"
        note = f" · {escape(r.note)}" if r.note else ""
        lines.append(
            f"{state} <code>{escape(r.code)}</code> · {r.days} дн · "
            f"{r.used_count}/{r.max_uses} · {until}{note}"
        )
    lines += ["", "<i>Нажмите на код, чтобы включить или отключить его.</i>"]
    return "\n".join(lines)


async def _render_promos(bot: Bot, redis: Redis, session: AsyncSession, tg_id: int, *, call=None) -> None:
    from app.services import promo as promo_service

    rows = await promo_service.list_codes(session, limit=10)
    await _render(
        bot,
        redis,
        tg_id,
        _promo_text(rows),
        promos_kb([(r.id, r.is_active) for r in rows]),
        call=call,
    )


@router.callback_query(F.data == "adm:promo")
async def adm_promo(
    call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    if await _require(call, settings, Role.admin) is None:
        return
    await _render_promos(call.bot, redis, session, int(call.from_user.id), call=call)
    await safe_answer_callback(call)


@router.callback_query(F.data == "adm:promo_new")
async def adm_promo_new(
    call: CallbackQuery, state: FSMContext, redis: Redis, settings: Settings
) -> None:
    if await _require(call, settings, Role.admin) is None:
        return
    await state.set_state(AdminPanel.waiting_promo)
    await edit_ui(
        call.bot,
        redis,
        int(call.from_user.id),
        "<b>🎟 Новый промокод</b>\n\n"
        "Пришлите: <code>код дней активаций срок_дней</code>\n\n"
        "Например: <code>START30 30 100 30</code> — код <b>START30</b>, "
        "30 дней Premium, 100 активаций, живёт 30 дней.\n\n"
        "Вместо кода можно написать <code>_</code> — сгенерирую случайный.\n"
        "Срок <code>0</code> — бессрочно.",
        kb=back_menu_kb(),
    )
    await safe_answer_callback(call)


def _parse_promo_input(raw: str) -> tuple[str, int, int, int | None]:
    from app.services.promo import generate_code

    parts = raw.split()
    if len(parts) < 3 or len(parts) > 4:
        raise ValueError("format")
    code = parts[0].strip()
    if code in {"_", "-", "auto"}:
        code = generate_code()
    days = int(parts[1])
    uses = int(parts[2])
    valid = int(parts[3]) if len(parts) == 4 else 30
    return code, days, uses, (None if valid == 0 else valid)


@router.message(AdminPanel.waiting_promo)
async def adm_promo_create(
    message: Message, state: FSMContext, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    if message.from_user is None:
        return
    if role_for(settings, int(message.from_user.id)) is None:
        await state.clear()
        return

    raw = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    tg_id = int(message.from_user.id)
    try:
        code, days, uses, valid = _parse_promo_input(raw)
    except (ValueError, TypeError):
        await edit_ui(
            message.bot,
            redis,
            tg_id,
            "Не разобрал. Формат: <code>КОД ДНЕЙ АКТИВАЦИЙ СРОК</code>, "
            "например <code>START30 30 100 30</code>.",
            kb=back_menu_kb(),
        )
        return

    from app.services import promo as promo_service

    promo, error = await promo_service.create_code(
        session,
        code=code,
        days=days,
        max_uses=uses,
        valid_days=valid,
        created_by=tg_id,
    )
    if promo is None:
        await edit_ui(message.bot, redis, tg_id, f"❌ {escape(error or 'Не получилось.')}", kb=back_menu_kb())
        return

    await service.log_action(
        session,
        admin_tg=tg_id,
        action="promo_create",
        details=f"{promo.code} days={promo.days} uses={promo.max_uses}",
    )
    await state.clear()
    await _render_promos(message.bot, redis, session, tg_id)


@router.callback_query(F.data.startswith("adm:promo_toggle:"))
async def adm_promo_toggle(
    call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    if await _require(call, settings, Role.admin) is None:
        return
    code_id = _tail_int(call.data, 2)
    if code_id is None:
        await safe_answer_callback(call, "Не понял, какой код.")
        return

    from app.services import promo as promo_service

    promo = await promo_service.toggle_active(session, code_id)
    if promo is None:
        await safe_answer_callback(call, "Код не найден")
        return
    await service.log_action(
        session,
        admin_tg=int(call.from_user.id),
        action="promo_enable" if promo.is_active else "promo_disable",
        details=promo.code,
    )
    await _render_promos(call.bot, redis, session, int(call.from_user.id), call=call)
    await safe_answer_callback(call, "Включён" if promo.is_active else "Отключён")

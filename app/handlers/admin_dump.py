from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Dialog, Message as DbMessage, User
from app.telegram_safe import safe_send_message
from app.utils.markdown import escape_markdown

logger = logging.getLogger(__name__)
router = Router(name="admin_dump")


def _split_text(text: str, limit: int = 3500) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in text.split("\n"):
        add_len = len(line) + 1
        if cur and cur_len + add_len > limit:
            parts.append("\n".join(cur))
            cur = [line]
            cur_len = len(line)
        else:
            cur.append(line)
            cur_len += add_len
    if cur:
        parts.append("\n".join(cur))
    return parts


async def _tg_label(bot, tg_id: int) -> str:
    try:
        chat = await bot.get_chat(tg_id)
        full_name = (getattr(chat, "full_name", None) or "").strip()
        username = (getattr(chat, "username", None) or "").strip()
        uname = f"@{username}" if username else ""
        name_part = f"{full_name} " if full_name else ""
        label = f"{name_part}{uname}".strip()
        label = escape_markdown(label)
        return f"{label} (id: `{tg_id}`, tg://user?id={tg_id})".strip()
    except Exception:
        return f"id: `{tg_id}` (tg://user?id={tg_id})"


async def _resolve_user(session: AsyncSession, arg: int) -> User | None:
    # Try as telegram_id
    res = await session.execute(select(User).where(User.telegram_id == arg))
    user = res.scalar_one_or_none()
    if user is not None:
        return user

    # Try as db user_id (small numbers)
    if arg < 10_000_000:
        res2 = await session.execute(select(User).where(User.id == arg))
        return res2.scalar_one_or_none()

    return None


def _premium_status(until: datetime | None) -> tuple[bool, str]:
    if until is None:
        return (False, "нет")
    u = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
    now = datetime.now(tz=timezone.utc)
    if u <= now:
        return (False, f"истекла {u.strftime('%Y-%m-%d %H:%M')} UTC")
    return (True, f"до {u.strftime('%Y-%m-%d %H:%M')} UTC")


async def _dump_user_profile(bot, session: AsyncSession, user: User) -> str:
    prem_on, prem_txt = _premium_status(user.subscription_until)
    label = await _tg_label(bot, int(user.telegram_id))
    lines = [
        "*USER PROFILE*",
        f"DB id: `{user.id}`",
        f"TG: {label}",
        f"Город: `{user.city or '-'}`",
        f"Бан: `{bool(user.is_banned)}`  Review: `{bool(user.is_under_review)}`",
        f"Premium: `{prem_on}` ({prem_txt})",
        f"Season chat/app: `{float(user.season_rating_chat or 0.0):.2f}` / `{float(user.season_rating_appearance or 0.0):.2f}`",
        f"Last20 chat/app: `{float(user.last_20_avg_chat or 0.0):.2f}` / `{float(user.last_20_avg_appearance or 0.0):.2f}`",
        f"Created: `{user.created_at.strftime('%Y-%m-%d %H:%M:%S') if user.created_at else '-'}`",
    ]
    return "\n".join(lines)


async def _set_chat_rating(session: AsyncSession, user: User, value: float) -> None:
    user.season_rating_chat = float(value)
    user.rating_chat = float(value)
    user.last_20_avg_chat = float(value)
    try:
        await session.commit()
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        raise


async def _dump_dialog(bot, session: AsyncSession, dialog_id: int, limit: int = 50) -> str:
    res_d = await session.execute(select(Dialog).where(Dialog.id == dialog_id))
    dialog = res_d.scalar_one_or_none()
    if dialog is None:
        return f"Диалог `{dialog_id}` не найден"

    res_u = await session.execute(select(User).where(User.id.in_([dialog.user1_id, dialog.user2_id])))
    users = res_u.scalars().all()
    id_to_tg = {u.id: int(u.telegram_id) for u in users}

    u1_tg = id_to_tg.get(dialog.user1_id)
    u2_tg = id_to_tg.get(dialog.user2_id)

    u1_label = await _tg_label(bot, u1_tg) if u1_tg else "не найден"
    u2_label = await _tg_label(bot, u2_tg) if u2_tg else "не найден"

    header = [
        "*DIALOG DUMP*",
        f"Диалог: `{dialog_id}`",
        f"U1: {u1_label}",
        f"U2: {u2_label}",
        "",
        "*Сообщения:*",
    ]

    q = (
        select(DbMessage)
        .where(DbMessage.dialog_id == dialog_id)
        .order_by(DbMessage.created_at.asc())
    )
    res_m = await session.execute(q)
    msgs = res_m.scalars().all()
    if limit:
        msgs = msgs[-limit:]

    lines: list[str] = header
    for m in msgs:
        tg = id_to_tg.get(m.from_user_id)
        who = await _tg_label(bot, tg) if tg else f"user_id={m.from_user_id}"
        content = m.text if m.text else (f"📷 photo_id={m.photo_id}" if m.photo_id else "<empty>")
        content = escape_markdown(content)
        ts = m.created_at.strftime("%H:%M:%S") if m.created_at else "-"
        lines.append(f"- `{ts}` {who}: {content}")

    return "\n".join(lines)


async def _set_premium(session: AsyncSession, user: User, days: int | None) -> datetime | None:
    now = datetime.now(tz=timezone.utc)
    if days is None:
        user.subscription_until = None
        try:
            await session.commit()
        except Exception:
            try:
                await session.rollback()
            except Exception:
                pass
            raise
        return None

    base = user.subscription_until
    if base is None:
        base_dt = now
    else:
        base_dt = base if base.tzinfo else base.replace(tzinfo=timezone.utc)
        if base_dt < now:
            base_dt = now

    user.subscription_until = base_dt + timedelta(days=days)
    try:
        await session.commit()
    except Exception:
        try:
            await session.rollback()
        except Exception:
            pass
        raise
    return user.subscription_until


async def _premium_summary(session: AsyncSession) -> tuple[int, int]:
    now = datetime.now(tz=timezone.utc)
    res_a = await session.execute(select(User.id).where(User.subscription_until.is_not(None)).where(User.subscription_until > now))
    active = len(res_a.scalars().all())
    res_t = await session.execute(select(User.id).where(User.subscription_until.is_not(None)))
    total_with_until = len(res_t.scalars().all())
    return (active, total_with_until)


async def _dump_user(bot, session: AsyncSession, tg_id: int, limit: int = 100) -> str:
    res_u = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = res_u.scalar_one_or_none()
    if user is None:
        return f"Пользователь `{tg_id}` не найден"

    header = [
        "*USER DUMP*",
        f"Пользователь: {await _tg_label(bot, tg_id)}",
        "",
        "*Последние сообщения:*",
    ]

    q = (
        select(DbMessage)
        .where(DbMessage.from_user_id == user.id)
        .order_by(DbMessage.created_at.desc())
        .limit(limit)
    )
    res_m = await session.execute(q)
    msgs = res_m.scalars().all()

    lines: list[str] = header
    for m in reversed(msgs):
        content = m.text if m.text else (f"📷 photo_id={m.photo_id}" if m.photo_id else "<empty>")
        content = escape_markdown(content)
        ts = m.created_at.strftime("%Y-%m-%d %H:%M:%S") if m.created_at else "-"
        lines.append(f"- `{ts}` dialog=`{m.dialog_id}`: {content}")

    return "\n".join(lines)


async def _run_broadcast(bot, session: AsyncSession, text: str, admin_chat_id: int) -> None:
    res = await session.execute(select(User.telegram_id))
    user_ids = res.scalars().all()
    
    count = 0
    blocked = 0
    failed = 0
    
    for uid in user_ids:
        try:
            await safe_send_message(bot, int(uid), text)
            count += 1
            # Sleep a bit to avoid flood limits
            await asyncio.sleep(0.05) 
        except Exception as e:
            if "forbidden" in str(e).lower():
                blocked += 1
            else:
                failed += 1
                logger.error("broadcast_failed tg_id=%s error=%s", uid, e)
                
    report = (
        "*BROADCAST FINISHED*\n"
        f"✅ Успешно: `{count}`\n"
        f"🚫 Блокировок: `{blocked}`\n"
        f"❌ Ошибок: `{failed}`"
    )
    await safe_send_message(bot, admin_chat_id, report, parse_mode="Markdown")


async def _admin_dump_handle(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not settings.alerts_chat_id:
        raise SkipHandler

    if message.chat is None or int(message.chat.id) != int(settings.alerts_chat_id):
        raise SkipHandler

    logger.info(
        "admin_dump_incoming chat_id=%s has_from_user=%s text=%r",
        getattr(message.chat, "id", None),
        message.from_user is not None,
        message.text,
    )

    # In channels messages can be sent "as channel" => from_user may be None.
    # If from_user exists, we enforce ADMINS check. If not, we allow inside alerts channel.
    if message.from_user is not None:
        if settings.admins_set and message.from_user.id not in settings.admins_set:
            raise SkipHandler

    text = (message.text or "").strip()
    if not text:
        raise SkipHandler

    parts = text.split()
    cmd = parts[0].lower() if parts else ""
    if cmd not in {"user", "dia", "premium", "help", "rate"}:
        raise SkipHandler

    if cmd == "help":
        help_text = (
            "*ADMIN COMMANDS*\n"
            "- `user <tg_id|db_id>` — профиль + последние сообщения\n"
            "- `dia <dialog_id>` — выгрузка диалога\n"
            "- `premium` — сводка premium\n"
            "- `premium <id> on <days>` — выдать/продлить premium\n"
            "- `premium <id> off` — снять premium\n"
        )
        await safe_send_message(message.bot, message.chat.id, help_text, parse_mode="Markdown")
        return

    if cmd == "rate":
        if len(parts) != 3:
            await safe_send_message(message.bot, message.chat.id, "Формат: `rate <id> <num>` (num: 0..10)", parse_mode="Markdown")
            return

        try:
            arg = int(parts[1])
        except Exception:
            await safe_send_message(message.bot, message.chat.id, "`id` должен быть числом", parse_mode="Markdown")
            return

        try:
            val = float(parts[2].replace(",", "."))
            if not (0.0 <= val <= 10.0):
                raise ValueError
        except Exception:
            await safe_send_message(message.bot, message.chat.id, "`num` должен быть числом 0..10", parse_mode="Markdown")
            return

        user = await _resolve_user(session, arg)
        if user is None:
            await safe_send_message(message.bot, message.chat.id, f"Пользователь `{arg}` не найден", parse_mode="Markdown")
            return

        await _set_chat_rating(session, user, val)
        prem_on, prem_txt = _premium_status(user.subscription_until)
        await safe_send_message(
            message.bot,
            message.chat.id,
            "\n".join(
                [
                    "*RATING UPDATED*",
                    f"DB=`{user.id}` tg=`{int(user.telegram_id)}`",
                    f"season_rating_chat=`{float(user.season_rating_chat or 0.0):.2f}`",
                    f"Premium: `{prem_on}` ({prem_txt})",
                ]
            ),
            parse_mode="Markdown",
        )
        return

    if cmd == "premium" and len(parts) == 1:
        active, total_with_until = await _premium_summary(session)
        out = "\n".join(
            [
                "*PREMIUM SUMMARY*",
                f"Active: `{active}`",
                f"With subscription until set: `{total_with_until}`",
            ]
        )
        await safe_send_message(message.bot, message.chat.id, out, parse_mode="Markdown")
        return

    if cmd == "premium":
        if len(parts) != 4 or parts[2].lower() not in {"on", "off"}:
            await safe_send_message(message.bot, message.chat.id, "Формат: `premium <id> on <days>` или `premium <id> off`", parse_mode="Markdown")
            return

        try:
            arg = int(parts[1])
        except Exception:
            await safe_send_message(message.bot, message.chat.id, "`id` должен быть числом", parse_mode="Markdown")
            return

        user = await _resolve_user(session, arg)
        if user is None:
            await safe_send_message(message.bot, message.chat.id, f"Пользователь `{arg}` не найден", parse_mode="Markdown")
            return

        mode = parts[2].lower()
        if mode == "off":
            until = await _set_premium(session, user, None)
            prem_on, prem_txt = _premium_status(until)
            await safe_send_message(
                message.bot,
                message.chat.id,
                f"Premium снят для DB=`{user.id}` tg=`{int(user.telegram_id)}` ({prem_on}, {prem_txt})",
                parse_mode="Markdown",
            )
            return

        try:
            days = int(parts[3])
            if days <= 0 or days > 3650:
                raise ValueError
        except Exception:
            await safe_send_message(message.bot, message.chat.id, "`days` должен быть числом 1..3650", parse_mode="Markdown")
            return

        until = await _set_premium(session, user, days)
        prem_on, prem_txt = _premium_status(until)
        await safe_send_message(
            message.bot,
            message.chat.id,
            f"Premium обновлён для DB=`{user.id}` tg=`{int(user.telegram_id)}` ({prem_on}, {prem_txt})",
            parse_mode="Markdown",
        )
        return

    if len(parts) != 2:
        raise SkipHandler

    try:
        arg = int(parts[1])
    except Exception:
        await safe_send_message(message.bot, message.chat.id, "Нужно число: `user <tg_id>` или `dia <dialog_id>`", parse_mode="Markdown")
        return

    try:
        if cmd == "user":
            user = await _resolve_user(session, arg)
            if user is None:
                out = f"Пользователь `{arg}` не найден"
            else:
                prof = await _dump_user_profile(message.bot, session, user)
                msgs = await _dump_user(message.bot, session, int(user.telegram_id))
                out = prof + "\n\n" + msgs
        else:
            out = await _dump_dialog(message.bot, session, arg)
    except Exception:
        logger.exception("admin_dump_failed cmd=%s arg=%s", cmd, arg)
        await safe_send_message(message.bot, message.chat.id, "Ошибка при выгрузке", parse_mode="Markdown")
        return

    for chunk in _split_text(out):
        await safe_send_message(message.bot, message.chat.id, chunk, parse_mode="Markdown")


@router.message()
async def admin_dump_commands_message(message: Message, session: AsyncSession, settings: Settings) -> None:
    await _admin_dump_handle(message, session, settings)


@router.channel_post()
async def admin_dump_commands_channel_post(message: Message, session: AsyncSession, settings: Settings) -> None:
    await _admin_dump_handle(message, session, settings)

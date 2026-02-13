from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from redis.asyncio import Redis

from app.telegram_safe import (
    safe_delete_message,
    safe_edit_message_text,
    safe_send_message,
)

logger = logging.getLogger(__name__)


_CTRL_PLACEHOLDER = "ㅤ"


def ui_message_key(tg_id: int) -> str:
    return f"ui:message_id:{tg_id}"


def dialog_controls_message_key(tg_id: int) -> str:
    return f"ui:dialog_controls_message_id:{tg_id}"


def reply_mode_key(tg_id: int) -> str:
    return f"ui:reply_mode:{tg_id}"


async def get_ui_message_id(redis: Redis, tg_id: int) -> int | None:
    v = await redis.get(ui_message_key(tg_id))
    return int(v) if v else None


async def set_ui_message_id(redis: Redis, tg_id: int, message_id: int) -> None:
    await redis.set(ui_message_key(tg_id), str(message_id), ex=60 * 60 * 24 * 30)


async def get_dialog_controls_message_id(redis: Redis, tg_id: int) -> int | None:
    v = await redis.get(dialog_controls_message_key(tg_id))
    return int(v) if v else None


async def set_dialog_controls_message_id(redis: Redis, tg_id: int, message_id: int) -> None:
    await redis.set(dialog_controls_message_key(tg_id), str(message_id), ex=60 * 60 * 24 * 30)


async def get_reply_mode(redis: Redis, tg_id: int) -> str:
    v = await redis.get(reply_mode_key(tg_id))
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="ignore")
    return str(v)


async def set_reply_mode(redis: Redis, tg_id: int, mode: str) -> None:
    await redis.set(reply_mode_key(tg_id), mode, ex=60 * 60 * 24 * 30)


async def clear_dialog_controls(bot: Bot, redis: Redis, tg_id: int) -> None:
    mid = await get_dialog_controls_message_id(redis, tg_id)
    if mid:
        await safe_delete_message(bot, tg_id, mid)
    await redis.delete(dialog_controls_message_key(tg_id))
    await redis.delete(reply_mode_key(tg_id))


async def set_persistent_reply_keyboard(bot: Bot, redis: Redis, tg_id: int, kb: ReplyKeyboardMarkup) -> None:
    current_mode = await get_reply_mode(redis, tg_id)
    if current_mode == "start":
        return

    prev_mid = await get_dialog_controls_message_id(redis, tg_id)
    if prev_mid:
        await safe_delete_message(bot, tg_id, prev_mid)

    msg = await safe_send_message(bot, tg_id, _CTRL_PLACEHOLDER, reply_markup=kb)
    await set_dialog_controls_message_id(redis, tg_id, msg.message_id)
    await set_reply_mode(redis, tg_id, "start")


async def ensure_reply_keyboard_removed(bot: Bot, redis: Redis, tg_id: int) -> None:
    current_mode = await get_reply_mode(redis, tg_id)
    if current_mode == "none":
        return

    prev_mid = await get_dialog_controls_message_id(redis, tg_id)
    if prev_mid:
        await safe_delete_message(bot, tg_id, prev_mid)

    msg = await safe_send_message(bot, tg_id, _CTRL_PLACEHOLDER, reply_markup=ReplyKeyboardRemove())
    await set_dialog_controls_message_id(redis, tg_id, msg.message_id)
    await set_reply_mode(redis, tg_id, "none")


async def edit_ui(
    bot: Bot,
    redis: Redis,
    tg_id: int,
    text: str,
    kb: InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None = None,
) -> None:
    mid = await get_ui_message_id(redis, tg_id)
    if not mid:
        msg = await safe_send_message(bot, tg_id, text, reply_markup=kb)
        await set_ui_message_id(redis, tg_id, msg.message_id)
        return

    ok = await safe_edit_message_text(bot, tg_id, mid, text, reply_markup=kb)
    if ok:
        return

    msg = await safe_send_message(bot, tg_id, text, reply_markup=kb)
    await set_ui_message_id(redis, tg_id, msg.message_id)


async def send_new_ui(
    bot: Bot,
    redis: Redis,
    tg_id: int,
    text: str,
    kb: InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None = None,
) -> Message:
    msg = await safe_send_message(bot, tg_id, text, reply_markup=kb)
    await set_ui_message_id(redis, tg_id, msg.message_id)
    return msg


async def replace_ui_message(
    bot: Bot,
    redis: Redis,
    tg_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None = None,
) -> Message:
    mid = await get_ui_message_id(redis, tg_id)
    if mid:
        await safe_delete_message(bot, tg_id, mid)
    msg = await safe_send_message(bot, tg_id, text, reply_markup=reply_markup)
    await set_ui_message_id(redis, tg_id, msg.message_id)
    return msg

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from redis.asyncio import Redis

from app.redis import keys
from app.telegram_safe import safe_delete_message, safe_edit_message_text, safe_send_message

logger = logging.getLogger(__name__)

UI_TTL_S = 60 * 60 * 24 * 30
PARSE_MODE = "HTML"


async def get_ui_message_id(redis: Redis, tg_id: int) -> int | None:
    return await _get_int(redis, keys.ui_message_id(int(tg_id)))


async def set_ui_message_id(redis: Redis, tg_id: int, message_id: int) -> None:
    try:
        await redis.set(keys.ui_message_id(int(tg_id)), str(int(message_id)), ex=UI_TTL_S)
    except Exception:
        pass


async def clear_ui_message_id(redis: Redis, tg_id: int) -> None:
    try:
        await redis.delete(keys.ui_message_id(int(tg_id)))
    except Exception:
        pass


async def edit_ui(
    bot: Bot,
    redis: Redis,
    tg_id: int,
    text: str,
    kb: InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None = None,
) -> Message | None:
    """Edit the single bot message that carries the current screen."""
    mid = await get_ui_message_id(redis, tg_id)
    if mid:
        ok = await safe_edit_message_text(bot, tg_id, mid, text, reply_markup=kb, parse_mode=PARSE_MODE)
        if ok:
            return None
        # The message is gone (deleted, too old, media): fall through to a new one.
    return await send_new_ui(bot, redis, tg_id, text, kb=kb, delete_previous=False)


async def send_new_ui(
    bot: Bot,
    redis: Redis,
    tg_id: int,
    text: str,
    kb: InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None = None,
    delete_previous: bool = True,
) -> Message | None:
    """Send a fresh screen message and keep the chat clean.

    The product promise is "the chat only contains what matters", so the previous
    screen message is deleted instead of piling up.
    """
    if delete_previous:
        prev = await get_ui_message_id(redis, tg_id)
        if prev:
            await safe_delete_message(bot, tg_id, prev)

    msg = await safe_send_message(bot, tg_id, text, reply_markup=kb, parse_mode=PARSE_MODE)
    if msg is not None:
        await set_ui_message_id(redis, tg_id, msg.message_id)
    return msg


async def cleanup_screen(bot: Bot, redis: Redis, tg_id: int, *, keyboard_too: bool = False) -> None:
    prev = await get_ui_message_id(redis, tg_id)
    if prev and keyboard_too:
        await safe_delete_message(bot, tg_id, prev)
    await clear_ui_message_id(redis, tg_id)


async def _get_int(redis: Redis, key: str) -> int | None:
    try:
        raw = await redis.get(key)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return int(raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        return None

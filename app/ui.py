from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.types import (
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from redis.asyncio import Redis

from app.redis import keys
from app.telegram_safe import safe_delete_message, safe_edit_message_text, safe_send_message

logger = logging.getLogger(__name__)

UI_TTL_S = 60 * 60 * 24 * 30
PARSE_MODE = "HTML"
NOTICE_TTL_S = 8

# Fire-and-forget helpers. The event loop keeps only a weak reference to a bare
# task, so a sleeping cleanup could in theory be garbage-collected mid-flight.
_BG_TASKS: set[asyncio.Task] = set()


def background(coro) -> None:
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def notice(
    bot: Bot,
    redis: Redis,
    tg_id: int,
    text: str,
    *,
    ttl_s: float = NOTICE_TTL_S,
    throttle_s: int = 20,
) -> None:
    """A transient line that deletes itself, so the main screen stays untouched."""
    try:
        fresh = await redis.set(keys.throttle(f"notice:{int(tg_id)}"), "1", nx=True, ex=int(throttle_s))
    except Exception:
        fresh = True
    if not fresh:
        return
    try:
        msg = await safe_send_message(bot, tg_id, text)
    except Exception:
        logger.info("notice_failed tg=%s", tg_id)
        return
    if msg is not None:
        background(_delete_later(bot, tg_id, msg.message_id, ttl_s))


async def _delete_later(bot: Bot, tg_id: int, message_id: int, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        await safe_delete_message(bot, tg_id, message_id)
    except Exception:
        logger.debug("notice_cleanup_failed tg=%s", tg_id)


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
    """Edit the single bot message that carries the current screen.

    `kb=None` means "no buttons": Telegram keeps the previous inline keyboard when
    reply_markup is omitted, which left dead step-1 buttons under the next screen.
    """
    mid = await get_ui_message_id(redis, tg_id)
    if mid:
        markup = kb if kb is not None else InlineKeyboardMarkup(inline_keyboard=[])
        ok = await safe_edit_message_text(bot, tg_id, mid, text, reply_markup=markup, parse_mode=PARSE_MODE)
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

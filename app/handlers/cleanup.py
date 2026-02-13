from __future__ import annotations

import logging

from aiogram import Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.types import Message
from redis.asyncio import Redis

from app.redis import keys

logger = logging.getLogger(__name__)
router = Router(name="cleanup")


@router.callback_query()
async def cleanup_callbacks_during_search(call: CallbackQuery, redis: Redis, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is not None:
        raise SkipHandler

    if await redis.get(f"pending_rating:{call.from_user.id}"):
        raise SkipHandler

    if await redis.get(keys.active_dialog(call.from_user.id)):
        raise SkipHandler

    searching = await redis.get(f"ui:search_message_id:{call.from_user.id}")
    if searching and call.data != "cancel_search":
        await call.answer()
        return

    raise SkipHandler


@router.message()
async def cleanup_unexpected_messages(message: Message, redis: Redis, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is not None:
        return

    if message.from_user is None:
        raise SkipHandler

    dialog = await redis.get(keys.active_dialog(message.from_user.id))
    if dialog:
        return

    pending_rating = await redis.get(f"pending_rating:{message.from_user.id}")
    if pending_rating:
        return

    searching = await redis.get(f"ui:search_message_id:{message.from_user.id}")
    if searching:
        if message.text and message.text.startswith("/"):
            try:
                await message.delete()
            except Exception:
                pass
            return
        try:
            await message.delete()
        except Exception:
            pass
        return

    if message.text and message.text.startswith("/"):
        raise SkipHandler

    try:
        await message.delete()
    except Exception:
        pass

    return

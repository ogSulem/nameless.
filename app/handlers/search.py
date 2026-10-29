from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.flows.profile import show_profile
from app.handlers.commands import get_user
from app.keyboards.search import searching_kb
from app.redis import keys
from app.services.dialog import DialogService
from app.services.matchmaking import MatchmakingService, seek_gender_for
from app.services.notifications import announce_match
from app.telegram_safe import safe_answer_callback
from app.ui import edit_ui, get_ui_message_id
from app.utils.ratelimit import rate_limit

logger = logging.getLogger(__name__)
router = Router(name="search")

SEARCHING_TEXT = (
    "🔎 <b>Ищу собеседника…</b>\n\n"
    "Обычно это занимает несколько секунд. Если в очереди сейчас никого нет — "
    "оставьте поиск включённым: как только появится подходящий человек, я сразу "
    "пришлю карточку.\n\n"
    "<i>Можно закрыть чат — поиск продолжится.</i>\n\n"
    "🎁 Позвать друга: /invite"
)

EMPTY_QUEUE_TEXT = (
    "🔎 <b>Ищу собеседника…</b>\n\n"
    "Сейчас в очереди никого нет — вы первый. Поиск остаётся включённым: как только "
    "кто-то появится, я сразу пришлю карточку.\n\n"
    "Так сервис растёт быстрее всего вдвоём: <b>🎁 /invite</b> — за друга, который "
    "зарегистрируется, вы получаете дни Premium.\n\n"
    "<i>Можно закрыть чат — поиск продолжится.</i>"
)


async def start_search_for(
    bot: Bot,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    tg_id: int,
) -> bool:
    """Run one matching attempt. Returns True when a partner was found."""
    user = await get_user(session, tg_id)
    if user is None:
        await edit_ui(bot, redis, tg_id, "Профиль не найден. Нажмите /start.")
        return False
    if user.is_banned:
        await edit_ui(bot, redis, tg_id, "🚫 Аккаунт заблокирован.")
        return False

    if await DialogService(redis, settings).active_dialog_id(session, tg_id):
        return False

    matchmaking = MatchmakingService(redis, settings)
    premium = await matchmaking.is_premium(session, user)
    result = await matchmaking.try_match(session=session, user=user, premium=premium)

    if result is None:
        waiting = await matchmaking.queue_size(
            seek_gender_for(getattr(user.gender, "value", user.gender)), None
        )
        text = SEARCHING_TEXT if waiting > 1 else EMPTY_QUEUE_TEXT
        await edit_ui(bot, redis, tg_id, text, kb=searching_kb())
        mid = await get_ui_message_id(redis, tg_id)
        if mid:
            try:
                await redis.set(keys.ui_search_message_id(tg_id), str(mid), ex=60 * 60 * 24)
            except Exception:
                pass
        return False

    await announce_match(bot, redis, session, settings, result.dialog_id, tg_id, result.partner_telegram_id)
    return True


@router.callback_query(F.data == "search:start")
async def search_start(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    tg_id = int(call.from_user.id)
    if not await rate_limit(redis, key=keys.rate(f"search:{tg_id}"), ttl_s=2):
        await safe_answer_callback(call, "Секунду…")
        return

    await safe_answer_callback(call)
    await start_search_for(call.bot, session, redis, settings, tg_id)


@router.callback_query(F.data == "search:cancel")
async def search_cancel(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    tg_id = int(call.from_user.id)
    if not await rate_limit(redis, key=keys.rate(f"search:cancel:{tg_id}"), ttl_s=1):
        await safe_answer_callback(call)
        return

    await MatchmakingService(redis, settings).dequeue(tg_id)
    try:
        await redis.delete(keys.ui_search_message_id(tg_id))
    except Exception:
        pass

    await show_profile(call.bot, redis, session, settings, tg_id)
    await safe_answer_callback(call, "Поиск отменён")

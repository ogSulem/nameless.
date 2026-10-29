"""User-side promo code redemption.

Chat hygiene: the message with the code is deleted, the result replaces the
current screen — one message, always.
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import User
from app.flows.states import Promo
from app.redis import keys
from app.services import promo as promo_service
from app.services.matchmaking import MatchmakingService
from app.telegram_safe import safe_answer_callback
from app.texts import days_label
from app.ui import edit_ui

logger = logging.getLogger(__name__)
router = Router(name="promo")

PROMPT = (
    "🎟 <b>Промокод</b>\n\n"
    "Пришлите код одним сообщением — например <code>START30</code>.\n\n"
    "<i>Один код активируется один раз на аккаунт.</i>"
)

STATUS_TEXT = {
    promo_service.RedeemStatus.bad_format: (
        "Код может содержать только латиницу, цифры, дефис и подчёркивание "
        "(3–24 символа). Проверьте и пришлите ещё раз."
    ),
    promo_service.RedeemStatus.not_found: "Такого кода нет. Проверьте написание.",
    promo_service.RedeemStatus.inactive: "Этот код отключён.",
    promo_service.RedeemStatus.expired: "Срок действия этого кода истёк.",
    promo_service.RedeemStatus.exhausted: "Код уже использован максимальное число раз.",
    promo_service.RedeemStatus.already_used: "Вы уже использовали этот код.",
    promo_service.RedeemStatus.banned: "Код недоступен для этого аккаунта.",
    promo_service.RedeemStatus.not_registered: "Сначала завершите регистрацию — /start.",
}


async def _attempts(redis: Redis, settings: Settings, tg_id: int) -> int:
    """Brute-forcing 24-character codes is hopeless, but a cap costs us nothing."""
    key = keys.promo_attempt_key(tg_id)
    try:
        count = int(await redis.incr(key))
        if count == 1:
            await redis.expire(key, 3600)
        return count
    except Exception:
        logger.warning("promo_attempt_counter_failed tg=%s", tg_id, exc_info=True)
        return 0


async def _apply(
    bot: Bot,
    redis: Redis,
    session: AsyncSession,
    settings: Settings,
    tg_id: int,
    raw_code: str,
    *,
    from_message: Message | None = None,
) -> None:
    limit = int(settings.promo_attempts_per_hour)
    if limit > 0 and await _attempts(redis, settings, tg_id) > limit:
        await edit_ui(
            bot, redis, tg_id,
            "Слишком много попыток ввода промокода. Попробуйте через час.",
        )
        return

    result = await promo_service.redeem(session, telegram_id=tg_id, raw_code=raw_code)

    if result.ok:
        await MatchmakingService(redis, settings).invalidate_premium(tg_id)
        until = f"{result.until:%d.%m.%Y}" if result.until else "—"
        text = (
            "✅ <b>Промокод активирован</b>\n\n"
            f"Premium на <b>{days_label(result.days)}</b>.\n"
            f"Действует до <b>{until}</b>.\n\n"
            "<i>Подбор Premium настраивается в профиле.</i>"
        )
        logger.info("promo_ok tg=%s code=%s days=%s", tg_id, result.code, result.days)
    else:
        base = STATUS_TEXT.get(result.status, "Код не подошёл.")
        text = f"❌ <b>Код не активирован</b>\n\n{base}"
        if result.status in {
            promo_service.RedeemStatus.bad_format,
            promo_service.RedeemStatus.not_found,
        }:
            text += "\n\nПопробуйте ещё раз или нажмите «← В профиль»."

    if from_message is not None:
        try:
            await from_message.delete()
        except Exception:
            pass

    from app.keyboards.start import back_kb

    await edit_ui(bot, redis, tg_id, text, kb=back_kb("menu:profile", "← В профиль"))


@router.callback_query(F.data == "menu:promo")
async def menu_promo(
    call: CallbackQuery, state: FSMContext, redis: Redis, settings: Settings
) -> None:
    if call.from_user is None:
        return
    await state.set_state(Promo.waiting_code)
    await edit_ui(call.bot, redis, int(call.from_user.id), PROMPT)
    await safe_answer_callback(call)


@router.message(Promo.waiting_code)
async def promo_input(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> None:
    if message.from_user is None:
        return
    await state.clear()
    await _apply(
        message.bot,
        redis,
        session,
        settings,
        int(message.from_user.id),
        message.text or "",
        from_message=message,
    )


@router.message(Command("promo"))
async def cmd_promo(
    message: Message,
    command: CommandObject,
    state: FSMContext,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> None:
    if message.from_user is None:
        return
    tg_id = int(message.from_user.id)
    code = (command.args or "").strip()

    exists = (
        await session.execute(select(User.id).where(User.telegram_id == tg_id))
    ).scalar_one_or_none()
    if exists is None:
        await message.bot.send_message(tg_id, "Сначала завершите регистрацию — /start.")
        return

    if not code:
        await state.set_state(Promo.waiting_code)
        try:
            await message.delete()
        except Exception:
            pass
        await edit_ui(message.bot, redis, tg_id, PROMPT)
        return

    await state.clear()
    await _apply(message.bot, redis, session, settings, tg_id, code, from_message=message)

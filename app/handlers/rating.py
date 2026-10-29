from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Rating, RatingType
from app.flows.profile import show_profile
from app.flows.states import Complaint
from app.handlers.commands import get_user
from app.keyboards.rating import complaint_prompt_kb, rating_kb
from app.redis import keys
from app.services.complaint import ComplaintService
from app.services.rating import RatingService
from app.telegram_safe import safe_answer_callback
from app.ui import edit_ui
from app.utils.ratelimit import rate_limit

logger = logging.getLogger(__name__)
router = Router(name="rating")


# ------------------------------------------------------------------- pending
async def _pending(redis: Redis, tg_id: int) -> dict[str, str | None]:
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.get(keys.pending_rating(tg_id))
        pipe.get(keys.pending_rating_partner(tg_id))
        pipe.get(keys.pending_rating_step(tg_id))
        pipe.get(keys.pending_rating_has_photos(tg_id))
        pipe.get(keys.pending_rating_action(tg_id))
        values = await pipe.execute()
    except Exception:
        return {}

    def s(v):
        if v is None:
            return None
        return v.decode() if isinstance(v, bytes) else str(v)

    dialog_id, partner, step, has_photos, action = (s(v) for v in values)
    return {
        "dialog_id": dialog_id,
        "partner": partner,
        "step": step or "chat",
        "has_photos": has_photos or "0",
        "action": action or "end",
    }


async def _clear_pending(redis: Redis, tg_id: int) -> None:
    try:
        pipe = redis.pipeline(transaction=False)
        for key in (
            keys.pending_rating(tg_id),
            keys.pending_rating_partner(tg_id),
            keys.pending_rating_step(tg_id),
            keys.pending_rating_has_photos(tg_id),
            keys.pending_rating_action(tg_id),
            keys.ui_rating_message_id(tg_id),
        ):
            pipe.delete(key)
        await pipe.execute()
    except Exception:
        pass


# ------------------------------------------------------------------- saving
async def _save_rating(
    session: AsyncSession,
    settings: Settings,
    *,
    dialog_id: int,
    from_tg: int,
    to_tg: int,
    rating_type: RatingType,
    value: int,
) -> bool:
    from_user = await get_user(session, from_tg)
    to_user = await get_user(session, to_tg)
    if from_user is None or to_user is None:
        return False

    service = RatingService(settings)
    anti = await service.decide_seasonal_validity(
        session=session, dialog_id=dialog_id, from_user_id=from_user.id, to_user_id=to_user.id
    )

    rating = Rating(
        dialog_id=dialog_id,
        from_user_id=from_user.id,
        to_user_id=to_user.id,
        rating_type=rating_type,
        value=value,
        is_seasonal_valid=anti.is_seasonal_valid,
    )
    session.add(rating)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        logger.info("rating_duplicate dialog_id=%s from=%s type=%s", dialog_id, from_tg, rating_type.value)
        return False
    except Exception:
        await session.rollback()
        logger.exception("rating_save_failed dialog_id=%s", dialog_id)
        return False

    update = await service.recalculate(session, to_user.id)
    logger.info(
        "rating_saved dialog_id=%s from=%s to=%s type=%s value=%s seasonal=%s -> %.2f (n=%s)",
        dialog_id, from_tg, to_tg, rating_type.value, value, anti.is_seasonal_valid,
        update.chat_rating, update.chat_count,
    )

    if anti.is_seasonal_valid and update.previous_chat_rating and update.previous_chat_rating - update.chat_rating >= 3:
        await _alert_rating_drop(settings, to_tg, update.previous_chat_rating, update.chat_rating, value)
    return True


async def _alert_rating_drop(settings: Settings, to_tg: int, prev: float, new: float, value: int) -> None:
    """Log-only on purpose: broadcasting this to a chat would leak per-user data."""
    logger.warning("rating_drop to=%s prev=%.2f new=%.2f last=%s", to_tg, prev, new, value)


# ------------------------------------------------------------------ finishing
async def _finish_rating(
    bot: Bot, session: AsyncSession, redis: Redis, settings: Settings, tg_id: int, action: str
) -> None:
    await _clear_pending(redis, tg_id)

    if action == "next":
        from app.handlers.search import start_search_for

        await start_search_for(bot, session, redis, settings, tg_id)
        return

    await edit_ui(bot, redis, tg_id, "Спасибо! Возвращаемся в профиль…")
    await show_profile(bot, redis, session, settings, tg_id)


# ------------------------------------------------------------------ callbacks
@router.callback_query(F.data.startswith("rate:"))
async def rate_callback(
    call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    tg_id = int(call.from_user.id)
    parts = (call.data or "").split(":")
    if len(parts) != 3:
        await safe_answer_callback(call)
        return

    step = parts[1]  # chat | app
    try:
        value = int(parts[2])
    except ValueError:
        await safe_answer_callback(call, "Некорректная оценка")
        return
    if not 0 <= value <= 10:
        await safe_answer_callback(call, "Оценка от 0 до 10")
        return

    state = await _pending(redis, tg_id)
    if not state.get("dialog_id") or not state.get("partner"):
        await edit_ui(call.bot, redis, tg_id, "Эта оценка уже неактуальна — откройте /start.")
        await safe_answer_callback(call)
        return

    if not await rate_limit(redis, key=keys.rate(f"rate:{tg_id}"), ttl_s=2):
        await safe_answer_callback(call, "Секунду…")
        return

    dialog_id = int(state["dialog_id"])
    partner_tg = int(state["partner"])
    rating_type = RatingType.chat if step == "chat" else RatingType.appearance

    ok = await _save_rating(
        session, settings,
        dialog_id=dialog_id, from_tg=tg_id, to_tg=partner_tg,
        rating_type=rating_type, value=value,
    )
    await safe_answer_callback(call, "Оценка принята" if ok else "Вы уже оценили")

    if step == "chat" and state.get("has_photos") == "1":
        try:
            await redis.set(keys.pending_rating_step(tg_id), "appearance", ex=60 * 60)
        except Exception:
            pass
        await edit_ui(
            call.bot, redis, tg_id,
            "📸 <b>А теперь внешность собеседника</b>\n\n"
            "Собеседник присылал фото человека — поставьте оценку от 0 до 10.\n"
            "<i>Оценка анонимна, её увидит только рейтинг.</i>",
            kb=rating_kb("rate:app"),
        )
        return

    await edit_ui(call.bot, redis, tg_id, "✅ Спасибо! Оценка учтена.")
    await _finish_rating(call.bot, session, redis, settings, tg_id, str(state.get("action") or "end"))


@router.callback_query(F.data == "rate:skip")
async def rate_skip(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    tg_id = int(call.from_user.id)
    state = await _pending(redis, tg_id)
    await safe_answer_callback(call, "Пропущено")
    await edit_ui(call.bot, redis, tg_id, "Ок, пропустили.")
    await _finish_rating(call.bot, session, redis, settings, tg_id, str(state.get("action") or "end"))


@router.message(F.text.regexp(r"^\s*(10|[0-9])\s*$"))
async def rating_by_text(
    message: Message, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    """Typing a number works too — some people never tap inline buttons."""
    if message.from_user is None:
        return
    tg_id = int(message.from_user.id)
    state = await _pending(redis, tg_id)
    if not state.get("dialog_id") or not state.get("partner"):
        raise SkipHandler

    try:
        await message.delete()
    except Exception:
        pass

    value = int((message.text or "0").strip())
    step = "app" if state.get("step") == "appearance" else "chat"
    rating_type = RatingType.chat if step == "chat" else RatingType.appearance
    await _save_rating(
        session, settings,
        dialog_id=int(state["dialog_id"]), from_tg=tg_id, to_tg=int(state["partner"]),
        rating_type=rating_type, value=value,
    )

    if step == "chat" and state.get("has_photos") == "1":
        try:
            await redis.set(keys.pending_rating_step(tg_id), "appearance", ex=60 * 60)
        except Exception:
            pass
        await edit_ui(
            message.bot, redis, tg_id,
            "📸 Теперь оцените внешность (0–10) или нажмите «Пропустить».",
            kb=rating_kb("rate:app"),
        )
        return

    await _finish_rating(
        message.bot, session, redis, settings, tg_id, str(state.get("action") or "end")
    )


# ------------------------------------------------------------------ complaints
@router.callback_query(F.data == "complaint:start")
async def complaint_start(
    call: CallbackQuery, state: FSMContext, redis: Redis, session: AsyncSession, settings: Settings
) -> None:
    tg_id = int(call.from_user.id)
    pending = await _pending(redis, tg_id)
    if not pending.get("dialog_id"):
        await safe_answer_callback(call, "Жалоба доступна сразу после диалога")
        return

    if not await rate_limit(redis, key=keys.rate(f"complaint:{tg_id}"), ttl_s=30):
        await safe_answer_callback(call, "Жалоба уже отправлена")
        return

    await state.set_state(Complaint.reason)
    await edit_ui(
        call.bot, redis, tg_id,
        "<b>🚨 Жалоба</b>\n\n"
        "Опишите, что произошло, одним сообщением. Модератор увидит переписку этого диалога.\n\n"
        "<i>Запрещены: оскорбления, угрозы, спам, интимные фото без согласия, "
        "материалы с несовершеннолетними.</i>",
        kb=complaint_prompt_kb(),
    )
    await safe_answer_callback(call)


@router.callback_query(F.data == "complaint:cancel")
async def complaint_cancel(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    await state.clear()
    await show_profile(call.bot, redis, session, settings, int(call.from_user.id))
    await safe_answer_callback(call, "Отменено")


@router.message(Complaint.reason)
async def complaint_reason(
    message: Message, state: FSMContext, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    if message.from_user is None:
        return
    tg_id = int(message.from_user.id)
    reason = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    if len(reason) < 5:
        await edit_ui(
            message.bot, redis, tg_id,
            "Слишком коротко. Опишите ситуацию хотя бы одним предложением.",
            kb=complaint_prompt_kb(),
        )
        return

    pending = await _pending(redis, tg_id)
    dialog_id = pending.get("dialog_id")
    await state.clear()

    if not dialog_id:
        await edit_ui(message.bot, redis, tg_id, "Жалоба недоступна: диалог уже закрыт.")
        return

    created = await ComplaintService().create(
        session, dialog_id=int(dialog_id), from_tg_id=tg_id, reason=reason
    )
    if created is not None:
        await ComplaintService().notify_moderators(message.bot, redis, settings, created, reason)

    await _clear_pending(redis, tg_id)
    await edit_ui(
        message.bot, redis, tg_id,
        "✅ <b>Жалоба принята</b>\n\n"
        f"Номер обращения: <code>#{created.complaint_id if created else '—'}</code>\n"
        "Модератор проверит переписку и примет решение. Спасибо, что помогаете сервису.",
    )
    await show_profile(message.bot, redis, session, settings, tg_id)

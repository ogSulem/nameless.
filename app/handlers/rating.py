from __future__ import annotations

import logging
from datetime import date

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.dispatcher.event.bases import SkipHandler
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Rating, RatingType, User
from app.keyboards.dialog_reply import dialog_reply_kb
from app.keyboards.rating import complaint_only_kb, complaint_prompt_kb
from app.keyboards.search import cancel_search_kb
from app.keyboards.start_reply import start_reply_kb
from app.flows.profile import show_profile
from app.redis import keys
from app.services.complaint import ComplaintService
from app.services.matchmaking import MatchmakingService
from app.services.rating import RatingService
from app.ui import send_new_ui, edit_ui, get_ui_message_id
from app.telegram_safe import safe_delete_message, safe_send_message, safe_edit_message_text
from app.utils.ratelimit import rate_limit

logger = logging.getLogger(__name__)
router = Router(name="rating")


class ComplaintStates(StatesGroup):
    waiting_reason = State()


def _b2s(v: str | bytes | None) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="ignore")
    return v


def _rating_message_key(tg_id: int) -> str:
    return keys.ui_rating_message_id(tg_id)


async def _user_id_by_tg_cached(redis: Redis, session: AsyncSession, tg_id: int) -> int | None:
    key = keys.user_id_by_tg(int(tg_id))
    try:
        v = await redis.get(key)
        if v:
            return int(v)
    except Exception:
        pass

    res = await session.execute(select(User.id).where(User.telegram_id == int(tg_id)))
    uid = res.scalar_one_or_none()
    if uid is None:
        return None

    try:
        await redis.set(key, str(int(uid)), ex=300)
    except Exception:
        pass
    return int(uid)


async def _get_rating_message_id(redis: Redis, tg_id: int) -> int | None:
    v = await redis.get(_rating_message_key(tg_id))
    return int(v) if v else None


async def _set_rating_message_id(redis: Redis, tg_id: int, message_id: int) -> None:
    await redis.set(_rating_message_key(tg_id), str(message_id), ex=60 * 60 * 24 * 30)


async def _edit_rating_message(bot, redis: Redis, tg_id: int, text: str, reply_markup=None) -> None:
    mid = await _get_rating_message_id(redis, tg_id)
    if mid:
        try:
            ok = await safe_edit_message_text(bot, tg_id, mid, text, reply_markup=reply_markup)
            if ok:
                return
        except Exception:
            pass
    
    msg = await send_new_ui(bot, redis, tg_id, text, kb=reply_markup)
    await _set_rating_message_id(redis, tg_id, msg.message_id)


async def _clear_pending(redis: Redis, tg_id: int) -> None:
    pipe = redis.pipeline(transaction=False)
    pipe.delete(keys.pending_rating(tg_id))
    pipe.delete(keys.pending_rating_has_photos(tg_id))
    pipe.delete(keys.pending_rating_partner(tg_id))
    pipe.delete(keys.pending_rating_action(tg_id))
    pipe.delete(keys.pending_rating_step(tg_id))
    pipe.delete(_rating_message_key(tg_id))
    try:
        await pipe.execute()
    except Exception:
        # Best-effort cleanup
        pass


async def _pending_dialog(redis: Redis, tg_id: int) -> tuple[int | None, bool]:
    pipe = redis.pipeline(transaction=False)
    pipe.get(keys.pending_rating(tg_id))
    pipe.get(keys.pending_rating_has_photos(tg_id))
    try:
        d_raw, hp_raw = await pipe.execute()
    except Exception:
        d_raw, hp_raw = None, None

    d = _b2s(d_raw)
    hp = _b2s(hp_raw)
    return (int(d) if d else None, hp == "1")


async def _pending_action(redis: Redis, tg_id: int) -> str:
    v = _b2s(await redis.get(keys.pending_rating_action(tg_id)))
    return v or "end"


async def _get_search_message_id(redis: Redis, tg_id: int) -> int | None:
    v = await redis.get(keys.ui_search_message_id(tg_id))
    return int(v) if v else None


async def _clear_search_message_id(redis: Redis, tg_id: int) -> None:
    await redis.delete(keys.ui_search_message_id(tg_id))


def _age(birth_date: date) -> int:
    today = date.today()
    years = today.year - birth_date.year
    if (today.month, today.day) < (birth_date.month, birth_date.day):
        years -= 1
    return years


def _gender_short(v: str) -> str:
    return "М" if v == "male" else "Ж"


async def _continue_after_rating_tg(bot, tg_id: int, session: AsyncSession, redis: Redis) -> None:
    pipe = redis.pipeline(transaction=False)
    pipe.get(keys.pending_rating_action(tg_id))
    pipe.get(_rating_message_key(tg_id))
    try:
        action_raw, mid_raw = await pipe.execute()
    except Exception:
        action_raw, mid_raw = None, None

    action = _b2s(action_raw) or "end"
    mid = int(mid_raw) if mid_raw else None

    await _clear_pending(redis, tg_id)

    logger.info("continue_rating tg=%s action=%s mid=%s", tg_id, action, mid)
    if action == "skip":
        # Ensure we edit the rating message to "Searching..."
        try:
            if mid:
                ok = await safe_edit_message_text(bot, tg_id, mid, "Ищу собеседника...", reply_markup=cancel_search_kb())
                if not ok:
                    raise Exception("edit_failed")
                await redis.set(keys.ui_search_message_id(tg_id), str(mid), ex=60 * 60 * 24 * 30)
            else:
                msg = await send_new_ui(bot, redis, tg_id, "Ищу собеседника...", kb=cancel_search_kb())
                await redis.set(keys.ui_search_message_id(tg_id), str(msg.message_id), ex=60 * 60 * 24 * 30)
        except Exception:
            msg = await send_new_ui(bot, redis, tg_id, "Ищу собеседника...", kb=cancel_search_kb())
            await redis.set(keys.ui_search_message_id(tg_id), str(msg.message_id), ex=60 * 60 * 24 * 30)

        res = await session.execute(select(User).where(User.telegram_id == tg_id))
        user = res.scalar_one_or_none()
        if user is None or user.is_banned:
            await show_profile(bot, redis, session, tg_id)
            return

        svc = MatchmakingService(redis)
        premium = await svc.is_user_premium(session, user.id)
        result = await svc.try_match(session=session, user=user, premium=premium)
        if result is None:
            return

        # MATCH FOUND
        # Clean up searching UI (which is our edited rating message)
        search_mid = await _get_search_message_id(redis, tg_id)
        if search_mid:
            await safe_delete_message(bot, tg_id, search_mid)
            await _clear_search_message_id(redis, tg_id)

        res2 = await session.execute(select(User).where(User.telegram_id == result.partner_user_id))
        partner = res2.scalar_one()

        my_card = (
            "Собеседник найден!\n"
            f"Пол: {_gender_short(partner.gender.value)}\n"
            f"Возраст: {_age(partner.birth_date)}\n"
            f"Город: {partner.city or '-'}\n"
            f"Рейтинг: {float(partner.season_rating_chat or 0.0):.1f}"
        )
        partner_card = (
            "Собеседник найден!\n"
            f"Пол: {_gender_short(user.gender.value)}\n"
            f"Возраст: {_age(user.birth_date)}\n"
            f"Город: {user.city or '-'}\n"
            f"Рейтинг: {float(user.season_rating_chat or 0.0):.1f}"
        )

        # 1. Send cards with reply keyboard ATTACHED
        await safe_send_message(bot, tg_id, my_card, reply_markup=dialog_reply_kb())
        
        partner_search_mid = await _get_search_message_id(redis, result.partner_user_id)
        if partner_search_mid:
            await safe_delete_message(bot, result.partner_user_id, partner_search_mid)
            await _clear_search_message_id(redis, result.partner_user_id)
        
        await safe_send_message(bot, result.partner_user_id, partner_card, reply_markup=dialog_reply_kb())
        
        logger.info("search_matched_after_skip dialog_id=%s u=%s p=%s", result.dialog_id, user.telegram_id, result.partner_user_id)
        return

    # For action == "end" (STOP pressed)
    await show_profile(bot, redis, session, tg_id)


async def _continue_after_rating(call: CallbackQuery, session: AsyncSession, redis: Redis) -> None:
    await _continue_after_rating_tg(call.bot, call.from_user.id, session, redis)


def _parse_rating(text: str | None) -> int | None:
    if text is None:
        return None
    t = text.strip()
    if not t.isdigit():
        return None
    v = int(t)
    if 0 <= v <= 10:
        return v
    return None


async def _pending_step(redis: Redis, tg_id: int) -> str:
    v = _b2s(await redis.get(keys.pending_rating_step(tg_id)))
    return v or "chat"


@router.message()
async def rating_text_input(message: Message, session: AsyncSession, redis: Redis, state: FSMContext, settings: Settings) -> None:
    if message.text:
        try:
            await message.delete()
        except Exception:
            pass

        if message.text.startswith("/"):
            return

    dialog_id, has_photos = await _pending_dialog(redis, message.from_user.id)
    if not dialog_id:
        current_state = await state.get_state()
        if current_state is not None:
            raise SkipHandler

        dialog = await redis.get(keys.active_dialog(message.from_user.id))
        if dialog:
            return

        return

    current_state = await state.get_state()
    if current_state is not None:
        raise SkipHandler

    logger.info(
        "rating_message_received tg=%s dialog_id=%s step_key=%s text=%r",
        message.from_user.id,
        dialog_id,
        await redis.get(keys.pending_rating_step(message.from_user.id)),
        message.text,
    )

    step = await _pending_step(redis, message.from_user.id)
    if step not in {"chat", "appearance"}:
        step = "chat"

    value = _parse_rating(message.text)

    if value is None:
        logger.info("rating_invalid_input dialog_id=%s tg=%s step=%s text=%r", dialog_id, message.from_user.id, step, message.text)
        try:
            await message.delete()
        except Exception:
            pass
        return

    from_user_id = await _user_id_by_tg_cached(redis, session, message.from_user.id)
    if from_user_id is None:
        return

    to_tg_raw = None
    try:
        to_tg_raw = await redis.get(keys.pending_rating_partner(message.from_user.id))
    except Exception:
        to_tg_raw = None

    to_user_id = None
    to_tg = int(to_tg_raw) if to_tg_raw else None
    if to_tg:
        to_user_id = await _user_id_by_tg_cached(redis, session, to_tg)
    if to_user_id is None:
        await send_new_ui(message.bot, redis, message.from_user.id, "Не удалось определить собеседника")
        return

    res_to = await session.execute(
        select(
            User.telegram_id,
            User.gender,
            User.birth_date,
            User.city,
            User.season_rating_chat,
            User.season_rating_appearance,
        ).where(User.id == to_user_id)
    )
    to_row = res_to.one_or_none()
    if to_row is None:
        return

    to_tg_id, to_gender, to_birth_date, to_city, to_season_chat, to_season_app = to_row

    logger.info(
        "rating_input dialog_id=%s from_tg=%s step=%s value=%s has_photos=%s",
        dialog_id,
        message.from_user.id,
        step,
        value,
        has_photos,
    )

    rating_type = RatingType.chat if step == "chat" else RatingType.appearance
    r = Rating(
        dialog_id=dialog_id,
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        rating_type=rating_type,
        value=value,
    )

    anti = await RatingService().decide_seasonal_validity(
        session=session,
        dialog_id=dialog_id,
        from_user_id=from_user_id,
        to_user_id=to_user_id,
    )
    r.is_seasonal_valid = anti.is_seasonal_valid

    session.add(r)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        await send_new_ui(message.bot, redis, message.from_user.id, "Оценка уже была поставлена")
        await _continue_after_rating_tg(message.bot, message.from_user.id, session, redis)
        return

    recalculated, new_chat, new_app, prev_chat = await RatingService().on_rating_saved(session=session, to_user_id=to_user_id)

    try:
        await redis.delete(keys.profile_text(int(to_tg_id)))
    except Exception:
        pass

    logger.info(
        "rating_recalculated_applied dialog_id=%s to_tg=%s avg_chat=%s avg_app=%s seasonal_valid=%s",
        dialog_id,
        int(to_tg_id),
        float(to_season_chat or 0.0),
        float(to_season_app or 0.0),
        bool(r.is_seasonal_valid),
    )

    if recalculated and step == "chat" and prev_chat and abs(prev_chat - new_chat) >= 3:
        for admin_id in settings.alerts_target_ids:
            try:
                await safe_send_message(
                    message.bot,
                    admin_id,
                    "\n".join(
                        [
                            "⚠️ Резкое изменение среднего рейтинга",
                            f"User: {int(to_tg_id)} (tg://user?id={int(to_tg_id)})",
                            f"Пол: {_gender_short(to_gender.value)}",
                            f"Возраст: {_age(to_birth_date)}",
                            f"Город: {to_city or '-'}",
                            f"Prev avg chat: {prev_chat:.2f}",
                            f"New avg chat: {new_chat:.2f}",
                            f"Incoming rating: {value}",
                            f"Dialog: {dialog_id}",
                            f"Rater: {message.from_user.id} (tg://user?id={message.from_user.id})",
                        ]
                    ),
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("failed_notify_admin_drop admin=%s", admin_id)

    if step == "chat" and has_photos:
        try:
            await redis.set(keys.pending_rating_step(message.from_user.id), "appearance", ex=60 * 60)
        except Exception:
            pass
        await _edit_rating_message(message.bot, redis, message.from_user.id, "Введи рейтинг по внешности от 0 до 10:")
        logger.info("rated_chat dialog_id=%s from=%s", dialog_id, message.from_user.id)
        return

    await _continue_after_rating_tg(message.bot, message.from_user.id, session, redis)
    logger.info("rated_%s dialog_id=%s from=%s", step, dialog_id, message.from_user.id)


@router.callback_query(F.data == "complaint")
async def complaint_start(call: CallbackQuery, state: FSMContext, redis: Redis) -> None:
    if call.from_user is None:
        await call.answer()
        return

    ok = await rate_limit(redis, key=f"rl:user:complaint_start:{call.from_user.id}", ttl_s=2)
    if not ok:
        await call.answer()
        return
    await state.set_state(ComplaintStates.waiting_reason)
    await _edit_rating_message(
        call.bot,
        redis,
        call.from_user.id,
        "Опиши причину жалобы текстом:",
        reply_markup=complaint_prompt_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "complaint_cancel")
async def complaint_cancel(call: CallbackQuery, state: FSMContext, session: AsyncSession, redis: Redis) -> None:
    if call.from_user is None:
        await call.answer()
        return

    ok = await rate_limit(redis, key=f"rl:user:complaint_cancel:{call.from_user.id}", ttl_s=2)
    if not ok:
        await call.answer()
        return
    current_state = await state.get_state()
    if current_state != ComplaintStates.waiting_reason.state:
        await call.answer()
        return

    await state.clear()
    try:
        from app.flows.profile import show_profile
        await show_profile(call.bot, redis, session, call.from_user.id)
    except Exception:
        logger.exception("COMPLAINT_CANCEL_SHOW_PROFILE_FAILED tg=%s", call.from_user.id)

    await call.answer()


@router.message(ComplaintStates.waiting_reason)
async def complaint_reason(message: Message, state: FSMContext, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    # 1. Store input and delete user message immediately
    reason = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass

    if not reason:
        return

    # 2. Get pending dialog info
    dialog_id, _ = await _pending_dialog(redis, message.from_user.id)
    if not dialog_id:
        await _edit_rating_message(message.bot, redis, message.from_user.id, "Нет диалога для жалобы")
        await state.clear()
        return

    svc = ComplaintService()

    # 4. Finalize: clear state and edit bot message to profile IMMEDIATELY
    await state.clear()
    await _clear_pending(redis, message.from_user.id)

    rating_mid = await redis.get(f"ui:rating_message_id:{message.from_user.id}")
    if rating_mid:
        try:
            await set_ui_message_id(redis, message.from_user.id, int(rating_mid))
        except Exception:
            pass
    
    # Show profile by editing the current UI message
    try:
        from app.flows.profile import show_profile
        await show_profile(message.bot, redis, session, message.from_user.id)
        logger.info("COMPLAINT_PROFILE_SHOWN tg=%s mid=%s", message.from_user.id, rating_mid)
    except Exception:
        logger.exception("FAILED_SHOW_PROFILE_IN_COMPLAINT tg=%s", message.from_user.id)

    # 5. Notify admins/channel (ALERTS_CHAT_ID has priority via settings.alerts_target_ids)
    admin_ids = [int(i) for i in settings.alerts_target_ids] if settings.alerts_target_ids else []
    if not admin_ids:
        logger.error("ALERTS_TARGET_IDS_EMPTY")
        return

    try:
        await svc.create_and_notify(
            bot=message.bot,
            session=session,
            admins=set(admin_ids),
            media_root=settings.media_root,
            dialog_id=dialog_id,
            from_tg_id=message.from_user.id,
            reason=reason,
        )
        logger.info("COMPLAINT_NOTIFY_OK dialog_id=%s to=%s", dialog_id, admin_ids)
    except Exception:
        logger.exception("COMPLAINT_NOTIFY_FAILED dialog_id=%s to=%s", dialog_id, admin_ids)

    logger.info("complaint_sent_ui_updated dialog_id=%s from=%s", dialog_id, message.from_user.id)

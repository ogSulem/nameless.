from __future__ import annotations

import io
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import ActiveDialog, Dialog, DialogStatus, User
from app.redis import keys
from app.services.dialog import DialogService
from app.services.ai import AIService
from app.ui import send_new_ui
from app.keyboards.rating import complaint_only_kb
from app.keyboards.start_reply import start_reply_kb as get_start_kb
from app.telegram_safe import safe_delete_message, safe_send_message, safe_send_photo
from app.utils.markdown import escape_markdown
from app.utils.ratelimit import rate_limit

logger = logging.getLogger(__name__)
router = Router(name="dialog")

_AI_SERVICE = AIService()
_AI_DETECT_SEMAPHORE: asyncio.Semaphore | None = None
_PHOTO_PIPELINE_SEMAPHORE: asyncio.Semaphore | None = None


def _get_ai_semaphore(settings: Settings) -> asyncio.Semaphore:
    global _AI_DETECT_SEMAPHORE
    if _AI_DETECT_SEMAPHORE is None:
        n = int(getattr(settings, "vision_concurrency", 2) or 2)
        if n <= 0:
            n = 1
        _AI_DETECT_SEMAPHORE = asyncio.Semaphore(n)
    return _AI_DETECT_SEMAPHORE


def _get_photo_pipeline_semaphore(settings: Settings) -> asyncio.Semaphore:
    global _PHOTO_PIPELINE_SEMAPHORE
    if _PHOTO_PIPELINE_SEMAPHORE is None:
        # Same cap as AI detect by default: safe and predictable under load.
        n = int(getattr(settings, "vision_concurrency", 2) or 2)
        if n <= 0:
            n = 1
        # Allow a bit more parallelism for I/O-heavy pipeline, but keep bounded.
        n = max(1, min(int(n) * 2, 8))
        _PHOTO_PIPELINE_SEMAPHORE = asyncio.Semaphore(n)
    return _PHOTO_PIPELINE_SEMAPHORE

async def _get_dialog_id(redis: Redis, tg_id: int) -> int | None:
    v = await redis.get(keys.active_dialog(tg_id))
    return int(v) if v else None


async def _get_user_id_and_rating_cached(
    redis: Redis,
    session: AsyncSession,
    tg_id: int,
) -> tuple[int | None, float]:
    uid_key = keys.user_id_by_tg(tg_id)
    rating_key = keys.user_rating_chat_by_tg(tg_id)
    pipe = redis.pipeline(transaction=False)
    pipe.get(uid_key)
    pipe.get(rating_key)
    try:
        uid_raw, rating_raw = await pipe.execute()
    except Exception:
        uid_raw, rating_raw = None, None

    uid: int | None = int(uid_raw) if uid_raw else None
    rating: float = float(rating_raw) if rating_raw else 0.0
    if uid is not None:
        return (uid, rating)

    res = await session.execute(select(User.id, User.season_rating_chat).where(User.telegram_id == tg_id))
    row = res.one_or_none()
    if row is None:
        return (None, 0.0)

    uid = int(row[0])
    rating = float(row[1] or 0.0)
    try:
        pipe2 = redis.pipeline(transaction=False)
        pipe2.set(uid_key, str(uid), ex=60)
        pipe2.set(rating_key, str(rating), ex=60)
        await pipe2.execute()
    except Exception:
        pass
    return (uid, rating)


async def _log_exception_throttled(
    redis: Redis,
    *,
    key: str,
    ttl_s: int,
    logger_: logging.Logger,
    exc_msg: str,
    warn_msg: str,
    **warn_kwargs: Any,
) -> None:
    allow_full = True
    try:
        ok = await redis.set(key, "1", nx=True, ex=int(ttl_s))
        allow_full = bool(ok)
    except Exception:
        allow_full = True

    if allow_full:
        logger_.exception(exc_msg)
    else:
        logger_.warning(warn_msg, **warn_kwargs)


@router.callback_query()
async def ignore_callbacks_during_dialog(call: CallbackQuery, redis: Redis) -> None:
    if call.from_user is None:
        await call.answer()
        return
    if await redis.get(keys.active_dialog(call.from_user.id)):
        await call.answer()
        return

    raise SkipHandler


async def _finish_and_request_rating(
    bot,
    redis: Redis,
    session: AsyncSession,
    dialog: Dialog,
    action: str,
    actor_tg_id: int,
) -> None:
    dialog_id = dialog.id
    res_tg = await session.execute(
        select(User.telegram_id)
        .where(User.id.in_([dialog.user1_id, dialog.user2_id]))
    )
    tgs = [int(x) for x in res_tg.scalars().all()]
    
    # Strictly define who is who to avoid any selection issues
    tg1 = None
    tg2 = None
    if len(tgs) >= 2:
        tg1, tg2 = tgs[0], tgs[1]
    elif len(tgs) == 1:
        tg1 = tgs[0]

    logger.info("FINISH_DIALOG_START dialog_id=%s actor=%s tgs=%s", dialog_id, actor_tg_id, tgs)

    try:
        if dialog.status != DialogStatus.finished:
            dialog.status = DialogStatus.finished
            dialog.finished_at = datetime.now(tz=timezone.utc)

        # IMPORTANT: Delete from ActiveDialog in DB
        await session.execute(delete(ActiveDialog).where(ActiveDialog.user_id.in_([dialog.user1_id, dialog.user2_id])))
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("failed_finish_dialog_db dialog_id=%s", dialog_id)

    # 1. Clear Redis and prepare rating data for BOTH (batch writes to reduce partial state)
    ttl = 60 * 60
    pipe = redis.pipeline(transaction=False)

    # Read appearance flags first (batch reads to reduce round-trips)
    has_human_for_tg: dict[int, str] = {}
    tgs_to_check = [int(t) for t in (tg1, tg2) if t]
    if tgs_to_check:
        read_pipe = redis.pipeline(transaction=False)
        for t in tgs_to_check:
            read_pipe.get(keys.appearance_rating_required(t, dialog_id))
        try:
            vals = await read_pipe.execute()
        except Exception:
            vals = [None for _ in tgs_to_check]

        for t, v in zip(tgs_to_check, vals, strict=False):
            has_human_for_tg[t] = "1" if v else "0"

    for t in [tg1, tg2]:
        if not t:
            continue
        pipe.delete(keys.active_dialog(int(t)))
        pipe.set(keys.pending_rating(int(t)), str(dialog_id), ex=ttl)
        pipe.set(keys.pending_rating_has_photos(int(t)), has_human_for_tg.get(int(t), "0"), ex=ttl)
        pipe.set(keys.pending_rating_step(int(t)), "chat", ex=ttl)

    if tg1 and tg2:
        # Avoid immediate rematch with the same partner (e.g. after skip)
        # TTL: keep a short cooldown to prevent rematch "right away" without long-term bias.
        pipe.set(keys.last_partner(int(tg1)), str(int(tg2)), ex=60 * 10)
        pipe.set(keys.last_partner(int(tg2)), str(int(tg1)), ex=60 * 10)

        pipe.set(keys.pending_rating_partner(int(tg1)), str(int(tg2)), ex=ttl)
        pipe.set(keys.pending_rating_partner(int(tg2)), str(int(tg1)), ex=ttl)

        # 2. Assign actions
        if action == "skip":
            a1, a2 = "skip", "skip"
        else:
            if actor_tg_id == int(tg1):
                a1, a2 = "end", "skip"
            else:
                a1, a2 = "skip", "end"

        pipe.set(keys.pending_rating_action(int(tg1)), a1, ex=ttl)
        pipe.set(keys.pending_rating_action(int(tg2)), a2, ex=ttl)

    try:
        await pipe.execute()
    except Exception:
        logger.exception("finish_dialog_redis_pipeline_failed dialog_id=%s", dialog_id)

    # 3. Send "Dialog finished" message to BOTH with reply keyboard /start
    start_kb = get_start_kb()

    for t in [tg1, tg2]:
        if not t: continue
        try:
            # We send /start button as requested
            await safe_send_message(bot, t, "Диалог завершен.", reply_markup=start_kb)
            logger.info("SENT_DIALOG_FINISHED_OK tg=%s", t)
        except Exception:
            logger.exception("FAILED_SEND_DIALOG_FINISHED tg=%s", t)

        try:
            # 4. Send the rating UI (inline)
            msg = await send_new_ui(
                bot,
                redis,
                t,
                "Оцени общение (0-10):",
                kb=complaint_only_kb(),
            )
            await redis.set(keys.ui_rating_message_id(t), str(msg.message_id), ex=60 * 60)
            logger.info("SENT_RATING_UI_OK tg=%s mid=%s", t, msg.message_id)
        except Exception:
            logger.exception("FAILED_SEND_RATING_UI tg=%s", t)

    logger.info("dialog_finished_complete dialog_id=%s", dialog_id)

    logger.info("dialog_finished dialog_id=%s action=%s", dialog_id, action)


@router.message(F.text.in_({"⏭️", "🛑"}))
async def dialog_finish_text(message: Message, session: AsyncSession, redis: Redis) -> None:
    if message.from_user is None:
        raise SkipHandler

    ok = await rate_limit(redis, key=f"rl:user:dialog_finish:{message.from_user.id}", ttl_s=1)
    if not ok:
        raise SkipHandler
    action = "skip" if message.text == "⏭️" else "end"

    # Prevent double execution if user taps repeatedly / duplicated updates.
    try:
        lock_key = keys.lock_finish_dialog(message.from_user.id)
        got = await redis.set(lock_key, "1", nx=True, ex=4)
        if not got:
            raise SkipHandler
    except SkipHandler:
        raise
    except Exception:
        # If Redis fails, continue (better to finish dialog than to hang).
        pass

    dialog_id = await _get_dialog_id(redis, message.from_user.id)
    if not dialog_id:
        raise SkipHandler

    dialog = await session.get(Dialog, dialog_id)
    if dialog is None:
        await redis.delete(keys.active_dialog(message.from_user.id))
        raise SkipHandler

    await _finish_and_request_rating(
        actor_tg_id=message.from_user.id,
        action=action,
        bot=message.bot,
        session=session,
        redis=redis,
        dialog=dialog,
    )


@router.message(F.photo | F.text)
async def relay_messages(message: Message, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if message.from_user is None:
        raise SkipHandler
    if message.chat is None:
        raise SkipHandler
    dialog_id = await _get_dialog_id(redis, message.from_user.id)
    if not dialog_id:
        raise SkipHandler

    is_command = False
    if message.text:
        t = message.text.lstrip()
        if t.startswith("/"):
            is_command = True
        elif message.entities:
            try:
                if any(getattr(e, "type", None) == "bot_command" for e in message.entities):
                    is_command = True
            except Exception:
                pass

    if is_command:
        try:
            await safe_delete_message(message.bot, message.chat.id, message.message_id)
        except Exception:
            pass
        raise SkipHandler

    me_user_id, me_rating = await _get_user_id_and_rating_cached(redis, session, message.from_user.id)
    if me_user_id is None:
        return

    partner_tg: int | None = None
    try:
        v = await redis.get(keys.dialog_partner_tg(dialog_id, message.from_user.id))
        if v:
            partner_tg = int(v)
    except Exception:
        partner_tg = None

    if partner_tg is None:
        res_d = await session.execute(select(Dialog).where(Dialog.id == dialog_id))
        dialog = res_d.scalar_one_or_none()
        if dialog is None or dialog.status != DialogStatus.active:
            await redis.delete(keys.active_dialog(message.from_user.id))
            raise SkipHandler

        partner_user_id = dialog.user2_id if dialog.user1_id == me_user_id else dialog.user1_id
        res_p = await session.execute(select(User.telegram_id).where(User.id == partner_user_id))
        partner_tg = int(res_p.scalar_one())
        try:
            await redis.set(keys.dialog_partner_tg(dialog_id, message.from_user.id), str(partner_tg), ex=60 * 60 * 12)
        except Exception:
            pass

    logger.info(
        "relay_start dialog_id=%s from_tg=%s to_tg=%s has_text=%s has_photo=%s",
        dialog_id,
        message.from_user.id,
        partner_tg,
        bool(message.text),
        bool(message.photo),
    )

    svc = DialogService(media_root=settings.media_root)

    if message.text:
        try:
            await safe_send_message(message.bot, partner_tg, message.text)
            logger.info("relay_text_ok dialog_id=%s from_tg=%s to_tg=%s", dialog_id, message.from_user.id, partner_tg)
        except Exception:
            logger.exception("relay_text_failed dialog_id=%s from_tg=%s to_tg=%s", dialog_id, message.from_user.id, partner_tg)

        try:
            await svc.save_text(session, dialog_id, me_user_id, message.text)
        except Exception:
            await _log_exception_throttled(
                redis,
                key=f"throttle:relay_db_save:text:{dialog_id}:{message.from_user.id}",
                ttl_s=60,
                logger_=logger,
                exc_msg=f"relay_text_db_save_failed dialog_id={dialog_id} from_tg={message.from_user.id}",
                warn_msg="relay_text_db_save_failed_throttled dialog_id=%s from_tg=%s",
                dialog_id=dialog_id,
                from_tg=message.from_user.id,
            )
        return

    if message.photo:
        # 1. Forward to partner
        try:
            await safe_send_photo(message.bot, partner_tg, photo=message.photo[-1].file_id)
            logger.info("relay_photo_ok dialog_id=%s from_tg=%s to_tg=%s", dialog_id, message.from_user.id, partner_tg)
        except Exception:
            logger.exception("relay_photo_failed dialog_id=%s from_tg=%s to_tg=%s", dialog_id, message.from_user.id, partner_tg)

        sender_username = escape_markdown(message.from_user.username or "NoTag")
        sender_full_name = escape_markdown(message.from_user.full_name or "Unknown")
        sender_rating = float(me_rating or 0.0)
        partner_label = f"{partner_tg}"

        async def _background_photo_pipeline() -> None:
            # Run expensive checks off the critical path.
            has_human = False
            ai_meta: dict[str, Any] | None = None
            sem_bg = _get_photo_pipeline_semaphore(settings)
            async with sem_bg:
                try:
                    redis_key = keys.dialog_sender_human_detected(dialog_id, message.from_user.id)

                    already_required = False
                    already_detected = None
                    try:
                        pre_pipe = redis.pipeline(transaction=False)
                        pre_pipe.get(keys.appearance_rating_required(partner_tg, dialog_id))
                        pre_pipe.get(redis_key)
                        v_req, v_det = await pre_pipe.execute()
                        already_required = bool(v_req == b"1" or v_req == "1")
                        already_detected = v_det
                    except Exception:
                        already_required = False
                        already_detected = None

                    if already_required:
                        has_human = True
                        ai_meta = {"cached": True, "reason": "appearance_flag"}
                    else:
                        # Check if we already detected a human from this sender in this dialog to save CPU
                        if already_detected == b"1" or already_detected == "1":
                            has_human = True
                            ai_meta = {"cached": True}
                        else:
                            dest = io.BytesIO()
                            # Use a smaller Telegram photo size for faster download and detection.
                            photo = message.photo[-2] if len(message.photo) >= 2 else message.photo[-1]
                            try:
                                await asyncio.wait_for(
                                    message.bot.download(photo, destination=dest),
                                    timeout=float(getattr(settings, "vision_timeout_s", 4.0) or 4.0),
                                )
                            except asyncio.TimeoutError:
                                raise asyncio.TimeoutError("download_timeout")
                            dest.seek(0)

                            photo_bytes = dest.read()
                            try:
                                _AI_SERVICE.configure_from_settings(settings)
                                sem = _get_ai_semaphore(settings)
                                async with sem:
                                    is_human, ai_meta = await asyncio.wait_for(
                                        _AI_SERVICE.detect_human_with_meta(photo_bytes),
                                        timeout=float(getattr(settings, "vision_timeout_s", 4.0) or 4.0),
                                    )
                            except asyncio.TimeoutError:
                                is_human = False
                                ai_meta = {"backend": "timeout", "error": "detect_timeout"}

                            if is_human:
                                has_human = True
                                await redis.set(redis_key, "1", ex=3600)

                                try:
                                    a_pipe = redis.pipeline(transaction=False)
                                    a_pipe.get(keys.active_dialog(message.from_user.id))
                                    a_pipe.get(keys.active_dialog(partner_tg))
                                    cur_sender_dialog, cur_partner_dialog = await a_pipe.execute()
                                except Exception:
                                    cur_sender_dialog, cur_partner_dialog = None, None
                                if (
                                    cur_sender_dialog
                                    and cur_partner_dialog
                                    and int(cur_sender_dialog) == int(dialog_id)
                                    and int(cur_partner_dialog) == int(dialog_id)
                                ):
                                    await redis.set(keys.appearance_rating_required(partner_tg, dialog_id), "1", ex=3600)
                                else:
                                    logger.info(
                                        "AI human detected but dialog not active anymore; skip appearance flag sender=%s partner=%s dialog_id=%s cur_sender=%s cur_partner=%s",
                                        message.from_user.id,
                                        partner_tg,
                                        dialog_id,
                                        cur_sender_dialog,
                                        cur_partner_dialog,
                                    )

                    if ai_meta is not None:
                        logger.info(
                            "ai_face_verdict dialog_id=%s from_tg=%s to_tg=%s verdict=%s backend=%s faces=%s eyes=%s size=%sx%s cached=%s ms=%s err=%s insight_err=%s",
                            dialog_id,
                            message.from_user.id,
                            partner_tg,
                            1 if has_human else 0,
                            ai_meta.get("backend"),
                            ai_meta.get("faces"),
                            ai_meta.get("eyes"),
                            ai_meta.get("w"),
                            ai_meta.get("h"),
                            1 if ai_meta.get("cached") else 0,
                            ai_meta.get("duration_ms"),
                            ai_meta.get("error"),
                            ai_meta.get("insight_error") or ai_meta.get("insight_init_error"),
                        )

                    caption = (
                        f"📸 From: {sender_full_name} (@{sender_username}) [ID: {message.from_user.id}] [Rating: {sender_rating:.1f}] "
                        f"-> To: {partner_label} [Dialog: {dialog_id}]"
                    )
                    caption += f" [AI Human: {'✅' if has_human else '❌'}]"

                    if ai_meta is not None:
                        backend = ai_meta.get("backend")
                        faces = ai_meta.get("faces")
                        eyes = ai_meta.get("eyes")
                        w = ai_meta.get("w")
                        h = ai_meta.get("h")
                        cached = ai_meta.get("cached")
                        err = ai_meta.get("error")

                        details = []
                        if backend is not None:
                            details.append(f"backend={backend}")
                        if w is not None and h is not None:
                            details.append(f"size={w}x{h}")
                        if faces is not None:
                            details.append(f"faces={faces}")
                        if eyes is not None:
                            details.append(f"eyes={eyes}")
                        if cached:
                            details.append("cached=1")
                        if err:
                            details.append(f"err={err}")

                        if details:
                            caption += " [AI " + ", ".join(details) + "]"

                    target_chat_id = int(getattr(settings, "alerts_chat_id", 0) or 0)
                    if target_chat_id:
                        try:
                            await safe_send_photo(
                                message.bot,
                                chat_id=target_chat_id,
                                photo=message.photo[-1].file_id,
                                caption=caption,
                            )
                        except Exception:
                            logger.exception("failed_send_photo_to_admin alerts_chat_id=%s", settings.alerts_chat_id)
                except Exception:
                    logger.exception("background_photo_pipeline_failed alerts_chat_id=%s", settings.alerts_chat_id)

        try:
            asyncio.create_task(_background_photo_pipeline())
        except Exception:
            logger.exception("failed_schedule_background_photo_pipeline")

        # 3. Save reference to DB (without local storage if possible, but keeping DB record for tracking)
        try:
            await svc.save_photo(message.bot, session, dialog_id, me_user_id, message.from_user.id, message)
        except Exception:
            await _log_exception_throttled(
                redis,
                key=f"throttle:relay_db_save:photo:{dialog_id}:{message.from_user.id}",
                ttl_s=60,
                logger_=logger,
                exc_msg=f"relay_photo_db_save_failed dialog_id={dialog_id} from_tg={message.from_user.id}",
                warn_msg="relay_photo_db_save_failed_throttled dialog_id=%s from_tg=%s",
                dialog_id=dialog_id,
                from_tg=message.from_user.id,
            )

        return

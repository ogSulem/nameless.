from __future__ import annotations

import io
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

logger = logging.getLogger(__name__)
router = Router(name="dialog")

async def _get_dialog_id(redis: Redis, tg_id: int) -> int | None:
    v = await redis.get(keys.active_dialog(tg_id))
    return int(v) if v else None


@router.callback_query()
async def ignore_callbacks_during_dialog(call: CallbackQuery, redis: Redis) -> None:
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

    # 1. Clear Redis and prepare rating data for BOTH
    for t in [tg1, tg2]:
        if not t: continue
        await redis.delete(keys.active_dialog(t))
        
        await redis.set(f"pending_rating:{t}", str(dialog_id), ex=60 * 60)
        
        # Appearance rating logic: only if the OTHER person sent a photo with a human
        other_tg = tg2 if t == tg1 else tg1
        has_human_photo = await redis.get(f"appearance_rating_required:{t}:{dialog_id}")
        
        hp = "1" if has_human_photo else "0"
        await redis.set(f"pending_rating_has_photos:{t}", hp, ex=60 * 60)
        await redis.set(f"pending_rating_step:{t}", "chat", ex=60 * 60)

    if tg1 and tg2:
        await redis.set(f"pending_rating_partner:{tg1}", str(tg2), ex=60 * 60)
        await redis.set(f"pending_rating_partner:{tg2}", str(tg1), ex=60 * 60)

        # 2. Assign actions
        a1 = action
        if action == "skip":
            a1, a2 = "skip", "skip"
        else:
            if actor_tg_id == tg1:
                a1, a2 = "end", "skip"
            else:
                a1, a2 = "skip", "end"
        
        await redis.set(f"pending_rating_action:{tg1}", a1, ex=60 * 60)
        await redis.set(f"pending_rating_action:{tg2}", a2, ex=60 * 60)

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
            await redis.set(f"ui:rating_message_id:{t}", str(msg.message_id), ex=60 * 60)
            logger.info("SENT_RATING_UI_OK tg=%s mid=%s", t, msg.message_id)
        except Exception:
            logger.exception("FAILED_SEND_RATING_UI tg=%s", t)

    logger.info("dialog_finished_complete dialog_id=%s", dialog_id)

    logger.info("dialog_finished dialog_id=%s action=%s", dialog_id, action)


@router.message(F.text.in_({"⏭️", "🛑"}))
async def dialog_finish_text(message: Message, session: AsyncSession, redis: Redis) -> None:
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

    res_d = await session.execute(select(Dialog).where(Dialog.id == dialog_id))
    dialog = res_d.scalar_one_or_none()
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

    res = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
    me = res.scalar_one_or_none()
    if me is None:
        return

    res_d = await session.execute(select(Dialog).where(Dialog.id == dialog_id))
    dialog = res_d.scalar_one_or_none()
    if dialog is None or dialog.status != DialogStatus.active:
        await redis.delete(keys.active_dialog(message.from_user.id))
        raise SkipHandler

    partner_user_id = dialog.user2_id if dialog.user1_id == me.id else dialog.user1_id
    res_p = await session.execute(select(User.telegram_id).where(User.id == partner_user_id))
    partner_tg = int(res_p.scalar_one())

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
        txt = (message.text or "")
        await svc.save_text(session, dialog_id, me.id, message.text)
        try:
            await safe_send_message(message.bot, partner_tg, message.text)
            logger.info("relay_text_ok dialog_id=%s from_tg=%s to_tg=%s", dialog_id, message.from_user.id, partner_tg)
        except Exception:
            logger.exception("relay_text_failed dialog_id=%s from_tg=%s to_tg=%s", dialog_id, message.from_user.id, partner_tg)
        return

    if message.photo:
        # 1. Forward to partner
        try:
            await safe_send_photo(message.bot, partner_tg, photo=message.photo[-1].file_id)
            logger.info("relay_photo_ok dialog_id=%s from_tg=%s to_tg=%s", dialog_id, message.from_user.id, partner_tg)
        except Exception:
            logger.exception("relay_photo_failed dialog_id=%s from_tg=%s to_tg=%s", dialog_id, message.from_user.id, partner_tg)

        # AI Vision check: offline face detection (OpenCV)
        has_human = False
        ai_meta: dict[str, Any] | None = None
        try:
            # Check if we already detected a human from this sender in this dialog to save CPU
            redis_key = f"dialog:{dialog_id}:sender:{message.from_user.id}:human_detected"
            already_detected = await redis.get(redis_key)
            
            if already_detected == "1":
                has_human = True
                ai_meta = {"cached": True}
                logger.info("AI human detection skipped (already detected in this dialog)")
            else:
                # Download photo to memory for AI analysis
                dest = io.BytesIO()
                photo = message.photo[-1]
                await message.bot.download(photo, destination=dest)
                dest.seek(0)
                
                # Call AI Service (offline)
                ai_service = AIService()
                is_human, ai_meta = await ai_service.detect_human_with_meta(dest.read())
                if is_human:
                    has_human = True
                    # Remember that this sender has sent a human photo in this dialog
                    await redis.set(redis_key, "1", ex=3600) # 1 hour expiry
                    # IMPORTANT: Set the global flag that this dialog REQUIRES appearance rating from PARTNER
                    await redis.set(f"appearance_rating_required:{partner_tg}:{dialog_id}", "1", ex=3600)
                    logger.info("AI human detected! Flag set for partner_tg=%s", partner_tg)

            # Prepare admin alert info
            username = escape_markdown(message.from_user.username or "NoTag")
            full_name = escape_markdown(message.from_user.full_name or "Unknown")
            rating = float(me.season_rating_chat or 0.0)
            
            # Prepare partner info
            res_p_full = await session.execute(select(User).where(User.telegram_id == partner_tg))
            partner_user = res_p_full.scalar_one_or_none()
            
            partner_label = f"{partner_tg}"
            if partner_user:
                p_name = f" ({partner_user.city})" if partner_user.city else ""
                p_tag = f" @{partner_user.username}" if partner_user.username else ""
                p_full = f" {partner_user.full_name}" if partner_user.full_name else ""
                partner_label = f"{p_full}{p_tag} [ID: {partner_tg}]{p_name}".strip()

            # Improved caption for admin
            caption = (
                f"📸 From: {full_name} (@{username}) [ID: {message.from_user.id}] [Rating: {rating:.1f}] "
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
            
            # Ensure the channel ID is correctly formatted for Telegram (-100 prefix)
            # The user says it is stored with -100, but the error "chat not found" 
            # often happens if the value is interpreted as a string or missing the prefix.
            target_chat_id = settings.alerts_chat_id
            chat_id_str = str(target_chat_id)
            if not chat_id_str.startswith("-100"):
                if chat_id_str.startswith("-"):
                    target_chat_id = int("-100" + chat_id_str[1:])
                else:
                    target_chat_id = int("-100" + chat_id_str)
            
            logger.info("forward_photo_to_admin attempt chat_id=%s", target_chat_id)

            await message.bot.send_photo(
                chat_id=target_chat_id,
                photo=message.photo[-1].file_id,
                caption=caption
            )
        except Exception:
            logger.exception("failed_forward_photo_to_admin alerts_chat_id=%s", settings.alerts_chat_id)

        # 3. Save reference to DB (without local storage if possible, but keeping DB record for tracking)
        await svc.save_photo(message.bot, session, dialog_id, me.id, message.from_user.id, message)

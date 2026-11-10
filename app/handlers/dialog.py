from __future__ import annotations

import logging
from datetime import UTC, datetime

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message, ReplyKeyboardRemove
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Dialog
from app.flows.pending import RATING_TTL_S
from app.keyboards.dialog_reply import NEXT, STOP
from app.keyboards.rating import rating_kb
from app.keyboards.start import back_kb
from app.redis import keys
from app.services.dialog import DialogInfo, DialogService
from app.services.vision import FaceDetector
from app.telegram_safe import (
    UserBlockedBot,
    safe_copy_message,
    safe_delete_message,
    safe_edit_message_reply_markup,
    safe_send_message,
)
from app.ui import (
    background,
    clear_ui_message_id,
    edit_ui,
    get_ui_message_id,
    notice,
    set_ui_message_id,
)
from app.utils.ratelimit import hit, rate_limit

logger = logging.getLogger(__name__)
router = Router(name="dialog")

_MEDIA_FIELDS = ("photo", "video", "voice", "video_note", "audio", "document", "sticker", "animation")


def _has_media(message: Message) -> bool:
    return any(getattr(message, field, None) for field in _MEDIA_FIELDS)


def _ratable(message: Message) -> bool:
    return bool(message.text) or _has_media(message)


# ------------------------------------------------------------------- finishing
async def finish_dialog(
    bot: Bot,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    dialog_id: int,
    actor_tg: int,
    *,
    action: str = "end",
    note: str | None = None,
) -> list[int]:
    """Close a dialog and open the anonymous rating flow for both participants.

    `note` is prepended to the rating screen (e.g. "собеседник недоступен") so a
    real explanation never replaces the chance to rate.
    """
    svc = DialogService(redis, settings)
    telegram_ids = await svc.finish(session, dialog_id, actor_tg_id=actor_tg) or []
    if not telegram_ids:
        return []

    dialog = await session.get(Dialog, dialog_id)
    had_photos = bool(dialog and dialog.has_photos)
    appearance_targets = await _appearance_targets(redis, dialog_id, telegram_ids, had_photos)

    try:
        pipe = redis.pipeline(transaction=False)
        for tg in telegram_ids:
            other = next((x for x in telegram_ids if x != tg), None)
            pipe.set(keys.pending_rating(tg), str(dialog_id), ex=RATING_TTL_S)
            pipe.set(keys.pending_rating_step(tg), "chat", ex=RATING_TTL_S)
            pipe.set(keys.pending_rating_has_photos(tg), "1" if tg in appearance_targets else "0", ex=RATING_TTL_S)
            pipe.set(keys.pending_rating_action(tg), action if tg == actor_tg else "end", ex=RATING_TTL_S)
            if other is not None:
                pipe.set(keys.pending_rating_partner(tg), str(other), ex=RATING_TTL_S)
                pipe.set(keys.last_partner(tg), str(other), ex=600)
            pipe.delete(keys.ui_search_message_id(tg))
        await pipe.execute()
    except Exception:
        logger.exception("finish_dialog_state_failed dialog_id=%s", dialog_id)

    for tg in telegram_ids:
        # One card in, one card out. The rating screen has to be a *new* message:
        # only a sent message can drop the "⏭️/🛑" reply keyboard (an edit cannot).
        # Its text therefore goes out complete on the first frame, and the buttons
        # are attached right after — the user never sees an intermediate screen.
        old_mid = await get_ui_message_id(redis, tg)
        if old_mid:
            await safe_delete_message(bot, tg, old_mid)
            await clear_ui_message_id(redis, tg)

        try:
            msg = await safe_send_message(
                bot,
                tg,
                (note + "\n\n" if note else "")
                + "⭐ <b>Насколько комфортным было общение?</b>\n\n"
                "Оценка анонимна: собеседник не увидит, кто её поставил.\n"
                "Можно пропустить — тогда просто вернётесь в профиль.",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode="HTML",
            )
        except UserBlockedBot:
            continue
        except Exception:
            logger.exception("finish_notify_failed tg=%s", tg)
            continue

        if msg is not None:
            await set_ui_message_id(redis, tg, msg.message_id)
            await safe_edit_message_reply_markup(bot, tg, msg.message_id, rating_kb("rate:chat"))

    logger.info("dialog_finished dialog_id=%s actor=%s action=%s", dialog_id, actor_tg, action)
    return telegram_ids


async def _appearance_targets(
    redis: Redis, dialog_id: int, telegram_ids: list[int], had_photos: bool
) -> set[int]:
    """Only users who actually *received a photo with a person* may rate appearance."""
    if not had_photos:
        return set()
    targets: set[int] = set()
    for tg in telegram_ids:
        try:
            flag = await redis.get(keys.appearance_rating_required(tg, dialog_id))
        except Exception:
            flag = None
        if flag:
            targets.add(tg)
    return targets


# --------------------------------------------------------------------- buttons
@router.message(F.text.in_({NEXT, STOP, "⏭️", "🛑"}))
async def finish_by_button(message: Message, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if message.from_user is None:
        raise SkipHandler

    tg_id = int(message.from_user.id)
    action = "next" if message.text in {NEXT, "⏭️"} else "end"

    if not await rate_limit(redis, key=keys.rate(f"finish:{tg_id}"), ttl_s=2):
        return

    dialog_id = await DialogService(redis, settings).active_dialog_id(session, tg_id)
    await _delete(message)

    if not dialog_id:
        await edit_ui(
            message.bot, redis, tg_id,
            "Сейчас нет активного диалога.",
            kb=back_kb("menu:profile", "← В профиль"),
        )
        return

    await finish_dialog(message.bot, session, redis, settings, dialog_id, tg_id, action=action)



# ----------------------------------------------------------------------- relay
@router.message()
async def relay_message(message: Message, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    if message.from_user is None or message.chat is None:
        raise SkipHandler

    tg_id = int(message.from_user.id)
    svc = DialogService(redis, settings)
    dialog_id = await svc.active_dialog_id(session, tg_id)
    if not dialog_id:
        raise SkipHandler

    if not _ratable(message):
        # A contact, location or poll: copy_message cannot relay it, and dropping
        # it silently made people think the bot was broken.
        await _delete(message)
        await notice(
            message.bot,
            redis,
            tg_id,
            "Такое переслать нельзя. Можно текстом, фото, видео, голосовым или стикером.",
        )
        return

    info = await svc.dialog_info(session, dialog_id, tg_id)
    if info is None or info.me is None:
        raise SkipHandler
    me = info.me

    now = datetime.now(tz=UTC)
    if me.is_banned:
        await _delete(message)
        await notice(message.bot, redis, tg_id, "🚫 Аккаунт заблокирован.")
        return
    if me.muted_until is not None:
        muted_until = me.muted_until if me.muted_until.tzinfo else me.muted_until.replace(tzinfo=UTC)
        if muted_until > now:
            await _delete(message)
            await notice(message.bot, redis, tg_id, "🔇 Отправка сообщений ограничена модерацией.")
            return

    allowed, _ = await hit(
        redis, key=keys.rate(f"msg:{tg_id}"), limit=int(settings.messages_per_10s), window_s=10
    )
    if not allowed:
        await _delete(message)
        await notice(message.bot, redis, tg_id, "⏳ Слишком быстро. Дайте собеседнику прочитать.")
        return

    if message.photo:
        photo_allowed, _ = await hit(
            redis, key=keys.rate(f"photo:{tg_id}"), limit=int(settings.photos_per_minute), window_s=60
        )
        if not photo_allowed:
            await _delete(message)
            await notice(message.bot, redis, tg_id, "⏳ Слишком много фото подряд. Подождите минуту.")
            return

    try:
        await safe_copy_message(
            message.bot,
            chat_id=info.partner_telegram_id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except UserBlockedBot:
        await _delete(message)
        await finish_dialog(
            message.bot,
            session,
            redis,
            settings,
            dialog_id,
            tg_id,
            action="end",
            note="Собеседник больше не может получать сообщения — диалог завершён.",
        )
        return
    except Exception:
        logger.exception("relay_failed dialog_id=%s from=%s", dialog_id, tg_id)
        await notice(message.bot, redis, tg_id, "Сообщение не доставлено. Попробуйте ещё раз.")
        return

    # Only after a successful delivery: "typing…" next to nothing would be a lie.
    background(_typing(
        message.bot, redis, info.partner_telegram_id, "upload_photo" if message.photo else "typing"
    ))

    await svc.heartbeat(info)
    await _delete(message)

    try:
        if message.text:
            await svc.save_text(session, dialog_id, info.me_db_id, message.text[:4096])
        elif message.photo:
            await svc.save_photo(session, dialog_id, info.me_db_id, message.photo[-1].file_id)
            background(
                _photo_pipeline(message.bot, redis, settings, info, message.photo[-1].file_id, tg_id)
            )
    except Exception:
        logger.exception("relay_persist_failed dialog_id=%s", dialog_id)


async def _mark_face_seen(redis: Redis, info: DialogInfo) -> None:
    try:
        await redis.set(
            keys.appearance_rating_required(info.partner_telegram_id, info.dialog_id), "1", ex=3600
        )
    except Exception:
        logger.warning("face_flag_failed dialog_id=%s", info.dialog_id)


async def _photo_pipeline(
    bot: Bot,
    redis: Redis,
    settings: Settings,
    info: DialogInfo,
    file_id: str,
    sender_tg: int,
) -> None:
    """Unlock the appearance rating only when the partner received a photo with a face."""
    cache_key = keys.sender_human_detected(info.dialog_id, sender_tg)
    try:
        if await redis.get(cache_key):
            await _mark_face_seen(redis, info)
            return

        detector = FaceDetector.shared(settings)
        if not detector.available:
            return

        verdict = await detector.check_telegram_photo(bot, file_id)
        logger.info(
            "face_verdict dialog_id=%s from=%s has_face=%s backend=%s faces=%s ms=%s",
            info.dialog_id, sender_tg, verdict.has_face, verdict.backend, verdict.faces, verdict.ms,
        )
        if verdict.has_face:
            try:
                await redis.set(cache_key, "1", ex=3600)
            except Exception:
                pass
            await _mark_face_seen(redis, info)
    except Exception:
        logger.exception("photo_pipeline_failed dialog_id=%s", info.dialog_id)


@router.edited_message()
async def edited_message(message: Message, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    """Edits are not relayed — say so instead of leaving the partner with stale text."""
    if message.from_user is None:
        return
    tg_id = int(message.from_user.id)
    svc = DialogService(redis, settings)
    dialog_id = await svc.active_dialog_id(session, tg_id)
    if not dialog_id:
        return
    info = await svc.dialog_info(session, dialog_id, tg_id)
    if info is None:
        return
    await notice(
        message.bot,
        redis,
        tg_id,
        "Исправления не доходят до собеседника — отправьте сообщение заново.",
    )


# --------------------------------------------------------------------- helpers
async def _delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def _typing(bot: Bot, redis: Redis, partner_tg: int, action: str = "typing") -> None:
    """Show the partner "is typing…" — the cheapest way to make a relay feel alive.

    Throttled to one action per 4 seconds per chat and never allowed to break the
    relay itself.
    """
    try:
        if not await rate_limit(redis, key=keys.rate(f"typing:{partner_tg}"), ttl_s=4):
            return
        await bot.send_chat_action(chat_id=partner_tg, action=action)
    except Exception:
        logger.debug("chat_action_failed tg=%s", partner_tg)

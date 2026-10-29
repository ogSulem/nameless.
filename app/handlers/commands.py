from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import ActiveDialog, Gender, User
from app.flows.profile import age_of, invalidate_profile, show_profile
from app.flows.states import ProfileEdit, Registration
from app.keyboards.profile import delete_confirm_kb, legal_kb
from app.keyboards.start import back_kb, city_kb, consent_kb, gender_kb, seek_gender_kb
from app.redis import keys
from app.services import admin as admin_service
from app.services.city_validator import sanitize_city
from app.services.dialog import DialogService
from app.services.matchmaking import MatchmakingService
from app.telegram_safe import safe_answer_callback
from app.texts import help_text, privacy_text, rules_text, terms_text
from app.ui import edit_ui, send_new_ui

logger = logging.getLogger(__name__)
router = Router(name="commands")


# --------------------------------------------------------------------- helpers
async def get_user(session: AsyncSession, tg_id: int) -> User | None:
    res = await session.execute(select(User).where(User.telegram_id == int(tg_id)))
    return res.scalar_one_or_none()


def parse_birth_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


async def caretaker_purge(bot: Bot, session: AsyncSession, redis: Redis, settings: Settings, tg_id: int) -> None:
    """Delete every trace of a user (152-ФЗ right to erasure)."""
    svc = DialogService(redis, settings)
    dialog_id = await svc.active_dialog_id(session, tg_id)
    if dialog_id:
        tgs = await svc.finish(session, dialog_id, actor_tg_id=tg_id)
        for other in tgs or []:
            if other != tg_id:
                try:
                    await bot.send_message(other, "Диалог завершён: собеседник удалил профиль.")
                except Exception:
                    pass

    user = await get_user(session, tg_id)
    if user is not None:
        await session.execute(delete(ActiveDialog).where(ActiveDialog.user_id == user.id))
        await session.delete(user)  # cascades to dialogs/messages/photos/ratings/complaints
        await session.commit()

    try:
        pipe = redis.pipeline(transaction=False)
        for key in (
            keys.profile_text(tg_id),
            keys.ui_message_id(tg_id),
            keys.ui_rating_message_id(tg_id),
            keys.ui_search_message_id(tg_id),
            keys.active_dialog(tg_id),
            keys.pending_rating(tg_id),
            keys.pending_rating_partner(tg_id),
            keys.pending_rating_step(tg_id),
            keys.pending_rating_has_photos(tg_id),
            keys.pending_rating_action(tg_id),
            keys.queue_member_key(tg_id),
            keys.seek_gender_key(tg_id),
            keys.user_id_by_tg(tg_id),
            keys.user_rating_chat_by_tg(tg_id),
            keys.online_key(tg_id),
        ):
            pipe.delete(key)
        await pipe.execute()
    except Exception:
        logger.warning("purge_redis_failed tg=%s", tg_id, exc_info=True)

    await admin_service.log_action(session, admin_tg=0, action="user_self_deleted", target_tg=tg_id)
    logger.info("user_deleted tg=%s", tg_id)


async def _delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


async def _store_seek(redis: Redis, tg_id: int, seek: str, fallback_gender: str) -> None:
    value = seek if seek in {"male", "female"} else ("female" if fallback_gender == "male" else "male")
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.set(keys.seek_gender_key(tg_id), value, ex=60 * 60 * 24 * 365)
        pipe.set(f"seek:display:{tg_id}", seek, ex=60 * 60 * 24 * 365)
        await pipe.execute()
    except Exception:
        logger.warning("store_seek_failed tg=%s", tg_id, exc_info=True)


# ---------------------------------------------------------------------- start
@router.message(CommandStart())
async def cmd_start(
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    redis: Redis,
    settings: Settings,
    bot: Bot,
) -> None:
    await _delete(message)
    if message.from_user is None:
        return

    tg_id = int(message.from_user.id)
    await MatchmakingService(redis, settings).mark_online(tg_id)

    user = await get_user(session, tg_id)

    if user is not None:
        if user.is_banned:
            await send_new_ui(
                bot, redis, tg_id,
                "🚫 <b>Аккаунт заблокирован.</b>\n\n"
                f"Причина: {escape(user.ban_reason or 'нарушение правил')}\n\n"
                + (f"Поддержка: {escape(settings.support_contact)}" if settings.support_contact else ""),
            )
            return

        current = await state.get_state()
        if current in {Registration.gender.state, Registration.birth_date.state,
                       Registration.city.state, Registration.seek.state}:
            # User already registered but the flow was interrupted: just finish it.
            await state.clear()
        dialog_id = await DialogService(redis, settings).active_dialog_id(session, tg_id)
        if dialog_id:
            await send_new_ui(
                bot, redis, tg_id,
                "Сейчас у вас идёт диалог.\n\n"
                "«⏭️ Следующий» — завершить и искать нового собеседника\n"
                "«🛑 Завершить» — выйти в профиль",
            )
            return

        await state.clear()
        await show_profile(bot, redis, session, settings, tg_id)
        return

    await state.clear()
    await state.set_state(Registration.consent)
    await send_new_ui(
        bot,
        redis,
        tg_id,
        "<b>Добро пожаловать в Nameless</b> 💎\n\n"
        "Анонимный чат: собеседник не видит ваш профиль и контакты.\n\n"
        f"{rules_text()}\n\n"
        f"<i>Нажимая кнопку ниже, вы подтверждаете, что вам {settings.min_age}+, и принимаете "
        "условия и политику обработки данных.</i>",
        kb=consent_kb(),
    )
    logger.info("registration_start tg=%s", tg_id)


@router.callback_query(Registration.consent, F.data == "consent:accept")
async def consent_accept(
    call: CallbackQuery, state: FSMContext, redis: Redis, session: AsyncSession, settings: Settings
) -> None:
    tg_id = int(call.from_user.id)
    user = await get_user(session, tg_id)
    if user is not None:
        await state.clear()
        await show_profile(call.bot, redis, session, settings, tg_id)
        await safe_answer_callback(call)
        return

    await state.update_data(
        consent_at=datetime.now(tz=UTC).isoformat(),
        consent_version=settings.terms_version,
    )
    await state.set_state(Registration.gender)
    await edit_ui(call.bot, redis, tg_id, "<b>Шаг 1 из 4.</b> Ваш пол:", kb=gender_kb())
    await safe_answer_callback(call)


@router.callback_query(Registration.gender, F.data.startswith("reg:gender:"))
async def reg_gender(call: CallbackQuery, state: FSMContext, redis: Redis) -> None:
    gender = call.data.rsplit(":", 1)[-1]
    if gender not in {"male", "female"}:
        await safe_answer_callback(call, "Некорректный выбор")
        return
    await state.update_data(gender=gender)
    await state.set_state(Registration.birth_date)
    await edit_ui(
        call.bot,
        redis,
        int(call.from_user.id),
        "<b>Шаг 2 из 4.</b> Дата рождения?\n\n"
        "Формат: <code>ДД.ММ.ГГГГ</code>, например <code>14.05.1998</code>.\n"
        "<i>Нужна только для возраста — другие видят лишь число лет.</i>",
    )
    await safe_answer_callback(call)


@router.message(Registration.birth_date)
async def reg_birth_date(message: Message, state: FSMContext, redis: Redis, settings: Settings) -> None:
    tg_id = int(message.from_user.id)
    await _delete(message)

    birth = parse_birth_date(message.text or "")
    if birth is None:
        await edit_ui(
            message.bot, redis, tg_id,
            "Не похоже на дату 🤔\n\nВведите в формате <code>ДД.ММ.ГГГГ</code>, например <code>14.05.1998</code>.",
        )
        return

    age = age_of(birth)
    if age < settings.min_age:
        await edit_ui(
            message.bot, redis, tg_id,
            f"🚫 Сервис доступен только с {settings.min_age} лет.\n\n"
            "Если ошиблись — введите дату заново в формате <code>ДД.ММ.ГГГГ</code>.",
        )
        return
    if age > 99:
        await edit_ui(
            message.bot, redis, tg_id,
            "Проверьте дату — кажется, там опечатка. Формат: <code>ДД.ММ.ГГГГ</code>.",
        )
        return

    await state.update_data(birth_date=birth.isoformat())
    await state.set_state(Registration.city)
    await edit_ui(
        message.bot, redis, tg_id,
        "<b>Шаг 3 из 4.</b> Ваш город?\n\n"
        "Напишите название — искать будем сначала в вашем городе, потом по всей стране.\n"
        "Или сразу выберите поиск по всей стране.",
        kb=city_kb(),
    )


@router.callback_query(Registration.city, F.data == "reg:city:global")
async def reg_city_global(
    call: CallbackQuery, state: FSMContext, redis: Redis, session: AsyncSession, settings: Settings
) -> None:
    await _finish_city(call.bot, redis, state, session, settings, int(call.from_user.id), city=None)
    await safe_answer_callback(call)


@router.message(Registration.city)
async def reg_city_text(
    message: Message, state: FSMContext, redis: Redis, session: AsyncSession, settings: Settings
) -> None:
    tg_id = int(message.from_user.id)
    await _delete(message)

    city = sanitize_city(message.text or "")
    if city is None:
        await edit_ui(
            message.bot, redis, tg_id,
            "Не получилось разобрать название города.\n\n"
            "Напишите одним словом или через дефис (например, <code>Казань</code> или "
            "<code>Нижний Новгород</code>) — либо выберите поиск по всей стране.",
            kb=city_kb(),
        )
        return

    await _finish_city(message.bot, redis, state, session, settings, tg_id, city=city)


async def _finish_city(
    bot: Bot,
    redis: Redis,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    tg_id: int,
    city: str | None,
) -> None:
    await state.update_data(city=city)
    await state.set_state(Registration.seek)
    await edit_ui(bot, redis, tg_id, "<b>Шаг 4 из 4.</b> Кого ищем?", kb=seek_gender_kb())


@router.callback_query(Registration.seek, F.data.startswith("reg:seek:"))
async def reg_seek(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> None:
    tg_id = int(call.from_user.id)
    seek = call.data.rsplit(":", 1)[-1]
    if seek not in {"male", "female", "any"}:
        await safe_answer_callback(call, "Некорректный выбор")
        return

    data = await state.get_data()
    try:
        gender = str(data["gender"])
        birth_date = datetime.fromisoformat(str(data["birth_date"])).date()
    except (KeyError, ValueError):
        await state.clear()
        await send_new_ui(call.bot, redis, tg_id, "Сессия регистрации истекла. Нажмите /start.")
        await safe_answer_callback(call)
        return

    consent_at = data.get("consent_at")
    consent_version = data.get("consent_version") or settings.terms_version

    user = await get_user(session, tg_id)
    if user is None:
        user = User(
            telegram_id=tg_id,
            gender=Gender(gender),
            birth_date=birth_date,
            city=data.get("city"),
            consent_at=datetime.fromisoformat(consent_at) if consent_at else datetime.now(tz=UTC),
            consent_version=str(consent_version),
            last_seen_at=datetime.now(tz=UTC),
        )
        session.add(user)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            if await get_user(session, tg_id) is None:
                await send_new_ui(call.bot, redis, tg_id, "Не удалось завершить регистрацию. Попробуйте /start.")
                await safe_answer_callback(call)
                return

    await _store_seek(redis, tg_id, seek, fallback_gender=gender)
    await state.clear()
    logger.info("registration_done tg=%s city=%s seek=%s", tg_id, user.city, seek)

    await show_profile(call.bot, redis, session, settings, tg_id, new_message=True)
    await safe_answer_callback(call, "Готово! Ищем?")


# ------------------------------------------------------------------ main menu
@router.callback_query(F.data == "menu:profile")
async def menu_profile(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    await show_profile(call.bot, redis, session, settings, int(call.from_user.id))
    await safe_answer_callback(call)


@router.callback_query(F.data == "menu:help")
async def menu_help(call: CallbackQuery, redis: Redis, settings: Settings) -> None:
    await edit_ui(call.bot, redis, int(call.from_user.id), help_text(settings), kb=back_kb())
    await safe_answer_callback(call)


@router.callback_query(F.data == "menu:legal")
async def menu_legal(call: CallbackQuery, redis: Redis) -> None:
    await edit_ui(
        call.bot, redis, int(call.from_user.id),
        "<b>⚙️ Данные и документы</b>\n\n"
        "Что хранится, сколько и как удалить — ниже.",
        kb=legal_kb(),
    )
    await safe_answer_callback(call)


@router.callback_query(F.data.startswith("legal:"))
async def legal_docs(call: CallbackQuery, redis: Redis, settings: Settings) -> None:
    what = call.data.rsplit(":", 1)[-1]
    if what == "terms":
        await edit_ui(call.bot, redis, int(call.from_user.id), terms_text(settings),
                      kb=back_kb("menu:legal", "← Назад"))
    elif what == "privacy":
        await edit_ui(call.bot, redis, int(call.from_user.id), privacy_text(settings),
                      kb=back_kb("menu:legal", "← Назад"))
    else:
        await safe_answer_callback(call)
        return
    await safe_answer_callback(call)


@router.callback_query(F.data == "account:delete_ask")
async def delete_ask(call: CallbackQuery, redis: Redis) -> None:
    await edit_ui(
        call.bot, redis, int(call.from_user.id),
        "<b>🗑 Удалить профиль?</b>\n\n"
        "Будут удалены профиль, все диалоги, сообщения, фотографии и оценки.\n"
        "Действие необратимо.\n\n"
        "<i>Сведения о платежах хранятся в сроки, установленные законом.</i>",
        kb=delete_confirm_kb(),
    )
    await safe_answer_callback(call)


@router.callback_query(F.data == "account:delete_confirm")
async def delete_confirm(call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    tg_id = int(call.from_user.id)
    await caretaker_purge(call.bot, session, redis, settings, tg_id)
    await send_new_ui(
        call.bot, redis, tg_id,
        "Профиль и данные удалены. Спасибо, что заглянули 💎\n\nНажмите /start, чтобы вернуться.",
    )
    await safe_answer_callback(call, "Данные удалены")


# ---------------------------------------------------------------- profile edit
@router.callback_query(F.data == "profile:city")
async def edit_city_start(call: CallbackQuery, state: FSMContext, redis: Redis) -> None:
    await state.set_state(ProfileEdit.city)
    await edit_ui(
        call.bot, redis, int(call.from_user.id),
        "<b>🏙 Смена города</b>\n\n"
        "Напишите название — искать будем сначала в вашем городе, потом по всей стране.",
        kb=city_kb("profile:city:global"),
    )
    await safe_answer_callback(call)


@router.callback_query(ProfileEdit.city, F.data == "profile:city:global")
async def edit_city_global(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    await _save_city(call.bot, redis, state, session, settings, int(call.from_user.id), None, call=call)
    await safe_answer_callback(call, "Ищем по всей стране")


@router.message(ProfileEdit.city)
async def edit_city_text(
    message: Message, state: FSMContext, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    tg_id = int(message.from_user.id)
    await _delete(message)

    city = sanitize_city(message.text or "")
    if city is None:
        await edit_ui(
            message.bot, redis, tg_id,
            "Не получилось разобрать название города.\n\n"
            "Напишите одним словом или через дефис, либо выберите поиск по всей стране.",
            kb=city_kb("profile:city:global"),
        )
        return

    await _save_city(message.bot, redis, state, session, settings, tg_id, city)


async def _save_city(
    bot: Bot,
    redis: Redis,
    state: FSMContext,
    session: AsyncSession,
    settings: Settings,
    tg_id: int,
    city: str | None,
    *,
    call: CallbackQuery | None = None,
) -> None:
    user = await get_user(session, tg_id)
    if user is None:
        await state.clear()
        if call is not None:
            await safe_answer_callback(call, "Профиль не найден")
            return
        await edit_ui(bot, redis, tg_id, "Нажмите /start, чтобы начать.")
        return

    user.city = city
    await session.commit()
    await state.clear()

    # The queue holds the old city; re-queueing under the new one avoids ghost matches.
    svc = MatchmakingService(redis, settings)
    await svc.dequeue(tg_id)
    await invalidate_profile(redis, tg_id)
    await show_profile(bot, redis, session, settings, tg_id, new_message=call is None)


@router.callback_query(F.data == "profile:seek")
async def edit_seek_start(call: CallbackQuery, state: FSMContext, redis: Redis) -> None:
    await state.set_state(ProfileEdit.seek)
    await edit_ui(
        call.bot, redis, int(call.from_user.id),
        "<b>👀 Кого ищем?</b>\n\nВыбор влияет на подбор собеседников.",
        kb=seek_gender_kb("pfseek"),
    )
    await safe_answer_callback(call)


@router.callback_query(F.data.startswith("pfseek:"))
async def edit_seek_choice(
    call: CallbackQuery,
    state: FSMContext,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> None:
    tg_id = int(call.from_user.id)
    seek = (call.data or "").rsplit(":", 1)[-1]
    user = await get_user(session, tg_id)
    if user is None or seek not in {"male", "female", "any"}:
        await state.clear()
        await safe_answer_callback(call, "Профиль не найден")
        return

    await _store_seek(redis, tg_id, seek, fallback_gender=getattr(user.gender, "value", user.gender))
    await state.clear()
    await MatchmakingService(redis, settings).dequeue(tg_id)
    await show_profile(call.bot, redis, session, settings, tg_id)
    await safe_answer_callback(call, "Сохранено")


# ------------------------------------------------------------------- commands
@router.message(Command("help"))
async def cmd_help(message: Message, redis: Redis, settings: Settings) -> None:
    await _delete(message)
    await send_new_ui(message.bot, redis, int(message.from_user.id), help_text(settings))


@router.message(Command("terms"))
async def cmd_terms(message: Message, redis: Redis, settings: Settings) -> None:
    await _delete(message)
    await send_new_ui(message.bot, redis, int(message.from_user.id), terms_text(settings))


@router.message(Command("privacy"))
async def cmd_privacy(message: Message, redis: Redis, settings: Settings) -> None:
    await _delete(message)
    await send_new_ui(message.bot, redis, int(message.from_user.id), privacy_text(settings))


@router.message(Command("rules"))
async def cmd_rules(message: Message, redis: Redis) -> None:
    await _delete(message)
    await send_new_ui(message.bot, redis, int(message.from_user.id), rules_text())


@router.message(Command("premium"))
async def cmd_premium(
    message: Message, redis: Redis, session: AsyncSession, settings: Settings, state: FSMContext
) -> None:
    from app.handlers.subscription import open_premium

    await _delete(message)
    await open_premium(message.bot, redis, session, settings, int(message.from_user.id), state)


@router.message(Command("delete_me"))
async def cmd_delete_me(message: Message, redis: Redis) -> None:
    await _delete(message)
    await edit_ui(
        message.bot, redis, int(message.from_user.id),
        "<b>🗑 Удалить профиль и все данные?</b>\n\nДействие необратимо.",
        kb=delete_confirm_kb(),
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    from app.handlers.dialog import finish_dialog

    await _delete(message)
    tg_id = int(message.from_user.id)
    svc = DialogService(redis, settings)

    dialog_id = await svc.active_dialog_id(session, tg_id)
    if dialog_id:
        await finish_dialog(message.bot, session, redis, settings, dialog_id, tg_id, action="end")
        await show_profile(message.bot, redis, session, settings, tg_id)
        return

    await MatchmakingService(redis, settings).dequeue(tg_id)
    try:
        await redis.delete(keys.ui_search_message_id(tg_id))
    except Exception:
        pass
    await show_profile(message.bot, redis, session, settings, tg_id)


@router.message(Command("admin"))
async def cmd_admin(
    message: Message, redis: Redis, settings: Settings, state: FSMContext, session: AsyncSession
) -> None:
    from app.access import role_for
    from app.handlers.admin import show_admin_menu

    await _delete(message)
    if message.from_user is None:
        return
    role = role_for(settings, message.from_user.id)
    if role is None:
        await send_new_ui(message.bot, redis, int(message.from_user.id), "Команда недоступна.")
        return
    await state.clear()
    await show_admin_menu(message.bot, redis, session, settings, int(message.from_user.id))

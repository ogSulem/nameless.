from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Gender, User
from app.flows.profile import age_of, birth_date_from_age, show_profile
from app.flows.states import ProfileEdit, Registration
from app.keyboards.profile import delete_confirm_kb, legal_kb
from app.keyboards.start import back_kb, city_kb, consent_kb, gender_kb, seek_gender_kb
from app.redis import keys
from app.services import account
from app.services import admin as admin_service
from app.services.city_validator import sanitize_city
from app.services.dialog import DialogService
from app.services.matchmaking import MatchmakingService, seek_gender_for
from app.telegram_safe import safe_answer_callback, safe_delete_message, safe_send_message
from app.texts import days_label, help_text, privacy_text, rules_text, terms_text
from app.ui import edit_ui, get_ui_message_id, notice, send_new_ui

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


def parse_age(raw: str) -> int | None:
    """Accept `25` — and, out of politeness, a birth date from people who type one."""
    text = (raw or "").strip()
    if text.isdigit() and len(text) <= 3:
        return int(text)
    birth = parse_birth_date(text)
    if birth is None:
        return None
    return age_of(birth)


async def caretaker_purge(bot: Bot, session: AsyncSession, redis: Redis, settings: Settings, tg_id: int) -> None:
    """Delete every trace of a user (152-ФЗ right to erasure)."""
    partners = await account.purge_user(session, redis, settings, tg_id)
    for other in partners:
        try:
            # Replaces the partner card: the chat keeps a single screen.
            await send_new_ui(
                bot,
                redis,
                other,
                "Диалог завершён: собеседник удалил профиль.\n\n"
                "Нажмите /start, чтобы найти нового.",
                kb=back_kb("search:start", "🔎 Найти собеседника"),
            )
        except Exception:
            logger.info("purge_notify_failed tg=%s", other)

    await admin_service.log_action(session, admin_tg=0, action="user_self_deleted", target_tg=tg_id)
    logger.info("user_deleted tg=%s", tg_id)


async def _delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


def _seek_value(seek: str, fallback_gender: str) -> str:
    """`any` is stored as itself; anything unexpected falls back to the opposite gender."""
    return seek if seek in {"male", "female", "any"} else seek_gender_for(fallback_gender)


async def _store_seek(
    session: AsyncSession,
    redis: Redis,
    tg_id: int,
    seek: str,
    fallback_gender: str,
    user: User | None = None,
) -> None:
    """Persist the preference in the DB (source of truth) and warm the Redis cache.

    `user` is passed in when the caller already has the row: registration used to
    read the brand-new account twice in a row.
    """
    value = _seek_value(seek, fallback_gender)
    if user is None:
        user = await get_user(session, tg_id)
    if user is not None and user.seek_gender != value:
        user.seek_gender = value
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            logger.warning("store_seek_db_failed tg=%s", tg_id, exc_info=True)
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.set(keys.seek_gender_key(tg_id), value, ex=60 * 60 * 24 * 365)
        pipe.set(keys.seek_display_key(tg_id), value, ex=60 * 60 * 24 * 365)
        await pipe.execute()
    except Exception:
        logger.warning("store_seek_redis_failed tg=%s", tg_id, exc_info=True)


def _consent_text(settings: Settings) -> str:
    return (
        "<b>Добро пожаловать в Nameless</b> 💎\n\n"
        "Анонимный чат: собеседник не видит ваш профиль и контакты.\n\n"
        f"{rules_text()}\n\n"
        f"<i>Нажимая кнопку ниже, вы подтверждаете, что вам {settings.min_age}+, и принимаете "
        "условия и политику обработки данных.</i>"
    )


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
    inviter = None
    if user is None:
        candidate = _parse_invite(message.text or "")
        if candidate is not None and candidate != tg_id:
            inviter = candidate

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
        if current in {Registration.gender.state, Registration.age.state,
                       Registration.city.state, Registration.seek.state}:
            # User already registered but the flow was interrupted: just finish it.
            await state.clear()
        dialog_id = await DialogService(redis, settings).active_dialog_id(session, tg_id)
        if dialog_id:
            # The dialog screen is the message that carries the conversation:
            # replacing it with a hint would delete what the person is reading.
            await notice(
                bot, redis, tg_id,
                "Сейчас у вас идёт диалог — просто напишите сообщение.\n"
                "<i>«⏭️ Следующий» — другой собеседник, «🛑 Завершить» — в профиль.</i>",
            )
            return

        await state.clear()
        await show_profile(bot, redis, session, settings, tg_id)
        return

    await state.clear()
    await state.set_state(Registration.consent)
    if inviter is not None:
        # Written *after* clear(), otherwise the invite payload is wiped.
        await state.update_data(invited_by=inviter)
    await send_new_ui(bot, redis, tg_id, _consent_text(settings), kb=consent_kb())
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


@router.callback_query(Registration.consent, F.data == "consent:back")
async def consent_back(
    call: CallbackQuery, state: FSMContext, redis: Redis, session: AsyncSession, settings: Settings
) -> None:
    """Legal documents are opened from the consent screen; they must lead back to it."""
    tg_id = int(call.from_user.id)
    if await get_user(session, tg_id) is not None:
        await state.clear()
        await show_profile(call.bot, redis, session, settings, tg_id)
        await safe_answer_callback(call)
        return
    await state.set_state(Registration.consent)
    await edit_ui(call.bot, redis, tg_id, _consent_text(settings), kb=consent_kb())
    await safe_answer_callback(call)


@router.callback_query(Registration.gender, F.data.startswith("reg:gender:"))
async def reg_gender(call: CallbackQuery, state: FSMContext, redis: Redis) -> None:
    gender = call.data.rsplit(":", 1)[-1]
    if gender not in {"male", "female"}:
        await safe_answer_callback(call, "Некорректный выбор")
        return
    await state.update_data(gender=gender)
    await state.set_state(Registration.age)
    await edit_ui(
        call.bot,
        redis,
        int(call.from_user.id),
        "<b>Шаг 2 из 4.</b> Сколько вам лет?\n\n"
        "Напишите числом, например <code>25</code>.\n\n"
        "<i>Другие видят только число лет, дату рождения мы не спрашиваем.</i>",
    )
    await safe_answer_callback(call)


@router.message(Registration.age)
async def reg_age(message: Message, state: FSMContext, redis: Redis, settings: Settings) -> None:
    tg_id = int(message.from_user.id)
    await _delete(message)

    age = parse_age(message.text or "")
    if age is None:
        await edit_ui(
            message.bot, redis, tg_id,
            "Не похоже на возраст 🤔\n\nНапишите число, например <code>25</code>.",
        )
        return

    if age < settings.min_age:
        await edit_ui(
            message.bot, redis, tg_id,
            f"🚫 Сервис доступен только с {settings.min_age} лет.\n\n"
            f"Если ошиблись — напишите возраст ещё раз (например, <code>{settings.min_age}</code>).",
        )
        return
    if age > 99:
        await edit_ui(
            message.bot, redis, tg_id,
            "Проверьте возраст — кажется, там опечатка. Напишите число, например <code>25</code>.",
        )
        return

    await state.update_data(age=age)
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
        birth_date = birth_date_from_age(int(data["age"]))
    except (KeyError, ValueError):
        await state.clear()
        await send_new_ui(call.bot, redis, tg_id, "Сессия регистрации истекла. Нажмите /start.")
        await safe_answer_callback(call)
        return

    consent_at = data.get("consent_at")
    consent_version = data.get("consent_version") or settings.terms_version
    invited_by = data.get("invited_by")
    invited_by = int(invited_by) if isinstance(invited_by, int | str) and str(invited_by).isdigit() else None
    if invited_by == tg_id:
        invited_by = None

    user = await get_user(session, tg_id)
    if user is None:
        user = User(
            telegram_id=tg_id,
            gender=Gender(gender),
            birth_date=birth_date,
            city=data.get("city"),
            seek_gender=_seek_value(seek, gender),
            consent_at=datetime.fromisoformat(consent_at) if consent_at else datetime.now(tz=UTC),
            consent_version=str(consent_version),
            last_seen_at=datetime.now(tz=UTC),
            invited_by=invited_by,
        )
        session.add(user)
        try:
            await session.commit()
        except Exception:
            await session.rollback()
            user = await get_user(session, tg_id)
            if user is None:
                await send_new_ui(call.bot, redis, tg_id, "Не удалось завершить регистрацию. Попробуйте /start.")
                await safe_answer_callback(call)
                return

    await _store_seek(session, redis, tg_id, seek, fallback_gender=gender, user=user)
    await state.clear()
    logger.info("registration_done tg=%s city=%s seek=%s", tg_id, user.city, seek)

    if invited_by:
        await _pay_referral(call.bot, redis, session, settings, inviter_tg=invited_by, invitee=user)

    await show_profile(call.bot, redis, session, settings, tg_id, user=user)
    await safe_answer_callback(call, "Готово! Ищем?")


# ------------------------------------------------------------------ main menu
@router.callback_query(F.data == "menu:profile")
async def menu_profile(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    # Coming home abandons whatever half-finished flow was running: otherwise the
    # next message the person types is eaten as a city / promo code / e-mail.
    await state.clear()
    await show_profile(call.bot, redis, session, settings, int(call.from_user.id))
    await safe_answer_callback(call)


@router.callback_query(F.data == "menu:help")
async def menu_help(call: CallbackQuery, state: FSMContext, redis: Redis, settings: Settings) -> None:
    await state.clear()
    await edit_ui(call.bot, redis, int(call.from_user.id), help_text(settings), kb=back_kb())
    await safe_answer_callback(call)


@router.callback_query(F.data == "menu:legal")
async def menu_legal(call: CallbackQuery, state: FSMContext, redis: Redis) -> None:
    await state.clear()
    await edit_ui(
        call.bot, redis, int(call.from_user.id),
        "<b>⚙️ Данные и документы</b>\n\n"
        "Что хранится, сколько и как удалить — ниже.",
        kb=legal_kb(),
    )
    await safe_answer_callback(call)


@router.callback_query(F.data.startswith("legal:"))
async def legal_docs(
    call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    # Deliberately keeps the FSM state: the consent screen links here, and
    # "← К регистрации" must find the flow it came from.
    what = call.data.rsplit(":", 1)[-1]
    if what not in {"terms", "privacy"}:
        await safe_answer_callback(call)
        return

    tg_id = int(call.from_user.id)
    # Before registration there is no profile to go back to — return to consent.
    back = (
        back_kb("menu:legal", "← Назад")
        if await get_user(session, tg_id) is not None
        else back_kb("consent:back", "← К регистрации")
    )
    text = terms_text(settings) if what == "terms" else privacy_text(settings)
    await edit_ui(call.bot, redis, tg_id, text, kb=back)
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
    # Purging wipes the UI pointer, so remember it first: the old screen is deleted
    # and the farewell becomes the only message in the chat.
    mid = await get_ui_message_id(redis, tg_id)
    await caretaker_purge(call.bot, session, redis, settings, tg_id)
    if mid:
        await safe_delete_message(call.bot, tg_id, mid)
    await send_new_ui(
        call.bot, redis, tg_id,
        "Профиль и данные удалены. Спасибо, что заглянули 💎\n\nНажмите /start, чтобы вернуться.",
        delete_previous=False,
    )
    await safe_answer_callback(call, "Данные удалены")


# -------------------------------------------------------------------- referrals
def _parse_invite(text: str) -> int | None:
    """`/start ref_123456` → 123456."""
    parts = (text or "").split(maxsplit=1)
    if len(parts) != 2:
        return None
    payload = parts[1].strip()
    if not payload.startswith("ref_"):
        return None
    raw = payload[4:]
    return int(raw) if raw.isdigit() else None


def invite_link(bot_username: str, tg_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{int(tg_id)}"


async def _pay_referral(
    bot: Bot,
    redis: Redis,
    session: AsyncSession,
    settings: Settings,
    *,
    inviter_tg: int,
    invitee: User,
) -> None:
    """Give the inviter Premium days for a friend who actually registered.

    Guards: the inviter must exist and not be banned; the bonus is paid once per
    invitee (`invite_rewarded`), so a replayed /start cannot farm days.
    """
    days = int(settings.referral_reward_days)
    if invitee.invite_rewarded:
        return
    if days <= 0 and int(settings.referral_welcome_days) <= 0:
        return

    inviter = await get_user(session, inviter_tg)
    if inviter is None or inviter.is_banned or int(inviter.telegram_id) == int(invitee.telegram_id):
        return

    from app.services import admin as admin_service
    from app.services.subscription import SubscriptionService

    # Anti-farming: a SIM farm cannot turn into a month of Premium per minute.
    cap = int(settings.referral_max_per_day)
    if cap > 0:
        today = datetime.now(tz=UTC).strftime("%Y%m%d")
        cap_key = keys.referral_cap_key(inviter_tg, today)
        try:
            paid_today = int(await redis.incr(cap_key))
            if paid_today == 1:
                await redis.expire(cap_key, 172800)
            if paid_today > cap:
                logger.info("referral_cap_hit inviter=%s", inviter_tg)
                return
        except Exception:
            logger.warning("referral_cap_failed inviter=%s", inviter_tg, exc_info=True)

    invitee.invite_rewarded = True
    await session.commit()

    welcome = int(settings.referral_welcome_days)

    if days > 0:
        until = await SubscriptionService().extend_subscription(
            session=session, telegram_id=inviter_tg, days=days
        )
        await MatchmakingService(redis, settings).invalidate_premium(inviter_tg)
        await admin_service.log_action(
            session,
            admin_tg=0,
            action="referral_bonus",
            target_tg=inviter_tg,
            details=f"+{days}d за приглашённого {invitee.telegram_id}",
        )
        try:
            # Replaces the inviter's current screen, so the chat stays at one message.
            await send_new_ui(
                bot,
                redis,
                inviter_tg,
                f"🎁 <b>+{days_label(days)} Premium</b> за приглашённого друга!\n\n"
                f"Теперь Premium активен до <b>{until:%d.%m.%Y}</b>.",
                kb=back_kb("menu:profile"),
            )
        except Exception:
            logger.info("referral_notify_failed tg=%s", inviter_tg)

    # The invited person gets a taste too — off by default, on for launch week.
    if welcome > 0:
        try:
            await SubscriptionService().extend_subscription(
                session=session, telegram_id=int(invitee.telegram_id), days=welcome
            )
            await MatchmakingService(redis, settings).invalidate_premium(int(invitee.telegram_id))
            await safe_send_message(
                bot,
                int(invitee.telegram_id),
                f"🎁 Вам <b>{welcome} дней Premium</b> — приветственный бонус за вход по ссылке друга.\n\n"
                "Подбор Premium настраивается в профиле.",
            )
        except Exception:
            logger.info("referral_welcome_failed tg=%s", invitee.telegram_id)

    logger.info(
        "referral_paid inviter=%s invitee=%s days=%s welcome=%s",
        inviter_tg,
        invitee.telegram_id,
        days,
        welcome,
    )


async def _invite_screen(bot: Bot, redis: Redis, session: AsyncSession, settings: Settings, tg_id: int) -> None:
    try:
        me = await bot.me()
        username = me.username or ""
    except Exception:
        username = ""
    if not username:
        await edit_ui(
            bot, redis, tg_id,
            "🎁 <b>Приглашения</b>\n\nСсылка-приглашение пока недоступна. "
            "Перешлите другу @username бота.",
            kb=back_kb("menu:profile"),
        )
        return

    invited = int(
        (
            await session.execute(
                select(func.count(User.id)).where(User.invited_by == tg_id)
            )
        ).scalar_one()
        or 0
    )
    reward = int(settings.referral_reward_days)
    await edit_ui(
        bot, redis, tg_id,
        "🎁 <b>Пригласите друга</b>\n\n"
        f"За каждого нового друга, который зарегистрируется по ссылке, вы получаете "
        f"<b>+{days_label(reward)} Premium</b>.\n\n"
        f"Уже приглашено: <b>{invited}</b>\n\n"
        f"<code>{invite_link(username, tg_id)}</code>\n\n"
        "<i>Награда начисляется один раз за каждого нового пользователя.</i>",
        kb=back_kb("menu:profile"),
    )


@router.message(Command("invite"))
async def cmd_invite(
    message: Message, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    await _delete(message)
    await _invite_screen(message.bot, redis, session, settings, int(message.from_user.id))


@router.callback_query(F.data == "menu:invite")
async def menu_invite(
    call: CallbackQuery, state: FSMContext, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    await state.clear()
    await _invite_screen(call.bot, redis, session, settings, int(call.from_user.id))
    await safe_answer_callback(call)


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
    await show_profile(bot, redis, session, settings, tg_id)


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

    await _store_seek(
        session, redis, tg_id, seek, fallback_gender=getattr(user.gender, "value", user.gender)
    )
    await state.clear()
    await MatchmakingService(redis, settings).dequeue(tg_id)
    await show_profile(call.bot, redis, session, settings, tg_id)
    await safe_answer_callback(call, "Сохранено")


# ------------------------------------------------------------------- commands
async def _info_screen(
    bot: Bot,
    redis: Redis,
    session: AsyncSession,
    settings: Settings,
    tg_id: int,
    text: str,
) -> None:
    """Show a reference text without wrecking what is currently on screen.

    In a dialog the partner card must survive, and during registration the consent
    screen must survive — there we answer with a self-deleting notice. Otherwise
    the screen is simply edited in place: no delete, no re-send, no flicker.
    """
    user = await get_user(session, tg_id)
    busy = user is not None and bool(
        await DialogService(redis, settings).active_dialog_id(session, tg_id)
    )
    if user is None or busy:
        await notice(bot, redis, tg_id, text, ttl_s=45)
        return
    await edit_ui(bot, redis, tg_id, text, kb=back_kb("menu:profile"))


async def _command_info(message: Message, session: AsyncSession, redis: Redis, settings: Settings, text: str) -> None:
    if message.from_user is None:
        return
    await _delete(message)
    await _info_screen(message.bot, redis, session, settings, int(message.from_user.id), text)


@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    await _command_info(message, session, redis, settings, help_text(settings))


@router.message(Command("terms"))
async def cmd_terms(message: Message, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    await _command_info(message, session, redis, settings, terms_text(settings))


@router.message(Command("privacy"))
async def cmd_privacy(message: Message, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    await _command_info(message, session, redis, settings, privacy_text(settings))


@router.message(Command("rules"))
async def cmd_rules(message: Message, session: AsyncSession, redis: Redis, settings: Settings) -> None:
    await _command_info(message, session, redis, settings, rules_text())


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

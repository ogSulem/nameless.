from __future__ import annotations

import logging
from datetime import date, datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Dialog, DialogStatus, Gender, User
from app.keyboards.start import gender_kb, skip_city_kb
from app.keyboards.start_reply import start_reply_kb
from app.flows.profile import show_profile
from app.redis import keys
from app.ui import edit_ui, get_ui_message_id, send_new_ui
from app.telegram_safe import safe_delete_message, safe_edit_message_reply_markup, safe_edit_message_text
from app.services.city_validator import normalize_city_name, is_valid_city, get_canonical_city_name

logger = logging.getLogger(__name__)
router = Router(name="start")


@router.callback_query()
async def ignore_callbacks_during_dialog(call: CallbackQuery, redis: Redis) -> None:
    if await redis.get(keys.active_dialog(call.from_user.id)):
        await call.answer()
        return

    raise SkipHandler


class RegistrationStates(StatesGroup):
    gender = State()
    birth_date = State()
    city = State()


async def _handle_start(message: Message, session: AsyncSession, state: FSMContext, redis: Redis) -> None:
    tg_id = message.from_user.id

    current_state = await state.get_state()
    if current_state is not None:
        try:
            await message.delete()
        except Exception:
            pass
        return

    pending_rating = await redis.get(f"pending_rating:{tg_id}")
    if pending_rating:
        try:
            await message.delete()
        except Exception:
            pass
        return

    dialog_id_raw = await redis.get(keys.active_dialog(tg_id))
    searching_msg_id = await redis.get(f"ui:search_message_id:{tg_id}")
    if dialog_id_raw or searching_msg_id:
        try:
            await message.delete()
        except Exception:
            pass
        return

    res = await session.execute(select(User).where(User.telegram_id == tg_id))
    user = res.scalar_one_or_none()

    if user is not None:
        await show_profile(message.bot, redis, session, tg_id)
        await state.clear()
        return

    # Store basic user info for DB
    await state.update_data(
        username=message.from_user.username,
        full_name=message.from_user.full_name
    )

    await state.set_state(RegistrationStates.gender)
    await edit_ui(message.bot, redis, tg_id, "Выбери пол:", kb=gender_kb())
    logger.info("registration_start tg_id=%s", tg_id)


@router.message(CommandStart())
async def start_cmd(message: Message, session: AsyncSession, state: FSMContext, redis: Redis) -> None:
    await _handle_start(message, session, state, redis)


@router.message(F.text.regexp(r"^/start(@[A-Za-z0-9_]+)?$") )
async def start_cmd_text(message: Message, session: AsyncSession, state: FSMContext, redis: Redis) -> None:
    await _handle_start(message, session, state, redis)


@router.message(RegistrationStates.gender)
async def reg_gender_text_ignore(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(RegistrationStates.gender, F.data.in_({"male", "female"}))
async def reg_gender(call: CallbackQuery, state: FSMContext, redis: Redis) -> None:
    await state.update_data(gender=call.data)
    await state.set_state(RegistrationStates.birth_date)

    # Reply keyboard cannot be attached via edit, so we replace UI message with a new one.
    # This keeps chat clean: previous UI message is deleted, and user sees only the last bot message.
    mid = await get_ui_message_id(redis, call.from_user.id)
    if mid:
        await safe_delete_message(call.bot, call.from_user.id, mid)

    await send_new_ui(call.bot, redis, call.from_user.id, "Введи дату рождения (ДД.ММ.ГГГГ)", kb=start_reply_kb())
    await call.answer()


@router.message(RegistrationStates.birth_date)
async def reg_birth_date(message: Message, state: FSMContext, redis: Redis) -> None:
    tg_id = message.from_user.id

    raw = (message.text or "").strip()
    try:
        birth_date = datetime.strptime(raw, "%d.%m.%Y").date()
    except Exception:
        try:
            await message.delete()
        except Exception:
            pass
        return

    today = date.today()
    age = today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))
    if age < 16 or age > 99:
        try:
            await message.delete()
        except Exception:
            pass
        return

    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(birth_date=birth_date.isoformat())
    await state.set_state(RegistrationStates.city)

    mid = await get_ui_message_id(redis, tg_id)
    if mid:
        try:
            # Some clients/flows are finicky when editing a message that was originally sent
            # with a reply keyboard. Edit text first (no markup), then attach inline keyboard.
            ok = await safe_edit_message_text(
                message.bot,
                tg_id,
                mid,
                "Укажи город или выбери глобальный поиск:",
            )
            if not ok:
                raise TelegramBadRequest("edit_failed")
            try:
                await safe_edit_message_reply_markup(
                    message.bot,
                    tg_id,
                    mid,
                    reply_markup=skip_city_kb(),
                )
            except TelegramBadRequest:
                pass
            return
        except TelegramBadRequest:
            logger.error("reg_birth_date_edit_city_failed_unexpected tg_id=%s mid=%s", message.from_user.id, mid)

        await safe_delete_message(message.bot, tg_id, mid)

    await send_new_ui(message.bot, redis, tg_id, "Укажи город или выбери глобальный поиск:", kb=skip_city_kb())


@router.callback_query(RegistrationStates.city, F.data == "city_global")
async def reg_city_global(call: CallbackQuery, state: FSMContext, session: AsyncSession, redis: Redis) -> None:
    await _finish_registration(call.from_user.id, call.bot, redis, state, session, city=None)
    await call.answer()


@router.message(RegistrationStates.city)
async def reg_city_text(message: Message, state: FSMContext, session: AsyncSession, redis: Redis) -> None:
    tg_id = message.from_user.id
    raw_city = (message.text or "").strip()

    try:
        await message.delete()
    except Exception:
        pass

    if not raw_city:
        mid = await get_ui_message_id(redis, tg_id)
        text = "Город не может быть пустым. Введи город или выбери глобальный поиск:"
        if mid:
            try:
                ok = await safe_edit_message_text(message.bot, tg_id, mid, text, reply_markup=skip_city_kb())
                if ok:
                    return
                return
            except Exception:
                pass
        await send_new_ui(message.bot, redis, tg_id, text, kb=skip_city_kb())
        return

    if not is_valid_city(raw_city):
        mid = await get_ui_message_id(redis, tg_id)
        normalized_error = normalize_city_name(raw_city)
        text = f"Город '{normalized_error}' не найден в базе РФ/СНГ. Попробуй еще раз или выбери глобальный поиск:"
        if mid:
            try:
                ok = await safe_edit_message_text(message.bot, tg_id, mid, text, reply_markup=skip_city_kb())
                if ok:
                    return
                return
            except Exception:
                pass
        await send_new_ui(message.bot, redis, tg_id, text, kb=skip_city_kb())
        return

    normalized = get_canonical_city_name(raw_city)
    
    await _finish_registration(tg_id, message.bot, redis, state, session, city=normalized)


async def _finish_registration(
    tg_id: int,
    bot,
    redis: Redis,
    state: FSMContext,
    session: AsyncSession,
    city: str | None,
) -> None:
    data = await state.get_data()

    res = await session.execute(select(User).where(User.telegram_id == tg_id))
    existing = res.scalar_one_or_none()

    if existing is not None and ("gender" not in data or "birth_date" not in data):
        existing.city = city
        await session.commit()
        await state.clear()
        await show_profile(bot, redis, session, tg_id)
        logger.info("city_changed tg_id=%s", tg_id)
        return

    if existing is not None:
        await state.clear()
        await show_profile(bot, redis, session, tg_id)
        return

    gender = Gender(data["gender"])

    birth_date = datetime.fromisoformat(data["birth_date"]).date()

    user = User(telegram_id=tg_id, gender=gender, birth_date=birth_date, city=city)
    session.add(user)
    await session.commit()

    await state.clear()
    await show_profile(bot, redis, session, tg_id)
    logger.info("registration_done tg_id=%s", tg_id)


@router.callback_query(F.data == "profile_change_city")
async def change_city(call: CallbackQuery, state: FSMContext, redis: Redis) -> None:
    await state.set_state(RegistrationStates.city)
    await edit_ui(call.bot, redis, call.from_user.id, "Укажи новый город или выбери глобальный поиск:", kb=skip_city_kb())
    await call.answer()

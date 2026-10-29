from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.flows.profile import premium_active, show_profile
from app.flows.states import Payment as PaymentStates
from app.handlers.commands import get_user
from app.keyboards.payment import payment_kb
from app.keyboards.start import back_kb
from app.services.matchmaking import MatchmakingService
from app.services.payments import PaymentError, PaymentService
from app.telegram_safe import safe_answer_callback, safe_send_message
from app.ui import edit_ui

logger = logging.getLogger(__name__)
router = Router(name="subscription")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-zА-Яа-я]{2,}$")


def _premium_status_text(settings: Settings, until: datetime | None) -> str:
    if until is None:
        return "не активен"
    aware = until if until.tzinfo else until.replace(tzinfo=UTC)
    if aware <= datetime.now(tz=UTC):
        return "истёк"
    return f"активен до <b>{aware:%d.%m.%Y}</b>"


async def open_premium(
    bot: Bot,
    redis: Redis,
    session: AsyncSession,
    settings: Settings,
    tg_id: int,
    state: FSMContext | None = None,
) -> None:
    user = await get_user(session, tg_id)
    if user is None:
        await edit_ui(bot, redis, tg_id, "Профиль не найден. Нажмите /start.")
        return

    if premium_active(user):
        await edit_ui(
            bot, redis, tg_id,
            "💎 <b>Premium уже активен</b>\n\n"
            f"Статус: {_premium_status_text(settings, user.subscription_until)}\n\n"
            "Что даёт Premium:\n"
            "• приоритет в подборе собеседников с высоким рейтингом;\n"
            "• в первую очередь показываются те, у кого рейтинг общения "
            f"{settings.premium_min_rating:.0f}+.\n\n"
            "Продлить подписку можно после окончания текущей.",
            kb=back_kb("menu:profile"),
        )
        return

    if not settings.payments_enabled:
        await edit_ui(
            bot, redis, tg_id,
            "💎 <b>Premium</b>\n\n"
            "Что даёт:\n"
            "• собеседники с высоким рейтингом в первую очередь;\n"
            "• приоритет в очереди подбора.\n\n"
            "<i>Оплата временно недоступна — загляните позже.</i>",
            kb=back_kb("menu:profile"),
        )
        return

    service = PaymentService(settings, redis)

    if service.telegram_enabled:
        try:
            init = await service.create_telegram_invoice(session, bot, user)
        except PaymentError:
            await edit_ui(
                bot, redis, tg_id,
                "Не удалось создать счёт. Попробуйте позже.",
                kb=back_kb("menu:profile"),
            )
            return

        await edit_ui(
            bot, redis, tg_id,
            _offer_text(settings, user.subscription_until),
            kb=payment_kb(init.url),
        )
        logger.info("premium_invoice_created tg=%s provider=telegram", tg_id)
        return

    # Direct ЮKassa needs a receipt e-mail (54-ФЗ), so we ask for it first.
    if state is not None:
        await state.set_state(PaymentStates.waiting_email)
    await edit_ui(
        bot, redis, tg_id,
        _offer_text(settings, user.subscription_until)
        + "\n\n📧 <b>Укажите e-mail для чека</b> (по 54-ФЗ чек обязателен).\n"
        "Отправьте его одним сообщением.",
        kb=back_kb("menu:profile"),
    )


def _offer_text(settings: Settings, until: datetime | None) -> str:
    return (
        "💎 <b>Premium</b>\n\n"
        f"Цена: <b>{settings.subscription_price_rub} ₽</b> за {settings.subscription_days} дней.\n"
        "Оплата разовая — автопродления нет.\n\n"
        "Что даёт:\n"
        f"• в первую очередь собеседники с рейтингом {settings.premium_min_rating:.0f}+;\n"
        "• приоритет в подборе;\n"
        "• если подходящих по рейтингу сейчас нет — подберём лучшего из доступных.\n\n"
        f"Текущий статус: {_premium_status_text(settings, until)}"
    )


@router.callback_query(F.data == "pay:premium")
async def pay_premium(
    call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings, state: FSMContext
) -> None:
    tg_id = int(call.from_user.id)
    service = PaymentService(settings, redis)

    if service.use_yookassa_direct and not service.telegram_enabled:
        await state.set_state(PaymentStates.waiting_email)
        await edit_ui(
            call.bot, redis, tg_id,
            _offer_text(settings, None)
            + "\n\n📧 <b>Укажите e-mail для чека</b> (по 54-ФЗ чек обязателен).\n"
            "Отправьте его одним сообщением.",
            kb=back_kb("menu:profile"),
        )
        await safe_answer_callback(call)
        return

    await open_premium(call.bot, redis, session, settings, tg_id)
    await safe_answer_callback(call)


@router.message(PaymentStates.waiting_email)
async def payment_email(
    message: Message, state: FSMContext, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    if message.from_user is None:
        return
    tg_id = int(message.from_user.id)
    try:
        await message.delete()
    except Exception:
        pass

    from app.services.dialog import DialogService

    if await DialogService(redis, settings).active_dialog_id(session, tg_id):
        await state.clear()
        await safe_send_message(
            message.bot, tg_id, "Сначала завершите диалог — затем можно оформить Premium."
        )
        return

    email = (message.text or "").strip()
    if not EMAIL_RE.match(email):
        await edit_ui(
            message.bot, redis, tg_id,
            "Похоже, это не e-mail. Пришлите адрес в формате <code>name@example.com</code>.",
            kb=back_kb("menu:profile"),
        )
        return

    await state.clear()
    user = await get_user(session, tg_id)
    if user is None:
        return

    service = PaymentService(settings, redis)
    try:
        bot_info = await message.bot.get_me()
        init = await service.create_yookassa_invoice(session, bot_info.username or "", user, email)
    except PaymentError:
        await edit_ui(
            message.bot, redis, tg_id,
            "Платёжный шлюз недоступен. Попробуйте, пожалуйста, позже.",
            kb=back_kb("menu:profile"),
        )
        return

    await edit_ui(
        message.bot, redis, tg_id,
        _offer_text(settings, user.subscription_until)
        + f"\n\n📧 Чек придёт на <b>{escape(email)}</b>.\n"
        "После оплаты нажмите «Я оплатил — проверить».",
        kb=payment_kb(init.url),
    )
    logger.info("premium_invoice_created tg=%s provider=yookassa", tg_id)


@router.callback_query(F.data == "pay:check")
async def pay_check(
    call: CallbackQuery, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    tg_id = int(call.from_user.id)
    service = PaymentService(settings, redis)

    if not service.use_yookassa_direct:
        await safe_answer_callback(call, "Оплата подтверждается автоматически", show_alert=True)
        return

    state, until = await service.check_yookassa_payment(session, tg_id)

    if state == "paid":
        await MatchmakingService(redis, settings).invalidate_premium(tg_id)
        await safe_answer_callback(call, "✅ Оплата получена, Premium активирован", show_alert=True)
        await safe_send_message(
            call.bot, tg_id,
            "✅ <b>Оплата получена!</b>\n\n"
            f"Premium активирован до <b>{until:%d.%m.%Y}</b>.\nСпасибо за поддержку 💎",
        )
        await show_profile(call.bot, redis, session, settings, tg_id)
        return

    if state == "canceled":
        await safe_answer_callback(call, "Платёж отменён", show_alert=True)
        await open_premium(call.bot, redis, session, settings, tg_id)
        return

    if state == "pending":
        await safe_answer_callback(
            call, "Оплата ещё обрабатывается. Обычно это занимает до минуты.", show_alert=True
        )
        return

    await safe_answer_callback(call, "Нет активного счёта — создайте новый", show_alert=True)
    await open_premium(call.bot, redis, session, settings, tg_id)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(
    message: Message, session: AsyncSession, redis: Redis, settings: Settings
) -> None:
    if message.from_user is None or message.successful_payment is None:
        return

    service = PaymentService(settings, redis)
    applied, until = await service.complete_telegram_payment(session, message.successful_payment)

    if not applied:
        await safe_send_message(
            message.bot, int(message.from_user.id),
            "Платёж получен, но уже был учтён ранее. Если Premium не активировался — напишите в поддержку.",
        )
        return

    await MatchmakingService(redis, settings).invalidate_premium(int(message.from_user.id))
    until_text = f"{until:%d.%m.%Y}" if until else "—"
    await safe_send_message(
        message.bot, int(message.from_user.id),
        "✅ <b>Оплата прошла!</b>\n\n"
        f"Premium активирован до <b>{until_text}</b>.\nСпасибо за поддержку 💎",
    )

    if settings.alerts_chat_id:
        try:
            await safe_send_message(
                message.bot, int(settings.alerts_chat_id),
                f"💰 <b>Оплата Premium</b>\n<code>{message.from_user.id}</code> · "
                f"{message.successful_payment.total_amount / 100:.0f} ₽",
                parse_mode="HTML",
            )
        except Exception:
            logger.warning("payment_alert_failed", exc_info=True)

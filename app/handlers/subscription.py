from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery, InlineKeyboardButton, InlineKeyboardMarkup
import json
import uuid
from app.keyboards.payment import payment_ui_kb
from app.ui import edit_ui
from app.services.yookassa import YookassaService
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.flows.profile import show_profile
from app.redis import keys
from app.services.subscription import SubscriptionService
from app.telegram_safe import safe_delete_message, safe_send_message
from app.utils.markdown import escape_markdown

logger = logging.getLogger(__name__)
router = Router(name="subscription")


async def _send_premium_invoice(message: Message, settings: Settings, redis) -> None:
    if message.from_user is None:
        await message.answer("Не удалось определить пользователя.")
        return

    if message.chat is None or message.chat.type != "private":
        await message.answer("Оплата доступна только в личном чате с ботом.")
        return

    user_id = int(message.from_user.id)

    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        # Fallback to Telegram Payments if direct API keys are missing
        if not settings.payments_provider_token:
            await message.answer("Платежи не настроены.")
            return
        
        # ... existing Telegram Payments logic ...
        price = LabeledPrice(label="Premium 30 days", amount=settings.subscription_price_rub * 100)

        # ЮKassa requires provider_data for receipt if it's a LIVE payment (for Russian laws)
        provider_data = {
            "receipt": {
                "items": [
                    {
                        "description": f"Premium подписка на {settings.subscription_days} дней",
                        "quantity": "1.00",
                        "amount": {
                            "value": str(settings.subscription_price_rub),
                            "currency": "RUB"
                        },
                        "vat_code": 1,
                        "payment_mode": "full_payment",
                        "payment_subject": "service"
                    }
                ]
            }
        }

        try:
            invoice_link = await message.bot.create_invoice_link(
                title="Premium",
                description=f"Подписка на {settings.subscription_days} дней",
                payload="premium_30",
                provider_token=settings.payments_provider_token,
                currency="RUB",
                prices=[price],
                provider_data=json.dumps(provider_data)
            )
        except Exception as e:
            logger.error("telegram_invoice_link_failed error=%s", e)
            await message.answer("Ошибка при создании счета. Попробуйте позже.")
            return
        
        await edit_ui(
            message.bot,
            redis,
            user_id,
            (
                "💎 *Оплата Premium*\n\n"
                "1. Нажмите кнопку **Оплатить** ниже\n"
                "2. Проведите платеж в открывшемся окне\n"
                "3. После оплаты Premium активируется автоматически\n"
            ),
            kb=payment_ui_kb(invoice_link),
        )
        logger.info("payment_invoice_sent tg_id=%s method=%s", user_id, "telegram")
        return

    # Direct Yookassa API
    yoo = YookassaService(settings.yookassa_shop_id, settings.yookassa_secret_key)
    # We need a return_url. For a bot, it can be just a link to the bot itself
    bot_info = await message.bot.get_me()
    return_url = f"https://t.me/{bot_info.username}"
    
    # Check if user is admin for 1 RUB test price
    price = settings.subscription_price_rub
    is_admin = user_id in settings.admins_set
    
    logger.info("payment_init tg_id=%s is_admin=%s admins_count=%s", user_id, is_admin, len(settings.admins_set))
    
    if is_admin:
        price = 1
        logger.info("admin_test_price_applied tg_id=%s", user_id)
    
    invoice_link, payment_id = await yoo.create_payment(
        amount=price,
        description=f"Premium подписка на {settings.subscription_days} дней (Test Price: {price} RUB)" if price == 1 else f"Premium подписка на {settings.subscription_days} дней",
        return_url=return_url,
        metadata={"tg_id": message.from_user.id, "days": settings.subscription_days}
    )
        
    if not invoice_link:
        await message.answer("Ошибка при связи с платежным шлюзом.")
        return
            
    # Store payment_id in Redis to check it later
    await redis.set(keys.payment_pending(user_id), payment_id, ex=3600)

    text = (
        "💎 *Оплата Premium*\n\n"
        "1. Нажмите кнопку **Оплатить** ниже\n"
        "2. Проведите платеж в открывшемся окне\n"
        "3. Вернитесь сюда и нажмите **Обновить статус**\n\n"
        "После успешной оплаты Premium будет активирован мгновенно."
    )

    await edit_ui(message.bot, redis, user_id, text, kb=payment_ui_kb(invoice_link))
    logger.info("payment_invoice_sent tg_id=%s method=%s", user_id, "yookassa_direct")


@router.callback_query(F.data == "menu_premium")
async def premium_menu(call: CallbackQuery, settings: Settings, session: AsyncSession, redis) -> None:
    if call.message is None:
        await call.answer()
        return

    await _send_premium_invoice(call.message, settings, redis)
    await call.answer()


@router.callback_query(F.data == "cancel_payment")
async def cancel_payment(call: CallbackQuery, settings: Settings, session: AsyncSession, redis) -> None:
    await show_profile(call.bot, redis, session, call.from_user.id)
    await call.answer("Оплата отменена")


@router.callback_query(F.data == "check_payment")
async def check_payment(call: CallbackQuery, settings: Settings, session: AsyncSession, redis) -> None:
    if call.from_user is None:
        await call.answer("Не удалось определить пользователя", show_alert=True)
        return

    payment_id = await redis.get(keys.payment_pending(int(call.from_user.id)))
    if isinstance(payment_id, bytes):
        payment_id = payment_id.decode()

    pid = (str(payment_id) if payment_id else "")
    pid_prefix = pid[:6] if pid else ""
    logger.info(
        "payment_check_manual tg_id=%s has_payment_id=%s payment_id_prefix=%s payment_id_len=%s",
        call.from_user.id,
        bool(pid),
        pid_prefix,
        len(pid) if pid else 0,
    )
    
    if settings.yookassa_shop_id and settings.yookassa_secret_key and payment_id:
        yoo = YookassaService(settings.yookassa_shop_id, settings.yookassa_secret_key)
        status, paid = await yoo.get_payment_status(payment_id)
        logger.info("payment_status_received tg_id=%s status=%s paid=%s", call.from_user.id, status, paid)
        
        if status == "succeeded" or paid:
            processed_key = keys.payment_processed("yookassa", str(payment_id))
            ok = await redis.set(processed_key, "1", nx=True, ex=60 * 60 * 24 * 30)
            if ok:
                svc = SubscriptionService()
                await svc.extend_subscription(session=session, telegram_id=call.from_user.id, days=settings.subscription_days)

            await redis.delete(keys.payment_pending(int(call.from_user.id)))
            await call.answer("✅ Оплата подтверждена! Premium активирован.", show_alert=True)
            try:
                await redis.delete(keys.profile_text(call.from_user.id))
            except Exception:
                pass
            await show_profile(call.bot, redis, session, call.from_user.id)
            return
        elif status == "canceled":
            await call.answer("❌ Платеж был отменен.", show_alert=True)
            await redis.delete(keys.payment_pending(int(call.from_user.id)))
            try:
                await redis.delete(keys.profile_text(call.from_user.id))
            except Exception:
                pass
            await show_profile(call.bot, redis, session, call.from_user.id)
            return
        else:
            # Still pending: keep the same UI with updated status
            invoice_link = None
            try:
                invoice_link = call.message.reply_markup.inline_keyboard[0][0].url
            except Exception:
                pass

            text = (
                "💎 *Оплата Premium*\n\n"
                "⏳ **Статус: Платеж еще в обработке или не оплачен.**\n"
                "Мы пока не получили подтверждение от банка. Если вы уже оплатили, пожалуйста, подождите 1-2 минуты.\n\n"
                "1. Нажмите кнопку **Оплатить** ниже\n"
                "2. Проведите платеж в открывшемся окне\n"
                "3. Вернитесь сюда и нажмите **Обновить статус**\n\n"
                "После успешной оплаты Premium будет активирован мгновенно."
            )
            
            try:
                from app.keyboards.payment import payment_ui_kb
                kb = payment_ui_kb(invoice_link) if invoice_link else call.message.reply_markup
                await edit_ui(call.bot, redis, call.from_user.id, text, kb=kb)
            except Exception:
                pass
            
            await call.answer("⏳ Платеж еще не подтвержден")
            return

    # If no pending payment or keys missing, just refresh profile
    await show_profile(call.bot, redis, session, call.from_user.id)
    await call.answer("Профиль обновлен")


@router.pre_checkout_query(F.invoice_payload == "premium_30")
async def pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message, session: AsyncSession, settings: Settings, redis) -> None:
    sp = message.successful_payment
    if sp is None:
        return

    if message.from_user is None:
        return

    if sp.invoice_payload != "premium_30":
        logger.warning(
            "payment_invalid_payload tg_id=%s payload=%s",
            message.from_user.id,
            sp.invoice_payload,
        )
        return

    if sp.currency != "RUB":
        logger.warning(
            "payment_invalid_currency tg_id=%s currency=%s",
            message.from_user.id,
            sp.currency,
        )
        return

    expected_amount = settings.subscription_price_rub * 100
    if sp.total_amount != expected_amount:
        logger.warning(
            "payment_invalid_amount tg_id=%s amount=%s expected=%s",
            message.from_user.id,
            sp.total_amount,
            expected_amount,
        )
        return

    # Idempotency: Telegram can resend successful_payment updates.
    processed_key = keys.payment_processed("telegram", str(sp.telegram_payment_charge_id))
    ok = await redis.set(processed_key, "1", nx=True, ex=60 * 60 * 24 * 30)

    until = None
    if ok:
        svc = SubscriptionService()
        until = await svc.extend_subscription(session=session, telegram_id=message.from_user.id, days=settings.subscription_days)
    else:
        logger.info("payment_already_processed tg_id=%s charge_id=%s", message.from_user.id, sp.telegram_payment_charge_id)

    await message.answer("Оплата прошла успешно. Premium активирован.")
    logger.info(
        "payment_success tg_id=%s amount=%s currency=%s charge_id=%s",
        message.from_user.id,
        sp.total_amount,
        sp.currency,
        sp.telegram_payment_charge_id,
    )

    if settings.alerts_chat_id:
        try:
            username = f"@{message.from_user.username}" if getattr(message.from_user, "username", None) else ""
            full_name = (getattr(message.from_user, "full_name", None) or "").strip()
            name_part = f"{full_name} " if full_name else ""
            until_txt = until.isoformat() if until else "-"
            user_label = escape_markdown(f"{name_part}{username}".strip())
            await safe_send_message(
                message.bot,
                int(settings.alerts_chat_id),
                "\n".join(
                    [
                        "*PREMIUM PURCHASE*",
                        f"User: {user_label} (id: `{message.from_user.id}`, tg://user?id={message.from_user.id})",
                        f"Until: `{until_txt}`",
                        f"Amount: `{sp.total_amount}` `{sp.currency}`",
                        f"Charge: `{sp.telegram_payment_charge_id}`",
                    ]
                ),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("failed_send_premium_purchase_alert tg_id=%s", message.from_user.id)

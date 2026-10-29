from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.types import LabeledPrice
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Payment, User
from app.redis import keys
from app.services.subscription import SubscriptionService
from app.services.yookassa import YookassaError, YookassaService

logger = logging.getLogger(__name__)

PAYLOAD_PREFIX = "premium"


class PaymentError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class PaymentInit:
    url: str | None
    payment_id: str
    provider: str


class PaymentService:
    """One place where money is involved, with a DB ledger.

    Previous version trusted a Redis key keyed by the *bot's* id and a hardcoded
    payload, so any price change silently swallowed payments. Now every payment is
    a DB row, verified by amount/currency, and marked paid exactly once.
    """

    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        *,
        gateway: YookassaService | None = None,
    ) -> None:
        self._settings = settings
        self._redis = redis
        self._gateway = gateway

    @property
    def yookassa(self) -> YookassaService:
        if self._gateway is None:
            self._gateway = YookassaService(
                self._settings.yookassa_shop_id, self._settings.yookassa_secret_key
            )
        return self._gateway

    @property
    def use_yookassa_direct(self) -> bool:
        return bool(self._settings.yookassa_shop_id and self._settings.yookassa_secret_key)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self._settings.payments_provider_token)

    @property
    def enabled(self) -> bool:
        return self.use_yookassa_direct or self.telegram_enabled

    # ---------------------------------------------------------------- telegram
    async def create_telegram_invoice(self, session: AsyncSession, bot: Bot, user: User) -> PaymentInit:
        if not self.telegram_enabled:
            raise PaymentError("telegram_payments_not_configured")

        external_id = uuid.uuid4().hex
        payload = f"{PAYLOAD_PREFIX}:{external_id}"

        payment = Payment(
            provider="telegram",
            external_id=external_id,
            telegram_id=int(user.telegram_id),
            amount=self._settings.premium_price_kopecks,
            currency="RUB",
            days=int(self._settings.subscription_days),
            status="pending",
            description="Premium-подписка",
        )
        session.add(payment)
        await session.commit()

        price = LabeledPrice(label=f"Premium на {self._settings.subscription_days} дней",
                            amount=self._settings.premium_price_kopecks)
        # Receipt data for the provider (Telegram forwards it to ЮKassa).
        provider_data = {
            "receipt": {
                "items": [
                    {
                        "description": f"Premium на {self._settings.subscription_days} дней",
                        "quantity": "1.00",
                        "amount": {"value": f"{self._settings.subscription_price_rub}.00", "currency": "RUB"},
                        "vat_code": 1,
                        "payment_mode": "full_payment",
                        "payment_subject": "service",
                    }
                ]
            }
        }

        try:
            url = await bot.create_invoice_link(
                title="Premium",
                description=f"Подписка Premium на {self._settings.subscription_days} дней",
                payload=payload,
                provider_token=self._settings.payments_provider_token,
                currency="RUB",
                prices=[price],
                provider_data=json.dumps(provider_data),
            )
        except Exception as exc:
            logger.exception("telegram_invoice_failed tg=%s", user.telegram_id)
            payment.status = "failed"
            await session.commit()
            raise PaymentError("invoice_failed") from exc

        await self._remember_pending(int(user.telegram_id), external_id)
        return PaymentInit(url=str(url), payment_id=external_id, provider="telegram")

    async def complete_telegram_payment(self, session: AsyncSession, sp) -> tuple[bool, datetime | None]:
        """Verify + apply a successful_payment update exactly once."""
        payload = (sp.invoice_payload or "").strip()
        parts = payload.split(":")
        external_id = parts[1] if len(parts) == 2 and parts[0] == PAYLOAD_PREFIX else ""
        if not external_id:
            logger.warning("payment_bad_payload payload=%s", payload)
            return False, None

        res = await session.execute(
            select(Payment).where(Payment.provider == "telegram").where(Payment.external_id == external_id)
        )
        payment = res.scalar_one_or_none()
        if payment is None:
            logger.warning("payment_unknown_payload external_id=%s", external_id)
            return False, None

        if payment.status == "paid":
            logger.info("payment_already_paid external_id=%s", external_id)
            return False, payment.paid_at

        expected = self._settings.premium_price_kopecks
        if sp.currency != "RUB" or int(sp.total_amount or 0) != expected or payment.amount != expected:
            logger.error(
                "payment_amount_mismatch external_id=%s got=%s/%s expected=%s",
                external_id, sp.total_amount, sp.currency, expected,
            )
            payment.status = "amount_mismatch"
            await session.commit()
            return False, None

        payment.status = "paid"
        payment.paid_at = datetime.now(tz=UTC)
        await session.commit()

        until = await SubscriptionService().extend_subscription(
            session=session, telegram_id=int(payment.telegram_id), days=int(payment.days)
        )
        await self._forget_pending(int(payment.telegram_id))
        logger.info("payment_paid_tg external_id=%s tg=%s until=%s", external_id, payment.telegram_id, until)
        return True, until

    # ---------------------------------------------------------------- yookassa
    async def create_yookassa_invoice(
        self, session: AsyncSession, bot_username: str, user: User, email: str
    ) -> PaymentInit:
        yoo = self.yookassa
        amount = int(self._settings.subscription_price_rub)
        return_url = f"https://t.me/{bot_username}"

        try:
            url, payment_id = await yoo.create_payment(
                amount_rub=amount,
                description=f"Premium на {self._settings.subscription_days} дней",
                return_url=return_url,
                customer_email=email,
                metadata={"telegram_id": int(user.telegram_id)},
            )
        except YookassaError as exc:
            logger.error("yookassa_invoice_failed tg=%s err=%s", user.telegram_id, exc)
            raise PaymentError("yookassa_unavailable") from exc

        payment = Payment(
            provider="yookassa",
            external_id=payment_id,
            telegram_id=int(user.telegram_id),
            amount=amount * 100,
            currency="RUB",
            days=int(self._settings.subscription_days),
            status="pending",
            description="Premium-подписка",
            receipt_email=email,
        )
        session.add(payment)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()

        await self._remember_pending(int(user.telegram_id), payment_id)
        return PaymentInit(url=url, payment_id=payment_id, provider="yookassa")

    async def check_yookassa_payment(self, session: AsyncSession, telegram_id: int) -> tuple[str, datetime | None]:
        """Poll the gateway. Returns (state, subscription_until) where state ∈ pending/paid/canceled."""
        pending = await self._pending_id(int(telegram_id))
        if not pending:
            return "none", None

        if not self.use_yookassa_direct:
            return "none", None

        try:
            data = await self.yookassa.get_payment(pending)
        except YookassaError:
            return "pending", None

        res = await session.execute(
            select(Payment).where(Payment.provider == "yookassa").where(Payment.external_id == pending)
        )
        payment = res.scalar_one_or_none()
        state, until = await self._settle_yookassa(session, payment, data, telegram_id)
        if state in {"paid", "canceled"}:
            await self._forget_pending(int(telegram_id))
        return state, until

    async def apply_yookassa_notification(
        self, session: AsyncSession, payment_id: str
    ) -> tuple[str, datetime | None]:
        """Handle a ЮKassa webhook by *re-reading* the payment from their API.

        The notification body itself is never trusted: anyone can POST to a public
        URL. We look the payment up ourselves, check the amount, and credit the
        subscription exactly once (a retried webhook must not double the term).
        """
        if not self.use_yookassa_direct or not payment_id:
            return "none", None
        try:
            data = await self.yookassa.get_payment(payment_id)
        except YookassaError:
            logger.warning("yookassa_webhook_refetch_failed id=%s", payment_id)
            return "pending", None

        res = await session.execute(
            select(Payment).where(Payment.provider == "yookassa").where(Payment.external_id == payment_id)
        )
        payment = res.scalar_one_or_none()
        return await self._settle_yookassa(
            session, payment, data, int(payment.telegram_id) if payment else 0
        )

    async def _settle_yookassa(
        self, session: AsyncSession, payment: Payment | None, data: dict, telegram_id: int
    ) -> tuple[str, datetime | None]:
        status = str(data.get("status") or "")

        if status == "succeeded" and bool(data.get("paid")):
            if payment is None:
                logger.warning("yookassa_payment_unknown id=%s", data.get("id"))
                return "none", None
            if payment.status == "paid":
                return "paid", payment.paid_at
            amount = data.get("amount") or {}
            try:
                paid_value = int(round(float(amount.get("value") or 0) * 100))
            except (TypeError, ValueError):
                paid_value = -1
            if paid_value != int(payment.amount) or payment.amount != self._settings.premium_price_kopecks:
                logger.error(
                    "yookassa_amount_mismatch id=%s paid=%s expected=%s",
                    payment.external_id, paid_value, payment.amount,
                )
                payment.status = "amount_mismatch"
                await session.commit()
                return "none", None
            payment.status = "paid"
            payment.paid_at = datetime.now(tz=UTC)
            await session.commit()
            until = await SubscriptionService().extend_subscription(
                session=session, telegram_id=int(payment.telegram_id), days=int(payment.days)
            )
            logger.info(
                "yookassa_payment_paid id=%s tg=%s until=%s", payment.external_id, payment.telegram_id, until
            )
            return "paid", until

        if status == "canceled":
            if payment is not None and payment.status == "pending":
                payment.status = "canceled"
                await session.commit()
            return "canceled", None

        return "pending", None

    # ------------------------------------------------------------------ pending
    async def _remember_pending(self, telegram_id: int, payment_id: str) -> None:
        try:
            await self._redis.set(keys.payment_pending(telegram_id), payment_id, ex=3600)
        except Exception:
            pass

    async def _pending_id(self, telegram_id: int) -> str | None:
        try:
            raw = await self._redis.get(keys.payment_pending(telegram_id))
        except Exception:
            return None
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    async def _forget_pending(self, telegram_id: int) -> None:
        try:
            await self._redis.delete(keys.payment_pending(telegram_id))
        except Exception:
            pass

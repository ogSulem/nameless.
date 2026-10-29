from __future__ import annotations

from datetime import UTC

import pytest
from sqlalchemy import select

from app.database.models import Payment
from app.services.payments import PaymentError, PaymentService


class FakeYoo:
    def __init__(self, status: str = "succeeded", value: str = "499.00", paid: bool = True) -> None:
        self.status = status
        self.value = value
        self.paid = paid
        self.calls = 0

    async def get_payment(self, payment_id: str) -> dict:
        self.calls += 1
        return {
            "id": payment_id,
            "status": self.status,
            "paid": self.paid,
            "amount": {"value": self.value, "currency": "RUB"},
        }


class FakeBot:
    def __init__(self) -> None:
        self.invoices = []

    async def create_invoice_link(self, **kwargs):
        self.invoices.append(kwargs)
        return "https://t.me/$invoice"


class FakeSuccessfulPayment:
    def __init__(self, payload: str, amount: int = 49900, currency: str = "RUB") -> None:
        self.invoice_payload = payload
        self.total_amount = amount
        self.currency = currency


def _settings_with_payments(settings):
    return settings.model_copy(
        update={"payments_provider_token": "provider-token", "subscription_price_rub": 499}
    )


@pytest.mark.asyncio
async def test_telegram_invoice_records_a_db_payment(session, redis, settings, make_user):
    settings = _settings_with_payments(settings)
    user = await make_user(500)
    bot = FakeBot()
    svc = PaymentService(settings, redis)

    init = await svc.create_telegram_invoice(session, bot, user)

    assert init.url == "https://t.me/$invoice"
    assert init.provider == "telegram"
    payment = (await session.execute(select(Payment))).scalar_one()
    assert payment.amount == 49900
    assert payment.telegram_id == 500
    assert payment.status == "pending"
    invoice = bot.invoices[0]
    assert invoice["provider_token"] == "provider-token"
    assert invoice["prices"][0].amount == 49900
    # 54-ФЗ receipt data is attached to the invoice.
    assert '"vat_code"' in invoice["provider_data"]


@pytest.mark.asyncio
async def test_payment_is_applied_exactly_once(session, redis, settings, make_user):
    """A double `successful_payment` (Telegram retries) must not double the term."""
    settings = _settings_with_payments(settings)
    user = await make_user(501)
    svc = PaymentService(settings, redis)
    init = await svc.create_telegram_invoice(session, FakeBot(), user)

    payload = f"premium:{init.payment_id}"
    ok, until = await svc.complete_telegram_payment(session, FakeSuccessfulPayment(payload))
    assert ok is True
    await session.refresh(user)
    first_until = user.subscription_until
    assert first_until is not None

    ok_again, until_again = await svc.complete_telegram_payment(session, FakeSuccessfulPayment(payload))
    assert ok_again is False
    await session.refresh(user)
    assert user.subscription_until == first_until


@pytest.mark.asyncio
async def test_wrong_amount_is_rejected_and_recorded(session, redis, settings, make_user):
    settings = _settings_with_payments(settings)
    user = await make_user(502)
    svc = PaymentService(settings, redis)
    init = await svc.create_telegram_invoice(session, FakeBot(), user)

    ok, _ = await svc.complete_telegram_payment(
        session, FakeSuccessfulPayment(f"premium:{init.payment_id}", amount=1)
    )
    assert ok is False
    await session.refresh(user)
    assert user.subscription_until is None
    payment = (await session.execute(select(Payment))).scalar_one()
    assert payment.status == "amount_mismatch"


@pytest.mark.asyncio
async def test_unknown_payload_is_rejected(session, redis, settings, make_user):
    settings = _settings_with_payments(settings)
    await make_user(503)
    svc = PaymentService(settings, redis)

    assert await svc.complete_telegram_payment(session, FakeSuccessfulPayment("premium:unknown")) == (False, None)
    assert await svc.complete_telegram_payment(session, FakeSuccessfulPayment("garbage")) == (False, None)


@pytest.mark.asyncio
async def test_invoice_requires_configuration(session, redis, settings, make_user):
    user = await make_user(504)
    svc = PaymentService(settings, redis)

    assert svc.enabled is False
    with pytest.raises(PaymentError):
        await svc.create_telegram_invoice(session, FakeBot(), user)


@pytest.mark.asyncio
async def test_yookassa_webhook_credits_once_and_ignores_bad_amount(session, redis, settings, make_user):
    settings = settings.model_copy(
        update={"yookassa_shop_id": "shop", "yookassa_secret_key": "secret", "subscription_price_rub": 499}
    )
    user = await make_user(505)
    svc = PaymentService(settings, redis, gateway=FakeYoo(status="succeeded", value="499.00"))  # type: ignore[arg-type]
    session.add(
        Payment(
            provider="yookassa",
            external_id="yk-1",
            telegram_id=505,
            amount=49900,
            currency="RUB",
            days=30,
            status="pending",
        )
    )
    await session.commit()

    state, until = await svc.apply_yookassa_notification(session, "yk-1")
    assert state == "paid"
    assert until is not None
    await session.refresh(user)
    first_until = user.subscription_until

    state_again, _ = await svc.apply_yookassa_notification(session, "yk-1")
    assert state_again == "paid"
    await session.refresh(user)
    assert user.subscription_until == first_until


@pytest.mark.asyncio
async def test_yookassa_webhook_rejects_underpayment(session, redis, settings, make_user):
    settings = settings.model_copy(
        update={"yookassa_shop_id": "shop", "yookassa_secret_key": "secret", "subscription_price_rub": 499}
    )
    user = await make_user(506)
    svc = PaymentService(settings, redis, gateway=FakeYoo(status="succeeded", value="1.00"))  # type: ignore[arg-type]
    session.add(
        Payment(
            provider="yookassa",
            external_id="yk-2",
            telegram_id=506,
            amount=49900,
            currency="RUB",
            days=30,
            status="pending",
        )
    )
    await session.commit()
    state, _ = await svc.apply_yookassa_notification(session, "yk-2")

    assert state == "none"
    await session.refresh(user)
    assert user.subscription_until is None


@pytest.mark.asyncio
async def test_pending_payment_expires_from_redis(session, redis, settings, make_user):
    settings = _settings_with_payments(settings)
    user = await make_user(507)
    svc = PaymentService(settings, redis)

    await svc.create_telegram_invoice(session, FakeBot(), user)
    assert await svc._pending_id(507) is not None


@pytest.mark.asyncio
async def test_successful_payment_activates_premium_and_profile(bot, dispatcher, session, redis, settings, make_user):
    """Деньги → Premium → карточка профиля: сквозная проверка без Telegram."""
    from datetime import datetime, timezone

    from app.database.models import Payment
    from app.services.payments import PaymentService

    paying = settings.model_copy(
        update={"payments_provider_token": "provider-token"}
    )
    user = await make_user(9080)
    svc = PaymentService(paying, redis)

    class FakeBot:
        async def create_invoice_link(self, **kwargs):
            return "https://t.me/$invoice"

    init = await svc.create_telegram_invoice(session, FakeBot(), user)  # type: ignore[arg-type]
    payment = (await session.execute(select(Payment))).scalar_one()
    assert payment.status == "pending"

    ok, until = await svc.complete_telegram_payment(
        session, FakeSuccessfulPayment(f"premium:{init.payment_id}")
    )
    assert ok is True and until is not None

    await session.refresh(user)
    assert user.subscription_until is not None
    assert payment.status == "paid"
    assert payment.paid_at is not None
    assert payment.paid_at <= datetime.now(tz=UTC)

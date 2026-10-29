from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.models import AdminAudit, PromoCode, PromoRedemption, User
from app.services import promo as promo_service
from tests.conftest import press, send_text
from tests.test_referrals import _register_tg


async def _make_code(session, **kwargs) -> PromoCode:
    code, error = await promo_service.create_code(session, **kwargs)
    assert error is None, error
    assert code is not None
    return code


def test_normalize_accepts_messy_input():
    assert promo_service.normalize("  start30 ") == "START30"
    assert promo_service.normalize("promo 30") == "PROMO30"
    assert promo_service.normalize(None) == ""


def test_generate_code_is_readable_and_valid():
    code = promo_service.generate_code()
    assert promo_service.CODE_RE.match(code)
    tail = code.split("-", 1)[1]
    assert "0" not in tail and "O" not in tail  # no lookalikes for support tickets
    assert "1" not in tail and "I" not in tail
    assert promo_service.generate_code() != code


@pytest.mark.asyncio
async def test_create_code_validates_input(session):
    _, error = await promo_service.create_code(session, code="AB", days=30)
    assert error and "3–24" in error

    _, error = await promo_service.create_code(session, code="OK30", days=0)
    assert error and "дней" in error

    _, error = await promo_service.create_code(session, code="OK30", days=30, max_uses=0)
    assert error and "активаций" in error

    first = await _make_code(session, code="OK30", days=30)
    assert first.code == "OK30"

    _, error = await promo_service.create_code(session, code="ok30", days=10)
    assert error and "уже есть" in error


@pytest.mark.asyncio
async def test_redeem_extends_premium_and_counts_once(session, make_user):
    user = await make_user(8100)
    await _make_code(session, code="HELLO30", days=30, max_uses=5)

    result = await promo_service.redeem(session, telegram_id=8100, raw_code="hello30")

    assert result.ok and result.days == 30
    await session.refresh(user)
    assert user.subscription_until is not None
    until = user.subscription_until
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    assert until > datetime.now(tz=UTC) + timedelta(days=29)

    code = (await session.execute(select(PromoCode).where(PromoCode.code == "HELLO30"))).scalar_one()
    assert code.used_count == 1

    # Same person, same code: no second month.
    again = await promo_service.redeem(session, telegram_id=8100, raw_code="HELLO30")
    assert again.status is promo_service.RedeemStatus.already_used
    await session.refresh(code)
    assert code.used_count == 1


@pytest.mark.asyncio
async def test_redeem_unknown_and_malformed(session, make_user):
    await make_user(8101)
    assert (
        await promo_service.redeem(session, telegram_id=8101, raw_code="NOPE")
    ).status is promo_service.RedeemStatus.not_found
    assert (
        await promo_service.redeem(session, telegram_id=8101, raw_code="!!")
    ).status is promo_service.RedeemStatus.bad_format


@pytest.mark.asyncio
async def test_redeem_requires_registration(session):
    await _make_code(session, code="NEWBIE", days=30, max_uses=5)
    result = await promo_service.redeem(session, telegram_id=999999, raw_code="NEWBIE")
    assert result.status is promo_service.RedeemStatus.not_registered


@pytest.mark.asyncio
async def test_redeem_is_blocked_for_banned_user(session, make_user):
    user = await make_user(8102)
    user.is_banned = True
    await session.commit()
    await _make_code(session, code="BAN30", days=30, max_uses=5)

    result = await promo_service.redeem(session, telegram_id=8102, raw_code="BAN30")

    assert result.status is promo_service.RedeemStatus.banned
    code = (await session.execute(select(PromoCode).where(PromoCode.code == "BAN30"))).scalar_one()
    assert code.used_count == 0, "a refused attempt must not burn a slot"


@pytest.mark.asyncio
async def test_used_up_code_reports_exhausted(session, make_user):
    await make_user(8103)
    await make_user(8104)
    await _make_code(session, code="ONE", days=30, max_uses=1)

    first = await promo_service.redeem(session, telegram_id=8103, raw_code="ONE")
    second = await promo_service.redeem(session, telegram_id=8104, raw_code="ONE")

    assert first.ok
    assert second.status is promo_service.RedeemStatus.exhausted
    code = (await session.execute(select(PromoCode).where(PromoCode.code == "ONE"))).scalar_one()
    assert code.used_count == 1


@pytest.mark.asyncio
async def test_expired_and_disabled_codes_are_refused(session, make_user):
    await make_user(8105)
    promo = await _make_code(session, code="OLD30", days=30, max_uses=5)
    promo.expires_at = datetime.now(tz=UTC) - timedelta(hours=1)
    await session.commit()

    assert (
        await promo_service.redeem(session, telegram_id=8105, raw_code="OLD30")
    ).status is promo_service.RedeemStatus.expired

    fresh = await _make_code(session, code="OFF30", days=30, max_uses=5)
    await promo_service.set_active(session, fresh.id, False)
    assert (
        await promo_service.redeem(session, telegram_id=8105, raw_code="OFF30")
    ).status is promo_service.RedeemStatus.inactive


@pytest.mark.asyncio
async def test_admin_creates_promo_and_sees_it_in_the_panel(bot, dispatcher, session, settings):
    ADMIN = 1

    await press(bot, dispatcher, ADMIN, "adm:promo")
    assert "Промокод" in bot.session.last_text()

    await press(bot, dispatcher, ADMIN, "adm:promo_new")
    await send_text(bot, dispatcher, ADMIN, "LAUNCH 30 100 14")

    text = bot.session.last_text()
    assert "LAUNCH" in text and "0/100" in text

    promo = (await session.execute(select(PromoCode).where(PromoCode.code == "LAUNCH"))).scalar_one()
    assert promo.days == 30 and promo.max_uses == 100
    assert promo.expires_at is not None

    audit = (
        await session.execute(select(AdminAudit).where(AdminAudit.action == "promo_create"))
    ).scalar_one()
    assert "LAUNCH" in (audit.details or "")


@pytest.mark.asyncio
async def test_admin_toggles_promo(bot, dispatcher, session, settings):
    ADMIN = 1
    promo = await _make_code(session, code="TOGGLE", days=30, max_uses=10)

    await press(bot, dispatcher, ADMIN, f"adm:promo_toggle:{promo.id}")

    await session.refresh(promo)
    assert promo.is_active is False
    assert "🚫" in bot.session.last_text()


@pytest.mark.asyncio
async def test_admin_cannot_create_a_broken_code(bot, dispatcher, session, settings):
    ADMIN = 1
    await press(bot, dispatcher, ADMIN, "adm:promo_new")
    await send_text(bot, dispatcher, ADMIN, "мусор")

    assert "Не разобрал" in bot.session.last_text()
    codes = (await session.execute(select(PromoCode))).scalars().all()
    assert codes == []


@pytest.mark.asyncio
async def test_user_redeems_code_through_the_profile_button(bot, dispatcher, session, settings):
    await _make_code(session, code="GIFT30", days=30, max_uses=10)
    await _register_tg(bot, dispatcher, 8200)

    await press(bot, dispatcher, 8200, "menu:promo")
    assert "Промокод" in bot.session.last_text()

    await send_text(bot, dispatcher, 8200, "gift30")

    assert "Промокод активирован" in bot.session.last_text()
    user = (await session.execute(select(User).where(User.telegram_id == 8200))).scalar_one()
    await session.refresh(user)
    assert user.subscription_until is not None
    assert bot.session.live_count(8200) == 1, "chat stays at one message"


@pytest.mark.asyncio
async def test_user_gets_a_clear_error_for_a_wrong_code(bot, dispatcher, session, settings):
    await _register_tg(bot, dispatcher, 8201)

    await send_text(bot, dispatcher, 8201, "/promo WRONG")

    text = bot.session.last_text()
    assert "Код не активирован" in text
    assert "нет" in text
    assert bot.session.live_count(8201) == 1


@pytest.mark.asyncio
async def test_redeem_attempts_are_rate_limited(bot, dispatcher, session, settings):
    await _register_tg(bot, dispatcher, 8202)

    for i in range(settings.promo_attempts_per_hour):
        await send_text(bot, dispatcher, 8202, f"/promo NOPE{i}")
    await send_text(bot, dispatcher, 8202, "/promo NOPE-LAST")

    assert "Слишком много попыток" in bot.session.last_text()
    redemptions = (await session.execute(select(PromoRedemption))).scalars().all()
    assert redemptions == []

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.database.models import AdminAudit, User
from app.handlers.commands import _parse_invite, invite_link
from tests.conftest import press, send_text


def test_invite_payload_parsing():
    assert _parse_invite("/start ref_123456") == 123456
    assert _parse_invite("/start ref_") is None
    assert _parse_invite("/start ref_abc") is None
    assert _parse_invite("/start") is None
    assert _parse_invite("") is None
    assert invite_link("mybot", 42) == "https://t.me/mybot?start=ref_42"


async def _register_tg(bot, dispatcher, tg_id: int, start_text: str = "/start"):
    await send_text(bot, dispatcher, tg_id, start_text)
    await press(bot, dispatcher, tg_id, "consent:accept")
    await press(bot, dispatcher, tg_id, "reg:gender:female")
    await send_text(bot, dispatcher, tg_id, "01.01.1997")
    await press(bot, dispatcher, tg_id, "reg:city:global")
    await press(bot, dispatcher, tg_id, "reg:seek:any")


@pytest.mark.asyncio
async def test_invite_rewards_the_inviter_once(bot, dispatcher, session, settings, make_user):
    inviter = await make_user(7001)

    await _register_tg(bot, dispatcher, 7002, start_text="/start ref_7001")

    invitee = (await session.execute(select(User).where(User.telegram_id == 7002))).scalar_one()
    await session.refresh(invitee)
    assert invitee.invited_by == 7001
    assert invitee.invite_rewarded is True

    await session.refresh(inviter)
    assert inviter.subscription_until is not None
    grant = inviter.subscription_until
    if grant.tzinfo is None:
        grant = grant.replace(tzinfo=UTC)
    assert grant > datetime.now(tz=UTC) + timedelta(days=int(settings.referral_reward_days) - 1)

    audit = (await session.execute(select(AdminAudit).where(AdminAudit.action == "referral_bonus"))).scalars().all()
    assert len(audit) == 1

    # Replayed deep link must not pay twice.
    from app.handlers.commands import _pay_referral

    await _pay_referral(bot, None, session, settings, inviter_tg=7001, invitee=invitee)  # type: ignore[arg-type]
    await session.refresh(inviter)
    assert inviter.subscription_until.replace(tzinfo=None) == grant.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_self_invite_is_ignored(bot, dispatcher, session, make_user):
    await _register_tg(bot, dispatcher, 7003, start_text="/start ref_7003")

    user = (await session.execute(select(User).where(User.telegram_id == 7003))).scalar_one()
    assert user.invited_by is None
    assert user.subscription_until is None


@pytest.mark.asyncio
async def test_invite_screen_shows_link_and_counter(bot, dispatcher, session, settings, make_user):
    await make_user(7004)
    await send_text(bot, dispatcher, 7004, "/invite")

    text = bot.session.last_text()
    assert "Пригласите друга" in text
    assert "t.me/testbot?start=ref_7004" in text
    assert "Уже приглашено: <b>0</b>" in text
    assert bot.session.live_count(7004) == 1


@pytest.mark.asyncio
async def test_referral_can_be_disabled(session, settings, make_user):
    """`REFERRAL_REWARD_DAYS=0` turns the bonus off without breaking registration."""
    from app.handlers.commands import _pay_referral

    inviter = await make_user(7005)
    invitee = await make_user(7006)
    settings_no_reward = settings.model_copy(update={"referral_reward_days": 0})

    await _pay_referral(  # type: ignore[arg-type]
        None, None, session, settings_no_reward, inviter_tg=7005, invitee=invitee
    )

    await session.refresh(inviter)
    assert inviter.subscription_until is None
    await session.refresh(invitee)
    assert invitee.invite_rewarded is False

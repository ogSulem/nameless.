from __future__ import annotations

from datetime import UTC

import pytest

from app.database.models import Complaint, ComplaintStatus
from app.services.admin import find_user
from tests.test_e2e_flow import press, send_text

ADMIN = 1  # settings fixture: ADMINS=1
SUPPORT = 3  # SUPPORT=3
MODERATOR = 2  # MODERATORS=2
NOBODY = 777


async def _open_admin(bot, dispatcher, tg_id: int) -> None:
    await send_text(bot, dispatcher, tg_id, "/admin")


@pytest.mark.asyncio
async def test_admin_menu_requires_a_role(bot, dispatcher, session, settings):
    await _open_admin(bot, dispatcher, NOBODY)

    assert "недоступна" in bot.session.last_text().lower()


@pytest.mark.asyncio
async def test_admin_menu_opens_with_stats(bot, dispatcher, session, make_user):
    await make_user(5000)
    await _open_admin(bot, dispatcher, ADMIN)

    text = bot.session.last_text()
    assert "Админ-панель" in text
    assert "Пользователи" in text
    assert "adm:stats" in bot.session.markup_buttons()
    assert "adm:audit:0" in bot.session.markup_buttons()


@pytest.mark.asyncio
async def test_support_cannot_ban(bot, dispatcher, session, make_user):
    target = await make_user(5001)

    await press(bot, dispatcher, SUPPORT, f"adm:ban:{target.telegram_id}")

    answers = " ".join(a for a in bot.session.answers if a)
    assert "Нет доступа" in answers
    await session.refresh(target)
    assert target.is_banned is False


@pytest.mark.asyncio
async def test_moderator_can_ban_and_action_is_audited(bot, dispatcher, session, make_user):
    from sqlalchemy import select

    from app.database.models import AdminAudit

    target = await make_user(5002)

    await press(bot, dispatcher, MODERATOR, f"adm:ban:{target.telegram_id}")

    await session.refresh(target)
    assert target.is_banned is True
    audit = (await session.execute(select(AdminAudit))).scalars().all()
    assert any(a.action == "ban" and a.admin_tg == MODERATOR for a in audit)


@pytest.mark.asyncio
async def test_unban_restores_access(bot, dispatcher, session, make_user):
    target = await make_user(5003, banned=True)

    await press(bot, dispatcher, MODERATOR, f"adm:unban:{target.telegram_id}")

    await session.refresh(target)
    assert target.is_banned is False


@pytest.mark.asyncio
async def test_user_lookup_by_telegram_id(bot, dispatcher, session, make_user):
    await make_user(5004, rating=9.0, ratings_count=12)

    await press(bot, dispatcher, SUPPORT, "adm:user_prompt")
    assert "Telegram ID" in bot.session.last_text()

    await send_text(bot, dispatcher, SUPPORT, "5004")

    text = bot.session.last_text()
    assert "5004" in text
    assert "9.00" in text
    assert "adm:ban:5004" in bot.session.markup_buttons()


@pytest.mark.asyncio
async def test_premium_grant_requires_admin_role(bot, dispatcher, session, make_user):
    target = await make_user(5005)

    await press(bot, dispatcher, MODERATOR, f"adm:prem:{target.telegram_id}")
    await session.refresh(target)
    assert target.subscription_until is None

    await press(bot, dispatcher, ADMIN, f"adm:prem:{target.telegram_id}")
    await session.refresh(target)
    assert target.subscription_until is not None


@pytest.mark.asyncio
async def test_complaint_queue_shows_the_conversation(bot, dispatcher, session, make_user):
    reporter = await make_user(5006)
    accused = await make_user(5007)
    session.add(
        Complaint(
            dialog_id=1,
            from_user_id=reporter.id,
            accused_user_id=accused.id,
            reason="Оскорбления в чате",
            status=ComplaintStatus.open.value,
        )
    )
    await session.commit()
    complaint = (await session.execute(__import__("sqlalchemy").select(Complaint))).scalars().first()

    await press(bot, dispatcher, SUPPORT, "adm:complaints:0")
    assert "Оскорбления" in bot.session.all_text() or "adm:complaint" in str(bot.session.markup_buttons())

    await press(bot, dispatcher, SUPPORT, f"adm:complaint:{complaint.id}")
    text = bot.session.last_text()
    assert "Жалоба" in text
    assert "5007" in text


@pytest.mark.asyncio
async def test_moderator_resolves_complaint(bot, dispatcher, session, make_user):
    reporter = await make_user(5008)
    accused = await make_user(5009)
    session.add(
        Complaint(
            dialog_id=1,
            from_user_id=reporter.id,
            accused_user_id=accused.id,
            reason="спам",
            status=ComplaintStatus.open.value,
        )
    )
    await session.commit()
    from sqlalchemy import select

    complaint = (await session.execute(select(Complaint))).scalars().first()

    await press(bot, dispatcher, MODERATOR, f"adm:cclose:{complaint.id}")

    await session.refresh(complaint)
    assert complaint.status == ComplaintStatus.resolved.value
    assert complaint.handled_by == MODERATOR


@pytest.mark.asyncio
async def test_payments_and_audit_are_admin_only(bot, dispatcher, session, make_user):
    await press(bot, dispatcher, MODERATOR, "adm:payments")
    answers = " ".join(a for a in bot.session.answers if a)
    assert "Нет доступа" in answers

    await press(bot, dispatcher, ADMIN, "adm:payments")
    assert "Платежи" in bot.session.last_text()

    await press(bot, dispatcher, ADMIN, "adm:audit:0")
    assert "Журнал" in bot.session.last_text()


@pytest.mark.asyncio
async def test_maintenance_reports_retention(bot, dispatcher, session, settings):
    await press(bot, dispatcher, ADMIN, "adm:maintenance")

    text = bot.session.last_text()
    assert "Обслуживание" in text
    assert "Удалено сообщений" in text


@pytest.mark.asyncio
async def test_unknown_user_lookup_is_graceful(bot, dispatcher, session):
    await press(bot, dispatcher, SUPPORT, "adm:user_prompt")
    await send_text(bot, dispatcher, SUPPORT, "999999999")

    assert "не найден" in bot.session.last_text().lower()
    assert await find_user(session, 999999999) is None


@pytest.mark.asyncio
async def test_premium_mode_toggle_changes_matching_preference(bot, dispatcher, session, redis, settings, make_user):
    from datetime import datetime, timedelta, timezone

    user = await make_user(5010)
    user.subscription_until = datetime.now(tz=UTC) + timedelta(days=10)
    await session.commit()

    await press(bot, dispatcher, 5010, "premium:mode")
    from app.services.matchmaking import MatchmakingService

    assert await MatchmakingService(redis, settings).premium_mode(5010) == "appearance"

    await press(bot, dispatcher, 5010, "premium:mode")
    assert await MatchmakingService(redis, settings).premium_mode(5010) == "chat"


@pytest.mark.asyncio
async def test_premium_mode_requires_active_subscription(bot, dispatcher, session, redis, settings, make_user):
    from app.services.matchmaking import MatchmakingService

    await make_user(5011)

    await press(bot, dispatcher, 5011, "premium:mode")

    answers = " ".join(a for a in bot.session.answers if a)
    assert "Premium" in answers
    assert await MatchmakingService(redis, settings).premium_mode(5011) == "chat"

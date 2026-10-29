from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.models import AdminAudit, Complaint, ComplaintStatus, Message, Photo
from app.services import admin as admin_service


def _naive(dt: datetime) -> datetime:
    """SQLite drops tzinfo; Postgres keeps it. Compare in naive UTC."""
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


async def _complaint(session, reporter_id: int, accused_id: int, status=ComplaintStatus.open, reason="x"):
    complaint = Complaint(
        dialog_id=1,
        from_user_id=reporter_id,
        accused_user_id=accused_id,
        reason=reason,
        status=status.value,
    )
    session.add(complaint)
    await session.commit()
    return complaint


@pytest.mark.asyncio
async def test_ban_and_unban_are_audited(session, make_user):
    user = await make_user(600)

    result = await admin_service.set_ban(session, user, admin_tg=1, banned=True, reason="спам")

    assert result.ok is True
    await session.refresh(user)
    assert user.is_banned is True
    assert user.ban_reason == "спам"

    audit = (await session.execute(select(AdminAudit).order_by(AdminAudit.id))).scalars().all()
    assert [a.action for a in audit] == ["ban"]
    assert audit[0].admin_tg == 1
    assert audit[0].target_tg == 600
    assert audit[0].details == "спам"

    await admin_service.set_ban(session, user, admin_tg=1, banned=False)
    await session.refresh(user)
    assert user.is_banned is False
    assert user.ban_reason is None


@pytest.mark.asyncio
async def test_grant_premium_extends_from_existing_future_date(session, make_user):
    user = await make_user(601)
    future = datetime.now(tz=UTC) + timedelta(days=10)
    user.subscription_until = future
    await session.commit()

    await admin_service.grant_premium(session, user, admin_tg=1, days=30)

    await session.refresh(user)
    assert _naive(user.subscription_until) >= _naive(future) + timedelta(days=29)


@pytest.mark.asyncio
async def test_grant_premium_from_expired_date_starts_now(session, make_user):
    user = await make_user(602)
    user.subscription_until = datetime.now(tz=UTC) - timedelta(days=100)
    await session.commit()

    before = datetime.now(tz=UTC)
    await admin_service.grant_premium(session, user, admin_tg=1, days=7)

    await session.refresh(user)
    assert _naive(user.subscription_until) >= _naive(before) + timedelta(days=6, hours=23)


@pytest.mark.asyncio
async def test_mute_sets_future_timestamp(session, make_user):
    user = await make_user(603)
    await admin_service.set_mute(session, user, admin_tg=1, hours=2)

    await session.refresh(user)
    assert user.muted_until is not None
    assert _naive(user.muted_until) > _naive(datetime.now(tz=UTC))


@pytest.mark.asyncio
async def test_reset_rating_clears_scores(session, make_user):
    user = await make_user(604, rating=1.5, ratings_count=20)

    await admin_service.reset_rating(session, user, admin_tg=1)

    await session.refresh(user)
    assert user.season_rating_chat == 0.0
    assert user.chat_ratings_count == 0


@pytest.mark.asyncio
async def test_complaint_queue_and_resolution(session, make_user):
    reporter = await make_user(605)
    target = await make_user(606)
    complaint = await _complaint(session, reporter.id, target.id, reason="Оскорбления")

    open_list = await admin_service.list_complaints(session, page=0)
    assert [row.id for row in open_list] == [complaint.id]
    assert open_list[0].accused_tg == 606

    detail = await admin_service.complaint_detail(session, complaint.id)
    assert detail is not None
    assert detail.accused_tg == 606
    assert detail.reporter_tg == 605

    result = await admin_service.close_complaint(
        session, complaint.id, admin_tg=1, status=ComplaintStatus.resolved, resolution="забанен"
    )
    assert result.ok is True

    await session.refresh(complaint)
    assert complaint.status == ComplaintStatus.resolved.value
    assert complaint.handled_by == 1
    assert complaint.resolution == "забанен"
    assert await admin_service.list_complaints(session, page=0) == []

    audit = (await session.execute(select(AdminAudit))).scalars().all()
    assert audit[-1].action == "complaint_resolved"


@pytest.mark.asyncio
async def test_closing_unknown_complaint_returns_error(session):
    result = await admin_service.close_complaint(
        session, 999, admin_tg=1, status=ComplaintStatus.resolved
    )
    assert result.ok is False


@pytest.mark.asyncio
async def test_user_card_shows_complaint_counts(session, settings, make_user):
    target = await make_user(607, rating=8.0, ratings_count=4)
    reporter = await make_user(608)
    await _complaint(session, reporter.id, target.id, ComplaintStatus.open)
    await _complaint(session, reporter.id, target.id, ComplaintStatus.resolved)

    card = await admin_service.user_card(session, target, settings)

    assert card.open_complaints == 1
    assert card.total_complaints == 2
    assert "607" in card.text
    assert "8.00" in card.text


@pytest.mark.asyncio
async def test_retention_job_deletes_only_expired_rows(session, settings, make_user):
    author = await make_user(609)
    old = datetime.now(tz=UTC) - timedelta(days=int(settings.retention_message_days) + 5)
    fresh = datetime.now(tz=UTC) - timedelta(days=1)

    session.add_all(
        [
            Message(dialog_id=1, from_user_id=author.id, text="old", created_at=old),
            Message(dialog_id=1, from_user_id=author.id, text="fresh", created_at=fresh),
            Photo(dialog_id=1, owner_user_id=author.id, file_path="f1", created_at=old),
            Photo(dialog_id=1, owner_user_id=author.id, file_path="f2", created_at=fresh),
        ]
    )
    await session.commit()

    report = await admin_service.run_maintenance(session, settings)

    assert report.deleted_messages == 1
    assert report.deleted_photos == 1
    texts = (await session.execute(select(Message.text))).scalars().all()
    assert texts == ["fresh"]


@pytest.mark.asyncio
async def test_stats_survive_empty_database(session, settings):
    s = await admin_service.stats(session)

    assert s.users == 0
    assert "Панель" in admin_service.stats_text(s, settings)


@pytest.mark.asyncio
async def test_delete_user_data_removes_profile(session, make_user):
    user = await make_user(610)

    result = await admin_service.delete_user_data(session, user, admin_tg=1)

    assert result.ok is True
    assert await admin_service.find_user(session, 610) is None

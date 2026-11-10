from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.models import ActiveDialog, Dialog, DialogStatus, Message
from app.redis import keys
from app.services.dialog import DialogService


async def _make_dialog(session, a_id: int, b_id: int, *, created_delta: timedelta = timedelta(0)) -> Dialog:
    dialog = Dialog(
        user1_id=a_id,
        user2_id=b_id,
        status=DialogStatus.active,
        created_at=datetime.now(tz=UTC) - created_delta,
    )
    session.add(dialog)
    await session.flush()
    session.add_all(
        [
            ActiveDialog(user_id=a_id, dialog_id=dialog.id),
            ActiveDialog(user_id=b_id, dialog_id=dialog.id),
        ]
    )
    await session.commit()
    return dialog


@pytest.mark.asyncio
async def test_active_dialog_is_recovered_from_db_when_cache_is_lost(session, redis, settings, make_user):
    """The old version deadlocked users forever once the 12h Redis TTL expired."""
    a = await make_user(10)
    b = await make_user(20)
    dialog = await _make_dialog(session, a.id, b.id)

    svc = DialogService(redis, settings)
    assert await redis.get(keys.active_dialog(10)) is None

    recovered = await svc.active_dialog_id(session, 10)

    assert recovered == dialog.id
    # …and the cache is repopulated for the fast path.
    assert await redis.get(keys.active_dialog(10)) == str(dialog.id)
    assert await redis.get(keys.dialog_partner_tg(dialog.id, 10)) == "20"


@pytest.mark.asyncio
async def test_heartbeat_extends_ttl(session, redis, settings, make_user):
    a = await make_user(11)
    b = await make_user(21)
    await _make_dialog(session, a.id, b.id)
    svc = DialogService(redis, settings)
    await svc.active_dialog_id(session, 11)

    await redis.expire(keys.active_dialog(11), 5)
    await svc.active_dialog_id(session, 11)

    ttl = await redis.ttl(keys.active_dialog(11))
    assert ttl > 60


@pytest.mark.asyncio
async def test_finish_dialog_clears_state_and_returns_participants(session, redis, settings, make_user):
    a = await make_user(12)
    b = await make_user(22)
    dialog = await _make_dialog(session, a.id, b.id)
    svc = DialogService(redis, settings)
    await svc.active_dialog_id(session, 12)

    telegram_ids = await svc.finish(session, dialog.id, actor_tg_id=12)

    assert set(telegram_ids or []) == {12, 22}
    await session.refresh(dialog)
    assert dialog.status == DialogStatus.finished
    assert dialog.finished_at is not None
    assert (await session.execute(select(ActiveDialog).where(ActiveDialog.dialog_id == dialog.id))).first() is None
    assert await redis.get(keys.active_dialog(12)) is None


@pytest.mark.asyncio
async def test_sweeper_closes_stale_dialogs(session, redis, settings, make_user):
    a = await make_user(13)
    b = await make_user(23)
    stale = await _make_dialog(session, a.id, b.id, created_delta=timedelta(hours=13))

    svc = DialogService(redis, settings)
    closed = await svc.sweep(session)

    assert [d for d, _ in closed] == [stale.id]
    await session.refresh(stale)
    assert stale.status == DialogStatus.finished


@pytest.mark.asyncio
async def test_sweeper_keeps_fresh_dialogs(session, redis, settings, make_user):
    a = await make_user(14)
    b = await make_user(24)
    await _make_dialog(session, a.id, b.id, created_delta=timedelta(hours=1))

    closed = await DialogService(redis, settings).sweep(session)
    assert closed == []


@pytest.mark.asyncio
async def test_save_text_and_photo(session, redis, settings, make_user):
    a = await make_user(15)
    b = await make_user(25)
    dialog = await _make_dialog(session, a.id, b.id)
    svc = DialogService(redis, settings)

    await svc.save_text(session, dialog.id, a.id, "привет")
    await svc.save_photo(session, dialog.id, a.id, "AgAC-file-id")

    rows = (
        await session.execute(select(Message).where(Message.dialog_id == dialog.id).order_by(Message.id.asc()))
    ).scalars().all()
    assert [m.text for m in rows] == ["привет", None]
    await session.refresh(dialog)
    assert dialog.has_photos is True


@pytest.mark.asyncio
async def test_dialog_info_rejects_finished_dialog(session, redis, settings, make_user):
    a = await make_user(16)
    b = await make_user(26)
    dialog = await _make_dialog(session, a.id, b.id)
    await DialogService(redis, settings).finish(session, dialog.id)

    assert await DialogService(redis, settings).dialog_info(session, dialog.id, 16) is None

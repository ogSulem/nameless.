from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.database.models import Dialog, DialogStatus, Gender, Rating, RatingType
from app.handlers.rating import _save_rating


@pytest.mark.asyncio
async def test_second_rating_for_the_same_dialog_is_ignored(session, settings, make_user):
    """The saving path flushes before recalculating: a duplicate must not count twice."""
    giver = await make_user(9401, gender=Gender.male)
    receiver = await make_user(9402, gender=Gender.female)
    dialog = Dialog(
        user1_id=giver.id,
        user2_id=receiver.id,
        status=DialogStatus.finished,
        created_at=datetime.now(tz=UTC),
        finished_at=datetime.now(tz=UTC),
    )
    session.add(dialog)
    await session.commit()

    first = await _save_rating(
        session, settings, dialog_id=dialog.id, from_tg=9401, to_tg=9402,
        rating_type=RatingType.chat, value=9,
    )
    assert first is True

    again = await _save_rating(
        session, settings, dialog_id=dialog.id, from_tg=9401, to_tg=9402,
        rating_type=RatingType.chat, value=2,
    )
    assert again is False

    rows = (await session.execute(select(Rating))).scalars().all()
    assert [r.value for r in rows] == [9]

    await session.refresh(receiver)
    assert receiver.chat_ratings_count == 1
    assert receiver.season_rating_chat == pytest.approx(
        round((9 + 5.0 * float(settings.rating_prior_weight)) / (1 + float(settings.rating_prior_weight)), 2)
    )

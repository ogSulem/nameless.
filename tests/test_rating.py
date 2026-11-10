from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import Dialog, DialogStatus, Rating, RatingType
from app.services.rating import RatingService


async def _dialog(session, a_id: int, b_id: int, *, days_ago: int = 0) -> Dialog:
    dialog = Dialog(
        user1_id=a_id,
        user2_id=b_id,
        status=DialogStatus.finished,
        created_at=datetime.now(tz=UTC) - timedelta(days=days_ago),
    )
    session.add(dialog)
    await session.commit()
    return dialog


async def _rate(session, from_id: int, to_id: int, value: int, *, valid: bool = True) -> Dialog:
    """One rating per dialog — the DB enforces (dialog, from, to, type) uniqueness."""
    dialog = await _dialog(session, from_id, to_id)
    session.add(
        Rating(
            dialog_id=dialog.id,
            from_user_id=from_id,
            to_user_id=to_id,
            rating_type=RatingType.chat,
            value=value,
            is_seasonal_valid=valid,
        )
    )
    await session.commit()
    return dialog


def test_blended_pulls_small_samples_towards_the_prior(settings):
    service = RatingService(settings)

    assert service.blended([]) == 0.0
    # One 0 must not pin a newcomer at 0.0 (prior 5.0 with weight 5).
    assert service.blended([0]) == pytest.approx(5 * 5 / 6, abs=0.01)
    # A large sample converges to the real mean.
    assert service.blended([10] * 50) == pytest.approx(9.55, abs=0.2)


@pytest.mark.asyncio
async def test_recalculate_uses_only_seasonal_valid_ratings(session, settings, make_user):
    author = await make_user(1)
    target = await make_user(2)
    await _rate(session, author.id, target.id, 10, valid=True)
    await _rate(session, author.id, target.id, 0, valid=False)

    update = await RatingService(settings).recalculate(session, target.id)

    await session.refresh(target)
    assert update.chat_count == 1
    assert target.chat_ratings_count == 1
    assert target.season_rating_chat == pytest.approx(service_prior(settings, [10]), abs=0.01)


def service_prior(settings, values):
    prior_mean = float(settings.rating_prior_mean)
    prior_weight = float(settings.rating_prior_weight)
    return (sum(values) + prior_mean * prior_weight) / (len(values) + prior_weight)


@pytest.mark.asyncio
async def test_recalculate_counts_and_window(session, settings, make_user):
    author = await make_user(3)
    target = await make_user(4)
    for value in (10, 10, 10, 2, 2, 2):
        await _rate(session, author.id, target.id, value)

    update = await RatingService(settings).recalculate(session, target.id)
    assert update.chat_count == 6
    assert 0 < update.chat_rating < 10


@pytest.mark.asyncio
async def test_rating_drop_flags_user_for_review(session, settings, make_user):
    author = await make_user(5)
    target = await make_user(6, rating=9.5, ratings_count=5)
    for _ in range(6):
        await _rate(session, author.id, target.id, 0)

    await RatingService(settings).recalculate(session, target.id)
    await session.refresh(target)
    assert target.is_under_review is True


@pytest.mark.asyncio
async def test_same_pair_meeting_too_often_is_not_seasonal(session, settings, make_user):
    a = await make_user(7)
    b = await make_user(8)
    for _ in range(4):
        await _dialog(session, a.id, b.id, days_ago=1)
    dialog = await _dialog(session, a.id, b.id)

    decision = await RatingService(settings).decide_seasonal_validity(
        session=session, dialog_id=dialog.id, from_user_id=a.id, to_user_id=b.id
    )
    assert decision.is_seasonal_valid is False
    assert decision.reason == "pair_met_too_often"


@pytest.mark.asyncio
async def test_normal_pair_is_seasonal(session, settings, make_user):
    a = await make_user(9)
    b = await make_user(10)
    dialog = await _dialog(session, a.id, b.id)

    decision = await RatingService(settings).decide_seasonal_validity(
        session=session, dialog_id=dialog.id, from_user_id=a.id, to_user_id=b.id
    )
    assert decision.is_seasonal_valid is True
    assert decision.reason is None

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.database.models import ActiveDialog, Dialog, Gender, User
from app.redis import keys
from app.services.matchmaking import MatchmakingService


async def _dialog_count(session) -> int:
    return int((await session.execute(select(func.count(Dialog.id)))).scalar_one())


@pytest.mark.asyncio
async def test_first_search_enqueues_second_search_matches(session, redis, settings, make_user):
    """Regression: matchmaking used to raise TypeError on every search and create 0 dialogs."""
    seeker = await make_user(100, gender=Gender.male)
    waiting = await make_user(200, gender=Gender.female)
    svc = MatchmakingService(redis, settings)

    assert await svc.try_match(session, seeker, premium=False) is None
    assert await redis.exists(keys.queue_member_key(100))

    result = await svc.try_match(session, waiting, premium=False)

    assert result is not None
    assert result.partner_telegram_id == 100
    assert result.dialog_id > 0
    assert await _dialog_count(session) == 1

    # Both users are now "in dialog" in the DB and in the cache.
    assert await session.get(ActiveDialog, seeker.id) is not None
    assert await session.get(ActiveDialog, waiting.id) is not None
    assert await redis.get(keys.active_dialog(100)) == str(result.dialog_id)

    # A third search must not match: both are busy.
    other = await make_user(300, gender=Gender.male)
    assert await svc.try_match(session, other, premium=False) is None


@pytest.mark.asyncio
async def test_city_queue_is_preferred_but_global_fallback_works(session, redis, settings, make_user):
    muscovite = await make_user(101, city="Москва")
    from_kazan = await make_user(201, gender=Gender.female, city="Казань")
    svc = MatchmakingService(redis, settings)

    await svc.enqueue(session, muscovite)
    result = await svc.try_match(session, from_kazan, premium=False)

    # Different cities: global queue is used as a fallback.
    assert result is not None
    assert result.partner_telegram_id == 101


@pytest.mark.asyncio
async def test_premium_picks_the_best_rated_candidate(session, redis, settings, make_user):
    low = await make_user(102, gender=Gender.female, rating=5.5, ratings_count=10)
    high = await make_user(103, gender=Gender.female, rating=8.7, ratings_count=10)
    mid = await make_user(104, gender=Gender.female, rating=7.4, ratings_count=10)
    seeker = await make_user(202, gender=Gender.male)

    svc = MatchmakingService(redis, settings)
    for candidate in (low, high, mid):
        await svc.enqueue(session, candidate)

    result = await svc.try_match(session, seeker, premium=True)

    assert result is not None
    assert result.partner_telegram_id == 103


@pytest.mark.asyncio
async def test_premium_falls_back_to_best_available_when_nobody_qualifies(session, redis, settings, make_user):
    """Documented in the terms: premium is a priority, not a guarantee."""
    weak = await make_user(105, gender=Gender.female, rating=3.0, ratings_count=5)
    weaker = await make_user(106, gender=Gender.female, rating=2.0, ratings_count=5)
    seeker = await make_user(203, gender=Gender.male)

    svc = MatchmakingService(redis, settings)
    await svc.enqueue(session, weak)
    await svc.enqueue(session, weaker)

    result = await svc.try_match(session, seeker, premium=True)

    assert result is not None
    assert result.partner_telegram_id == 105


@pytest.mark.asyncio
async def test_banned_candidates_are_skipped_and_purged(session, redis, settings, make_user):
    banned = await make_user(107, gender=Gender.female, banned=True)
    good = await make_user(108, gender=Gender.female)
    seeker = await make_user(204, gender=Gender.male)

    svc = MatchmakingService(redis, settings)
    await svc.enqueue(session, banned)
    await svc.enqueue(session, good)

    result = await svc.try_match(session, seeker, premium=False)

    assert result is not None
    assert result.partner_telegram_id == 108
    assert await redis.zscore(keys.queue_key("female", "Москва"), "107") is None


@pytest.mark.asyncio
async def test_last_partner_is_not_rematched_immediately(session, redis, settings, make_user):
    first = await make_user(110, gender=Gender.female)
    second = await make_user(111, gender=Gender.female)
    seeker = await make_user(205, gender=Gender.male)

    svc = MatchmakingService(redis, settings)
    await svc.enqueue(session, first)
    await svc.enqueue(session, second)
    await redis.set(keys.last_partner(205), "110", ex=600)

    result = await svc.try_match(session, seeker, premium=False)

    assert result is not None
    assert result.partner_telegram_id == 111
    # The skipped partner stays in the queue for somebody else.
    assert await redis.zscore(keys.queue_key("female", "Москва"), "110") is not None


@pytest.mark.asyncio
async def test_offline_ghosts_are_dropped_from_the_queue(session, redis, settings, make_user):
    ghost = await make_user(112, gender=Gender.female)
    alive = await make_user(113, gender=Gender.female)
    seeker = await make_user(206, gender=Gender.male)

    svc = MatchmakingService(redis, settings)
    await svc.enqueue(session, ghost)
    await svc.enqueue(session, alive)
    # The ghost closed the bot: the "online" key expired.
    await redis.delete(keys.online_key(112))

    result = await svc.try_match(session, seeker, premium=False)

    assert result is not None
    assert result.partner_telegram_id == 113
    assert await redis.zscore(keys.queue_key("female", "Москва"), "112") is None


@pytest.mark.asyncio
async def test_user_with_active_dialog_cannot_search_again(session, redis, settings, make_user):
    a = await make_user(114, gender=Gender.female)
    b = await make_user(207, gender=Gender.male)

    svc = MatchmakingService(redis, settings)
    await svc.enqueue(session, a)
    result = await svc.try_match(session, b, premium=False)
    assert result is not None

    assert await svc.try_match(session, b, premium=False) is None


@pytest.mark.asyncio
async def test_dequeue_removes_user_from_queue(session, redis, settings, make_user):
    user = await make_user(115)
    svc = MatchmakingService(redis, settings)
    queue = await svc.enqueue(session, user)

    assert await redis.zscore(queue, "115") is not None
    await svc.dequeue(115)
    assert await redis.zscore(queue, "115") is None


@pytest.mark.asyncio
async def test_premium_cache_can_be_invalidated(session, redis, settings, make_user):
    user = await make_user(116)
    svc = MatchmakingService(redis, settings)

    assert await svc.is_premium(session, user) is False
    from datetime import datetime, timedelta

    user.subscription_until = datetime.now(tz=UTC) + timedelta(days=30)
    await session.commit()

    # Still cached as "not premium"…
    assert await svc.is_premium(session, user) is False
    # …until the payment handler invalidates the cache.
    await svc.invalidate_premium(116)
    assert await svc.is_premium(session, user) is True


@pytest.mark.asyncio
async def test_premium_appearance_mode_prefers_the_most_attractive(session, redis, settings, make_user):
    from datetime import datetime, timedelta, timezone

    plain = await make_user(120, gender=Gender.female, rating=9.5, ratings_count=20)
    plain.season_rating_appearance = 2.0
    plain.appearance_ratings_count = 0
    beautiful = await make_user(121, gender=Gender.female, rating=6.0, ratings_count=10)
    beautiful.season_rating_appearance = 9.3
    beautiful.appearance_ratings_count = 12
    await session.commit()

    seeker = await make_user(220, gender=Gender.male)
    svc = MatchmakingService(redis, settings)
    await svc.set_premium_mode(220, "appearance")
    await svc.enqueue(session, plain)
    await svc.enqueue(session, beautiful)

    result = await svc.try_match(session, seeker, premium=True)

    assert result is not None
    assert result.partner_telegram_id == 121


@pytest.mark.asyncio
async def test_premium_chat_mode_is_the_default(session, redis, settings, make_user):
    svc = MatchmakingService(redis, settings)
    assert await svc.premium_mode(999) == "chat"
    assert await svc.set_premium_mode(999, "appearance") == "appearance"
    assert await svc.premium_mode(999) == "appearance"
    assert await svc.set_premium_mode(999, "garbage") == "chat"


@pytest.mark.asyncio
async def test_premium_appearance_falls_back_when_nobody_is_rated(session, redis, settings, make_user):
    """Honest fallback: never leave a paying user waiting forever."""
    a = await make_user(122, gender=Gender.female, rating=4.0, ratings_count=3)
    seeker = await make_user(221, gender=Gender.male)
    svc = MatchmakingService(redis, settings)
    await svc.set_premium_mode(221, "appearance")
    await svc.enqueue(session, a)

    result = await svc.try_match(session, seeker, premium=True)

    assert result is not None
    assert result.partner_telegram_id == 122


@pytest.mark.asyncio
async def test_match_requires_mutual_consent(session, redis, settings, make_user):
    """Женщина, ищущая женщин, не должна попадать к мужчине, который ищет женщин."""
    svc = MatchmakingService(redis, settings)
    man = await make_user(110, gender=Gender.male)
    woman = await make_user(210, gender=Gender.female)

    await svc.try_match(session, man, premium=False)
    # Мужчина ищет женщин; женщина тоже ищет женщин — интересы не совпадают.
    await redis.set(keys.seek_gender_key(210), "female")

    assert await svc.try_match(session, woman, premium=False) is None
    assert await _dialog_count(session) == 0


@pytest.mark.asyncio
async def test_premium_user_is_matched_before_waiting_free_user(session, redis, settings, make_user):
    """Premium даёт приоритет в очереди: его забирают раньше, чем ждущего бесплатного."""
    from datetime import timedelta

    svc = MatchmakingService(redis, settings)
    free = await make_user(120, gender=Gender.female)
    premium = await make_user(220, gender=Gender.female)
    premium_row = await session.get(User, premium.id)
    premium_row.subscription_until = datetime.now(tz=UTC) + timedelta(days=5)
    await session.commit()

    await svc.enqueue(session, free)
    await svc.enqueue(session, premium)

    seeker = await make_user(320, gender=Gender.male)
    result = await svc.try_match(session, seeker, premium=False)

    assert result is not None
    assert result.partner_telegram_id == 220


@pytest.mark.asyncio
async def test_premium_scan_batches_its_db_queries(session, engine, redis, settings, make_user):
    """A premium scan over many candidates must not turn into N+1 round-trips."""
    from sqlalchemy import event

    svc = MatchmakingService(redis, settings)
    for i in range(10):
        cand = await make_user(900 + i, gender=Gender.female, rating=6.0 + i * 0.1, ratings_count=5)
        await svc.enqueue(session, cand)

    seeker = await make_user(990, gender=Gender.male)

    selects: list[str] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", before_cursor_execute)
    try:
        result = await svc.try_match(session, seeker, premium=True)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before_cursor_execute)

    assert result is not None
    assert len(selects) <= 8, f"too many queries for one match: {len(selects)}"

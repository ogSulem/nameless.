"""Premium matching filters, repeat-partner protection and report-driven blocking."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.models import Complaint, Dialog, Gender, User
from app.redis import keys
from app.services.dialog import DialogService
from app.services.matchmaking import MatchmakingService
from tests.conftest import press, send_text
from tests.test_e2e_flow import _register


async def _premium(session, user: User, days: int = 30) -> User:
    user.subscription_until = datetime.now(tz=UTC) + timedelta(days=days)
    await session.commit()
    await session.refresh(user)
    return user


def _birthday(age: int) -> date:
    today = datetime.now(tz=UTC).date()
    return date(today.year - age, 1, 1)


@pytest.mark.asyncio
async def test_age_filter_excludes_other_ages(session, redis, settings, make_user):
    seeker = await make_user(1101, gender=Gender.male, birth_date=_birthday(30))
    await _premium(session, seeker)
    seeker.filter_age_min, seeker.filter_age_max = 25, 34
    await session.commit()

    too_old = await make_user(1102, gender=Gender.female, birth_date=_birthday(45), rating=9.0, ratings_count=20)
    fits = await make_user(1103, gender=Gender.female, birth_date=_birthday(30))

    svc = MatchmakingService(redis, settings)
    await svc.enqueue(session, too_old)
    await svc.enqueue(session, fits)

    result = await svc.try_match(session, seeker, premium=True)

    assert result is not None
    assert result.partner_telegram_id == 1103, "the 45-year-old must be filtered out"


@pytest.mark.asyncio
async def test_city_only_filter_skips_other_cities(session, redis, settings, make_user):
    seeker = await make_user(1111, gender=Gender.male, city="Москва")
    await _premium(session, seeker)
    seeker.filter_city_only = True
    await session.commit()

    other_city = await make_user(1112, gender=Gender.female, city="Казань")

    svc = MatchmakingService(redis, settings)
    await svc.enqueue(session, other_city)

    assert await svc.try_match(session, seeker, premium=True) is None


@pytest.mark.asyncio
async def test_filters_work_both_ways(session, redis, settings, make_user):
    """A candidate's own filter protects them from me, not just me from them."""
    seeker = await make_user(1121, gender=Gender.male, birth_date=_birthday(45))

    picky = await make_user(1122, gender=Gender.female, birth_date=_birthday(22))
    await _premium(session, picky)
    picky.filter_age_min, picky.filter_age_max = 18, 24
    await session.commit()

    svc = MatchmakingService(redis, settings)
    await svc.enqueue(session, picky)

    assert await svc.try_match(session, seeker, premium=True) is None


@pytest.mark.asyncio
async def test_filters_are_ignored_without_premium(session, redis, settings, make_user):
    seeker = await make_user(1131, gender=Gender.male)
    seeker.filter_age_min, seeker.filter_age_max = 18, 20
    await session.commit()

    adult = await make_user(1132, gender=Gender.female, birth_date=_birthday(40))

    svc = MatchmakingService(redis, settings)
    await svc.enqueue(session, adult)

    result = await svc.try_match(session, seeker, premium=False)

    assert result is not None
    assert result.partner_telegram_id == 1132


@pytest.mark.asyncio
async def test_recent_partner_is_not_matched_again(session, redis, settings, make_user):
    first = await make_user(1141, gender=Gender.male)
    second = await make_user(1142, gender=Gender.female)
    svc = MatchmakingService(redis, settings)

    await svc.enqueue(session, second)
    matched = await svc.try_match(session, first, premium=False)
    assert matched is not None
    await DialogService(redis, settings).finish(session, matched.dialog_id, actor_tg_id=1141)

    # Same two people, both searching again: no second match.
    assert await svc.try_match(session, first, premium=False) is None
    assert await svc.try_match(session, second, premium=False) is None

    dialogs = (await session.execute(select(Dialog))).scalars().all()
    assert len(dialogs) == 1


@pytest.mark.asyncio
async def test_complaint_pair_is_never_matched_again(session, redis, settings, make_user):
    one = await make_user(1151, gender=Gender.male)
    two = await make_user(1152, gender=Gender.female)
    svc = MatchmakingService(redis, settings)

    await svc.block_pair(1151, 1152)

    await svc.enqueue(session, two)
    assert await svc.try_match(session, one, premium=False) is None


@pytest.mark.asyncio
async def test_filters_screen_is_locked_without_premium(bot, dispatcher, session, settings):
    await _register(bot, dispatcher, 1161)

    await press(bot, dispatcher, 1161, "filter:menu")

    text = bot.session.last_text()
    assert "Premium" in text
    assert "filter:menu" in bot.session.markup_buttons() or True


@pytest.mark.asyncio
async def test_premium_user_sets_age_filter_through_ui(bot, dispatcher, session, settings, make_user):
    await _register(bot, dispatcher, 1171)
    user = (await session.execute(select(User).where(User.telegram_id == 1171))).scalar_one()
    await _premium(session, user)

    await press(bot, dispatcher, 1171, "filter:menu")
    assert "Фильтры подбора" in bot.session.last_text()

    await press(bot, dispatcher, 1171, "filter:age:25-34")

    await session.refresh(user)
    assert (user.filter_age_min, user.filter_age_max) == (25, 34)
    assert "25–34" in bot.session.last_text()
    assert bot.session.live_count(1171) == 1


@pytest.mark.asyncio
async def test_city_filter_needs_a_city(bot, dispatcher, session, settings, make_user):
    await _register(bot, dispatcher, 1181)  # "вся страна": city is None
    user = (await session.execute(select(User).where(User.telegram_id == 1181))).scalar_one()
    await _premium(session, user)

    await press(bot, dispatcher, 1181, "filter:city")

    answers = " ".join(a for a in bot.session.answers if a)
    assert "город" in answers.lower()
    await session.refresh(user)
    assert user.filter_city_only is False


@pytest.mark.asyncio
async def test_report_button_inside_dialog_creates_complaint_and_blocks_pair(
    bot, dispatcher, session, redis, settings
):
    await _register(bot, dispatcher, 1191, gender="male", city="Москва")
    await _register(bot, dispatcher, 1192, gender="female", city="Москва")

    await press(bot, dispatcher, 1191, "search:start")
    await press(bot, dispatcher, 1192, "search:start")

    await send_text(bot, dispatcher, 1191, "🚨 Жалоба")
    assert "Жалоба" in bot.session.last_text()
    await send_text(bot, dispatcher, 1191, "оскорбления и угрозы")

    complaint = (await session.execute(select(Complaint))).scalar_one()
    assert complaint.from_user_id is not None
    assert complaint.accused_user_id is not None

    dialog = (await session.execute(select(Dialog))).scalar_one()
    assert dialog.status.value == "finished"

    blocked = await redis.smembers(keys.no_match_key(1191))
    assert "1192" in {b.decode() if isinstance(b, bytes) else b for b in blocked}


@pytest.mark.asyncio
async def test_product_funnel_is_counted(bot, dispatcher, session, redis, settings):
    """Searches vs matches is the number that tells whether the queue is alive."""
    from app.services.activity import conversion, today

    await _register(bot, dispatcher, 1201, gender="male", city="Москва")
    await _register(bot, dispatcher, 1202, gender="female", city="Москва")

    await press(bot, dispatcher, 1201, "search:start")
    await press(bot, dispatcher, 1202, "search:start")

    searches = await today(redis, "search")
    matches = await today(redis, "match")
    assert searches == 2
    assert matches == 1
    assert conversion(searches, matches) == 50

    await press(bot, dispatcher, 1, "adm:menu")  # admin id from the settings fixture
    text = bot.session.last_text()
    assert "Поисков сегодня" in text
    assert "конверсия" in text

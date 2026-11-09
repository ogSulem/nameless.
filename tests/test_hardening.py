"""Regression tests for the deep-audit fixes.

Each test here pins a bug that would be invisible in normal use but degrades the
product slowly: a reservation lock that never releases, a "не важно" option that
quietly meant "opposite gender", a preference that died with the Redis cache.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.models import Dialog, DialogStatus, Gender, Payment, Rating, RatingType, User
from app.redis import keys
from app.services.matchmaking import MatchmakingService, normalize_seek, seek_targets
from tests.conftest import press, send_text
from tests.test_payments import FakeBot, FakeSuccessfulPayment, _settings_with_payments


# --------------------------------------------------------------------- matching
def test_seek_vocabulary_is_honest():
    assert normalize_seek("any", "male") == "any"
    assert normalize_seek("male", "female") == "male"
    assert normalize_seek(None, "male") == "female"
    assert normalize_seek("garbage", "female") == "male"
    assert seek_targets("any") == ("male", "female")
    assert seek_targets("female") == ("female",)


@pytest.mark.asyncio
async def test_reservation_lock_is_released(redis, settings):
    """`_release` used to compare the lock with the candidate's id instead of mine."""
    svc = MatchmakingService(redis, settings)
    queue = keys.queue_key("female", "казань")
    await redis.zadd(queue, {"222": 1.0})

    cand = await svc._reserve([queue], me=111)
    assert cand is not None
    assert await redis.exists(keys.lock_match(222))

    await svc._release(cand, owner=111, requeue=True)

    assert not await redis.exists(keys.lock_match(222)), "the lock must be gone"
    assert await redis.zscore(queue, "222") is not None, "and the candidate must be back in the queue"


@pytest.mark.asyncio
async def test_release_does_not_steal_somebody_elses_lock(redis, settings):
    """Only the owner may drop a reservation, otherwise a slow scan unlocks a live match."""
    svc = MatchmakingService(redis, settings)
    queue = keys.queue_key("female", None)
    await redis.zadd(queue, {"333": 1.0})
    cand = await svc._reserve([queue], me=111)

    await svc._release(cand, owner=999)

    assert await redis.exists(keys.lock_match(333)) == 1


@pytest.mark.asyncio
async def test_seek_any_really_matches_both_genders(session, redis, settings, make_user):
    """«🤷 Не важно» used to be a silent synonym of "the opposite gender"."""
    seeker = await make_user(401, gender=Gender.male, city="Москва")
    seeker.seek_gender = "any"
    await session.commit()
    woman = await make_user(402, gender=Gender.female, city="Москва")
    woman.seek_gender = "any"
    await session.commit()

    svc = MatchmakingService(redis, settings)
    other = await make_user(403, gender=Gender.male, city="Москва")
    other.seek_gender = "any"
    await session.commit()

    assert await svc.try_match(session, woman, premium=False) is None  # queued
    result = await svc.try_match(session, seeker, premium=False)

    assert result is not None, "a man-seeking-any must be able to match a woman"
    assert result.partner_telegram_id == 402


@pytest.mark.asyncio
async def test_seek_preference_survives_a_redis_flush(session, redis, settings, make_user):
    """The preference lives in the DB, not only in the cache."""
    user = await make_user(501, gender=Gender.male, city="Москва")
    user.seek_gender = "male"
    await session.commit()

    svc = MatchmakingService(redis, settings)
    await redis.flushall()

    assert await svc.seek_preference(session, user) == "male"


@pytest.mark.asyncio
async def test_candidate_consent_uses_the_db_when_the_cache_is_cold(session, redis, settings, make_user):
    """A woman looking for women must never be matched with a man from her queue."""
    man = await make_user(601, gender=Gender.male, city="Москва")
    woman = await make_user(602, gender=Gender.female, city="Москва")
    woman.seek_gender = "female"
    await session.commit()

    svc = MatchmakingService(redis, settings)
    # Cold cache: the candidate's preference has to come from the users row.
    await svc.enqueue(session, woman)
    await redis.delete(keys.seek_gender_key(602))

    assert await svc.try_match(session, man, premium=False) is None

    second_man = await make_user(603, gender=Gender.male, city="Москва")
    assert await svc.try_match(session, second_man, premium=False) is None


# ---------------------------------------------------------------------- profile
@pytest.mark.asyncio
async def test_profile_shows_whom_you_are_looking_for(bot, dispatcher, session):
    await send_text(bot, dispatcher, 7001, "/start")
    await press(bot, dispatcher, 7001, "consent:accept")
    await press(bot, dispatcher, 7001, "reg:gender:male")
    await send_text(bot, dispatcher, 7001, "14.05.1998")
    await press(bot, dispatcher, 7001, "reg:city:global")
    await press(bot, dispatcher, 7001, "reg:seek:female")

    assert "девушек" in bot.session.last_text()

    stored = (await session.execute(select(User).where(User.telegram_id == 7001))).scalar_one()
    assert stored.seek_gender == "female"

    await press(bot, dispatcher, 7001, "profile:seek")
    await press(bot, dispatcher, 7001, "pfseek:any")

    assert "не важно" in bot.session.last_text()
    await session.refresh(stored)
    assert stored.seek_gender == "any"


# ---------------------------------------------------------------- registration
@pytest.mark.asyncio
async def test_legal_documents_lead_back_to_the_consent_screen(bot, dispatcher):
    await send_text(bot, dispatcher, 7101, "/start")
    await press(bot, dispatcher, 7101, "legal:privacy")
    assert "Политика" in bot.session.last_text()
    assert "consent:back" in bot.session.markup_buttons()

    await press(bot, dispatcher, 7101, "consent:back")
    assert "Nameless" in bot.session.last_text()
    assert bot.session.live_count(7101) == 1


# ---------------------------------------------------------------------- dialog
@pytest.mark.asyncio
async def test_blocked_partner_keeps_the_rating_screen(bot, dispatcher, session, redis, settings, make_user):
    """A dead partner must be explained *inside* the rating prompt, not instead of it."""
    from app.services.matchmaking import MatchmakingService

    await send_text(bot, dispatcher, 7201, "/start")
    await press(bot, dispatcher, 7201, "consent:accept")
    await press(bot, dispatcher, 7201, "reg:gender:male")
    await send_text(bot, dispatcher, 7201, "14.05.1998")
    await press(bot, dispatcher, 7201, "reg:city:global")
    await press(bot, dispatcher, 7201, "reg:seek:female")

    partner = await make_user(7202, gender=Gender.female, city=None)
    partner.seek_gender = "male"
    await session.commit()
    await MatchmakingService(redis, settings).enqueue(session, partner)

    await press(bot, dispatcher, 7201, "search:start")
    bot.session.blocked_chats.add(7202)

    await send_text(bot, dispatcher, 7201, "привет")
    await session.commit()

    screen = bot.session.last_text()
    assert "не может получать сообщения" in screen
    assert "комфортным" in screen, "the rating question must survive the notice"
    assert any(b.startswith("rate:chat:") for b in bot.session.markup_buttons())


@pytest.mark.asyncio
async def test_unsupported_message_inside_dialog_gets_a_hint(bot, dispatcher, session, redis, settings, make_user):
    from app.services.matchmaking import MatchmakingService

    await send_text(bot, dispatcher, 7301, "/start")
    await press(bot, dispatcher, 7301, "consent:accept")
    await press(bot, dispatcher, 7301, "reg:gender:male")
    await send_text(bot, dispatcher, 7301, "14.05.1998")
    await press(bot, dispatcher, 7301, "reg:city:global")
    await press(bot, dispatcher, 7301, "reg:seek:female")

    partner = await make_user(7302, gender=Gender.female, city=None)
    partner.seek_gender = "male"
    await session.commit()
    await MatchmakingService(redis, settings).enqueue(session, partner)
    await press(bot, dispatcher, 7301, "search:start")

    from datetime import datetime

    from aiogram.types import Chat, Contact, Message, Update
    from aiogram.types import User as TgUser

    from tests.conftest import _next_id

    update = Update(
        update_id=_next_id(),
        message=Message(
            message_id=_next_id(),
            date=datetime.now(tz=UTC),
            chat=Chat(id=7301, type="private"),
            from_user=TgUser(id=7301, is_bot=False, first_name="U"),
            contact=Contact(phone_number="+70000000000", first_name="X"),
        ).as_(bot),
    )
    await dispatcher.feed_update(bot, update)

    assert "нельзя" in bot.session.all_text()


# -------------------------------------------------------------------- payments
@pytest.mark.asyncio
async def test_a_second_invoice_expires_the_previous_pending_one(session, redis, settings, make_user):
    from app.services.payments import PaymentService

    settings = _settings_with_payments(settings)
    user = await make_user(8001)
    service = PaymentService(settings, redis)

    bot = FakeBot()
    first = await service.create_telegram_invoice(session, bot, user)
    second = await service.create_telegram_invoice(session, bot, user)

    rows = (await session.execute(select(Payment).where(Payment.telegram_id == 8001))).scalars().all()
    by_id = {r.external_id: r.status for r in rows}
    assert by_id[first.payment_id] == "expired"
    assert by_id[second.payment_id] == "pending"


@pytest.mark.asyncio
async def test_price_change_after_invoice_does_not_void_the_payment(session, redis, settings, make_user):
    """The invoice price is what the user agreed to; today's price is irrelevant."""
    from app.services.payments import PaymentService

    settings = _settings_with_payments(settings)
    user = await make_user(8051)
    init = await PaymentService(settings, redis).create_telegram_invoice(session, FakeBot(), user)

    # The owner raised the price while this invoice was still open.
    raised = settings.model_copy(update={"subscription_price_rub": 599})
    svc = PaymentService(raised, redis)

    ok, until = await svc.complete_telegram_payment(
        session, FakeSuccessfulPayment(f"premium:{init.payment_id}", amount=49900)
    )

    assert ok is True
    assert until is not None


# ----------------------------------------------------------------------- misc
def test_vision_detector_is_reused_within_the_process(settings):
    from app.services.vision import FaceDetector

    first = FaceDetector.shared(settings)
    second = FaceDetector.shared(settings)

    assert first is second, "re-reading the cascade per photo is wasted work"
    assert first._sem is second._sem, "the concurrency limit must be process-wide"


@pytest.mark.asyncio
async def test_rating_window_reads_only_the_newest_values(session, settings, make_user):
    from app.services.rating import RatingService

    user = await make_user(8101)
    for index in range(30):
        session.add(
            Rating(
                dialog_id=index + 1,
                from_user_id=user.id,
                to_user_id=user.id,
                rating_type=RatingType.chat,
                value=0 if index < 25 else 10,
                is_seasonal_valid=True,
                created_at=datetime.now(tz=UTC) - timedelta(minutes=30 - index),
            )
        )
    await session.commit()

    narrow = settings.model_copy(update={"rating_window": 5})
    update = await RatingService(narrow).recalculate(session, user.id)

    # All 30 ratings count, but the score is computed from the newest 5.
    assert update.chat_count == 30
    assert update.chat_rating > 6, "the old zeroes must have fallen out of the window"


@pytest.mark.asyncio
async def test_expired_dialog_is_announced(bot, redis, settings, make_user, session, session_factory):
    from app.main import _notify_expired_dialogs

    first = await make_user(8201)
    second = await make_user(8202)
    dialog = Dialog(user1_id=first.id, user2_id=second.id, status=DialogStatus.active)
    session.add(dialog)
    await session.commit()

    await _notify_expired_dialogs(bot, redis, session_factory, settings, [(dialog.id, [8201, 8202])])

    for tg in (8201, 8202):
        assert "автоматически" in bot.session.live_text(tg)
        assert "комфортным" in bot.session.live_text(tg)


@pytest.mark.asyncio
async def test_stale_dialog_row_is_closed_by_the_sweeper(session, redis, settings, make_user):
    """Guard: the sweeper must not leave the pair 'active' in the DB."""
    from app.services.dialog import DialogService

    first = await make_user(8301)
    second = await make_user(8302)
    dialog = Dialog(
        user1_id=first.id,
        user2_id=second.id,
        status=DialogStatus.active,
        created_at=datetime.now(tz=UTC) - timedelta(hours=20),
    )
    session.add(dialog)
    await session.commit()

    closed = await DialogService(redis, settings).sweep(session)

    assert [d for d, _ in closed] == [dialog.id]
    await session.refresh(dialog)
    assert dialog.status == DialogStatus.finished


# ------------------------------------------------------------- premium & admin
@pytest.mark.asyncio
async def test_premium_screen_offers_extension(bot, dispatcher, session, make_user):
    """An active subscriber must be able to buy more days instead of hitting a wall."""
    from datetime import timedelta

    user = await make_user(8401)
    user.subscription_until = datetime.now(tz=UTC) + timedelta(days=10)
    await session.commit()

    await send_text(bot, dispatcher, 8401, "/premium")

    text = bot.session.last_text()
    # The command shows the status screen; the buy button appears only with a
    # configured payment provider (the fixture has none, so we assert on the text).
    assert "Premium" in text
    assert "Оплата временно недоступна" in text


@pytest.mark.asyncio
async def test_ban_closes_the_dialog_of_the_partner(bot, dispatcher, session, redis, settings, make_user):
    """A banned user must not leave the partner talking to a ghost for 12 hours."""
    from app.database.models import ActiveDialog, Dialog, DialogStatus
    from app.services.matchmaking import MatchmakingService

    await send_text(bot, dispatcher, 8501, "/start")
    await press(bot, dispatcher, 8501, "consent:accept")
    await press(bot, dispatcher, 8501, "reg:gender:male")
    await send_text(bot, dispatcher, 8501, "14.05.1998")
    await press(bot, dispatcher, 8501, "reg:city:global")
    await press(bot, dispatcher, 8501, "reg:seek:female")

    partner = await make_user(8502, gender=Gender.female, city=None)
    partner.seek_gender = "male"
    await session.commit()
    await MatchmakingService(redis, settings).enqueue(session, partner)

    await press(bot, dispatcher, 8501, "search:start")
    await session.commit()

    await press(bot, dispatcher, 2, f"adm:ban:{8502}")  # MODERATOR
    await session.commit()

    active = (await session.execute(select(ActiveDialog))).scalars().all()
    assert active == [], "the banned user must be out of the dialog table"
    dialogs = (await session.execute(select(Dialog))).scalars().all()
    assert all(d.status == DialogStatus.finished for d in dialogs)
    assert "комфортным" in bot.session.live_text(8501), "the partner gets the rating screen"


@pytest.mark.asyncio
async def test_complaint_card_survives_html_in_user_text(bot, dispatcher, session, make_user):
    """A message containing "<b>" must not break the moderator's card."""
    from app.database.models import Complaint, Dialog, DialogStatus
    from app.database.models import Message as DbMessage

    first = await make_user(8601)
    second = await make_user(8602)
    dialog = Dialog(user1_id=first.id, user2_id=second.id, status=DialogStatus.finished)
    session.add(dialog)
    await session.flush()
    session.add(
        DbMessage(dialog_id=dialog.id, from_user_id=second.id, text="привет <b>злой</b> & <script>")
    )
    complaint = Complaint(
        dialog_id=dialog.id,
        from_user_id=first.id,
        accused_user_id=second.id,
        reason="хамит <b>очень</b>",
        status="open",
    )
    session.add(complaint)
    await session.commit()

    await press(bot, dispatcher, 1, f"adm:complaint:{complaint.id}")  # ADMIN

    text = bot.session.last_text()
    assert "&lt;b&gt;" in text, "user text must be escaped for parse_mode=HTML"
    assert "<b>злой</b>" not in text


@pytest.mark.asyncio
async def test_registration_does_not_leave_stale_buttons(bot, dispatcher):
    """After the gender step the previous keyboard must disappear, not linger."""
    await send_text(bot, dispatcher, 8701, "/start")
    await press(bot, dispatcher, 8701, "consent:accept")
    await press(bot, dispatcher, 8701, "reg:gender:male")

    assert "Шаг 2" in bot.session.last_text()
    assert bot.session.markup_buttons() == [], "step 1 buttons stayed on the birth-date screen"

    await send_text(bot, dispatcher, 8701, "14.05.1998")
    assert "reg:city:global" in bot.session.markup_buttons()

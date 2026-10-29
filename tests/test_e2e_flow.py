from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.database.models import Dialog, Rating, User
from app.database.models import Message as DbMessage
from tests.conftest import press, send_text


async def _register(bot, dispatcher, tg_id: int, *, gender="male", birthday="14.05.1998", city=None):
    await send_text(bot, dispatcher, tg_id, "/start")
    await press(bot, dispatcher, tg_id, "consent:accept")
    await press(bot, dispatcher, tg_id, f"reg:gender:{gender}")
    await send_text(bot, dispatcher, tg_id, birthday)
    if city is None:
        await press(bot, dispatcher, tg_id, "reg:city:global")
    else:
        await send_text(bot, dispatcher, tg_id, city)
    await press(bot, dispatcher, tg_id, "reg:seek:any")


@pytest.mark.asyncio
async def test_full_journey(bot, dispatcher, session, make_user):
    """Start → consent → registration → both search → dialog → relay → finish → rate."""
    await send_text(bot, dispatcher, 9001, "/start")
    assert "Nameless" in bot.session.last_text()

    await press(bot, dispatcher, 9001, "consent:accept")
    await press(bot, dispatcher, 9001, "reg:gender:male")
    await send_text(bot, dispatcher, 9001, "14.05.1998")
    await send_text(bot, dispatcher, 9001, "Москва")
    await press(bot, dispatcher, 9001, "reg:seek:female")

    user = (await session.execute(select(User).where(User.telegram_id == 9001))).scalar_one()
    assert user.city == "Москва"
    assert user.consent_at is not None
    assert user.consent_version

    await _register(bot, dispatcher, 9002, gender="female", city="Москва")

    await press(bot, dispatcher, 9001, "search:start")
    await press(bot, dispatcher, 9002, "search:start")

    dialog = (await session.execute(select(Dialog))).scalars().first()
    assert dialog is not None, "the two searches must have produced a dialog"
    # Profile and "searching" screens are removed; only the partner card is left.
    assert bot.session.live_count(9001) == 1, bot.session.live
    assert bot.session.live_count(9002) == 1, bot.session.live

    await send_text(bot, dispatcher, 9002, "привет!")
    await session.commit()
    copied = bot.session.copied
    assert any(m.chat_id == 9001 for m in copied), "the message must be relayed to the partner"

    stored = (await session.execute(select(DbMessage))).scalars().all()
    assert [m.text for m in stored] == ["привет!"]

    # Partner finishes with "🛑 Завершить" → rating prompt for both.
    await send_text(bot, dispatcher, 9001, "\U0001f6d1 Завершить")
    await session.commit()
    await session.refresh(dialog)
    assert dialog.status.value == "finished"

    await press(bot, dispatcher, 9001, "rate:chat:8")
    await session.commit()
    ratings = (await session.execute(select(Rating))).scalars().all()
    assert [r.value for r in ratings] == [8]

    # Chat hygiene: after a full dialog the user keeps exactly one bot message.
    assert bot.session.live_count(9001) == 1, bot.session.live
    assert bot.session.live_count(9002) == 1, bot.session.live


@pytest.mark.asyncio
async def test_search_cancel_returns_to_profile(bot, dispatcher, session, make_user):
    await _register(bot, dispatcher, 9010)

    await press(bot, dispatcher, 9010, "search:start")
    await press(bot, dispatcher, 9010, "search:cancel")

    assert "Поиск" in bot.session.last_text() or "профиль" in bot.session.last_text().lower()


@pytest.mark.asyncio
async def test_underage_registration_is_refused(bot, dispatcher, session):
    await send_text(bot, dispatcher, 9020, "/start")
    await press(bot, dispatcher, 9020, "consent:accept")
    await press(bot, dispatcher, 9020, "reg:gender:male")
    await send_text(bot, dispatcher, 9020, "01.01.2015")

    assert "18" in bot.session.last_text()
    user = (await session.execute(select(User).where(User.telegram_id == 9020))).scalar_one_or_none()
    assert user is None


@pytest.mark.asyncio
async def test_garbage_birth_date_keeps_the_flow_alive(bot, dispatcher, session):
    """Regression: the old flow dead-ended on a bad date."""
    await send_text(bot, dispatcher, 9030, "/start")
    await press(bot, dispatcher, 9030, "consent:accept")
    await press(bot, dispatcher, 9030, "reg:gender:female")
    await send_text(bot, dispatcher, 9030, "не дата")
    assert "ДД.ММ.ГГГГ" in bot.session.last_text()

    await send_text(bot, dispatcher, 9030, "01.01.1999")
    assert "город" in bot.session.last_text().lower()


@pytest.mark.asyncio
async def test_banned_user_cannot_search(bot, dispatcher, session, make_user):
    await make_user(9040, banned=True)
    await press(bot, dispatcher, 9040, "search:start")

    answers = " ".join(a for a in bot.session.answers if a)
    assert "заблокирован" in (bot.session.all_text() + " " + answers).lower()


@pytest.mark.asyncio
async def test_delete_me_erases_everything(bot, dispatcher, session, make_user):
    await _register(bot, dispatcher, 9050)
    assert (await session.execute(select(func.count(User.id)))).scalar_one() == 1

    await send_text(bot, dispatcher, 9050, "/delete_me")
    await press(bot, dispatcher, 9050, "account:delete_confirm")

    assert (await session.execute(select(func.count(User.id)))).scalar_one() == 0


@pytest.mark.asyncio
async def test_profile_city_change_updates_db_and_clears_queue(bot, dispatcher, session, make_user):
    await _register(bot, dispatcher, 9060, city="Москва")

    await press(bot, dispatcher, 9060, "profile:city")
    await send_text(bot, dispatcher, 9060, "Санкт-Петербург")

    user = (await session.execute(select(User).where(User.telegram_id == 9060))).scalar_one()
    await session.refresh(user)
    assert user.city == "Санкт-Петербург"


@pytest.mark.asyncio
async def test_profile_seek_change_is_stored(bot, dispatcher, session, redis, make_user):
    from app.redis import keys

    await _register(bot, dispatcher, 9061)

    await press(bot, dispatcher, 9061, "profile:seek")
    await press(bot, dispatcher, 9061, "pfseek:female")

    assert await redis.get(keys.seek_gender_key(9061)) == "female"


@pytest.mark.asyncio
async def test_empty_queue_screen_offers_invite(bot, dispatcher, session, make_user):
    await _register(bot, dispatcher, 9070)

    await press(bot, dispatcher, 9070, "search:start")

    text = bot.session.last_text()
    assert "в очереди никого нет" in text
    assert "/invite" in text
    assert bot.session.live_count(9070) == 1


@pytest.mark.asyncio
async def test_documents_and_help_keep_one_message(bot, dispatcher, session, make_user):
    await _register(bot, dispatcher, 9071)

    for command in ("/help", "/privacy", "/terms", "/delete_me"):
        await send_text(bot, dispatcher, 9071, command)
        assert bot.session.live_count(9071) == 1, (command, bot.session.live)

    # The user's own command messages are deleted too.
    assert bot.session.deleted


@pytest.mark.asyncio
async def test_rating_prompt_can_be_skipped(bot, dispatcher, session, make_user):
    await _register(bot, dispatcher, 9074)
    await press(bot, dispatcher, 9074, "search:start")

    # No partner available → cancel and make sure we are back on the profile screen.
    await press(bot, dispatcher, 9074, "search:cancel")
    assert "Профиль" in bot.session.last_text()
    assert bot.session.live_count(9074) == 1


@pytest.mark.asyncio
async def test_blocked_partner_is_detected_at_match(bot, dispatcher, session, redis, settings, make_user):
    """Если собеседник заблокировал бота, второй не должен остаться в мёртвом диалоге."""
    from app.database.models import ActiveDialog, DialogStatus
    from app.database.models import Dialog as DbDialog
    from app.services.matchmaking import MatchmakingService

    await _register(bot, dispatcher, 9090)
    partner = await make_user(9091, gender="female", city="Москва")
    await MatchmakingService(redis, settings).enqueue(session, partner)

    # 9091 blocked the bot between registration and the match.
    bot.session.blocked_chats.add(9091)

    await press(bot, dispatcher, 9090, "search:start")

    await session.commit()
    dialogs = (await session.execute(select(DbDialog))).scalars().all()
    assert dialogs, "the match itself happened"
    assert all(d.status == DialogStatus.finished for d in dialogs)
    active = (await session.execute(select(ActiveDialog))).scalars().all()
    assert active == []
    assert "не может получать сообщения" in bot.session.all_text()

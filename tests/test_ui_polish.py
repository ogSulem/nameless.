"""No redundant renders, no flicker, nothing lost on screen.

Every test here answers one question: what does the person actually *see*, and
how many times does the screen change on the way there?
"""

from __future__ import annotations

import pytest
from aiogram.methods import EditMessageText, SendMessage

from app.database.models import Gender, User
from app.redis import keys
from app.services.dialog import DialogService
from app.services.matchmaking import MatchmakingService
from app.ui import edit_ui
from tests.conftest import press, send_text


def _edits(bot) -> int:
    return sum(1 for method in bot.session.sent if isinstance(method, EditMessageText))


async def _registered(bot, dispatcher, tg_id: int, gender: str = "male") -> None:
    await send_text(bot, dispatcher, tg_id, "/start")
    await press(bot, dispatcher, tg_id, "consent:accept")
    await press(bot, dispatcher, tg_id, f"reg:gender:{gender}")
    await send_text(bot, dispatcher, tg_id, "30")
    await press(bot, dispatcher, tg_id, "reg:city:global")
    await press(bot, dispatcher, tg_id, "reg:seek:any")


async def _paired(bot, dispatcher, session, redis, settings, me: int, other: int) -> int:
    await _registered(bot, dispatcher, me)
    partner = User(
        telegram_id=other,
        gender=Gender.female,
        birth_date=__import__("datetime").date(1996, 5, 5),
        city=None,
        seek_gender="any",
        is_banned=False,
    )
    session.add(partner)
    await session.commit()
    await MatchmakingService(redis, settings).enqueue(session, partner)
    await press(bot, dispatcher, me, "search:start")
    return int(await DialogService(redis, settings).active_dialog_id(session, me))


# --------------------------------------------------------------------- renders
@pytest.mark.asyncio
async def test_identical_screen_is_not_rendered_twice(bot, dispatcher, redis):
    """Tapping the same button twice must not cost an API call (and a flicker)."""
    await send_text(bot, dispatcher, 9101, "/start")
    before = _edits(bot)

    await edit_ui(bot, redis, 9101, "Один и тот же текст")
    once = _edits(bot)
    await edit_ui(bot, redis, 9101, "Один и тот же текст")
    twice = _edits(bot)

    assert once > before, "the first render must reach Telegram"
    assert twice == once, "an unchanged screen must not be sent again"

    # A real change still goes through.
    await edit_ui(bot, redis, 9101, "Другой текст")
    assert _edits(bot) == twice + 1


@pytest.mark.asyncio
async def test_new_message_is_remembered_as_current_screen(bot, dispatcher, redis):
    """After a fresh send, editing the *same* text is a no-op too."""
    from app.ui import send_new_ui

    msg = await send_new_ui(bot, redis, 9102, "Экран")
    assert msg is not None
    before = _edits(bot)
    await edit_ui(bot, redis, 9102, "Экран")
    assert _edits(bot) == before


# ------------------------------------------------------------- dialog finishing
@pytest.mark.asyncio
async def test_dialog_end_shows_the_survey_in_one_frame(bot, dispatcher, session, redis, settings):
    """No intermediate "Диалог завершён" text flashing before the survey."""
    await _paired(bot, dispatcher, session, redis, settings, 9103, 9104)

    await send_text(bot, dispatcher, 9103, "привет")
    await send_text(bot, dispatcher, 9103, "🛑 Завершить")

    sent_texts = [m.text for m in bot.session.sent if getattr(m, "text", None)]
    assert not any("Диалог завершён" in t for t in sent_texts)
    assert "комфортным" in bot.session.last_text()
    assert any(b.startswith("rate:chat:") for b in bot.session.markup_buttons())


# ----------------------------------------------------------------- reference UI
@pytest.mark.asyncio
async def test_help_does_not_destroy_the_partner_card(bot, dispatcher, session, redis, settings):
    """During a dialog the card is the only place the partner's info lives."""
    await _paired(bot, dispatcher, session, redis, settings, 9105, 9106)
    card = bot.session.live_text(9105)
    assert "Собеседник найден" in card

    await send_text(bot, dispatcher, 9105, "/help")

    assert "Собеседник найден" in bot.session.live_text(9105), "the card was overwritten"
    assert any("Как это работает" in t for t in bot.session.texts())


@pytest.mark.asyncio
async def test_help_edits_the_screen_when_nothing_is_at_stake(bot, dispatcher, session):
    """Outside a dialog it is the same single screen, edited in place."""
    await _registered(bot, dispatcher, 9107)
    before = sum(1 for m in bot.session.sent if isinstance(m, SendMessage))

    await send_text(bot, dispatcher, 9107, "/help")

    assert "Как это работает" in bot.session.last_text()
    after = sum(1 for m in bot.session.sent if isinstance(m, SendMessage))
    assert after == before, "delete+send would have created a second screen"
    assert bot.session.live_count(9107) == 1


@pytest.mark.asyncio
async def test_help_during_registration_keeps_the_consent_screen(bot, dispatcher, session):
    """A person who asks for help must not lose the age-gate screen."""
    await send_text(bot, dispatcher, 9108, "/start")
    await send_text(bot, dispatcher, 9108, "/help")

    assert "Nameless" in bot.session.live_text(9108), "the consent screen must survive"


# --------------------------------------------------------------------- profiles
@pytest.mark.asyncio
async def test_profile_edit_returns_to_the_profile_without_a_flash(bot, dispatcher, session, redis, settings):
    await _registered(bot, dispatcher, 9109)
    await press(bot, dispatcher, 9109, "profile:city")
    await send_text(bot, dispatcher, 9109, "Казань")

    assert "Казань" in bot.session.last_text()
    assert bot.session.live_count(9109) == 1


@pytest.mark.asyncio
async def test_survey_end_lands_on_the_profile_in_one_render(bot, dispatcher, session, redis, settings):
    await _paired(bot, dispatcher, session, redis, settings, 9110, 9111)
    await send_text(bot, dispatcher, 9110, "🛑 Завершить")
    before = _edits(bot)

    await press(bot, dispatcher, 9110, "rate:chat:9")

    assert "Профиль" in bot.session.last_text()
    assert _edits(bot) == before + 1, "an intermediate 'спасибо' frame would be +2"


@pytest.mark.asyncio
async def test_complaint_receipt_stays_on_screen(bot, dispatcher, session, redis, settings):
    """The complaint number is worth keeping, not flashing past."""
    await _paired(bot, dispatcher, session, redis, settings, 9112, 9113)
    await send_text(bot, dispatcher, 9112, "🛑 Завершить")
    await press(bot, dispatcher, 9112, "complaint:start")
    await send_text(bot, dispatcher, 9112, "оскорблял в переписке")

    screen = bot.session.last_text()
    assert "Жалоба принята" in screen
    assert "menu:profile" in bot.session.markup_buttons()


@pytest.mark.asyncio
async def test_pending_rating_state_is_untouched_by_help(bot, dispatcher, session, redis, settings):
    """A reference screen must not eat the survey."""
    await _paired(bot, dispatcher, session, redis, settings, 9114, 9115)
    await send_text(bot, dispatcher, 9114, "🛑 Завершить")

    await send_text(bot, dispatcher, 9114, "/help")
    await press(bot, dispatcher, 9114, "rate:chat:10")

    screen = bot.session.last_text()
    assert "Профиль" in screen
    assert await redis.get(keys.pending_rating(9114)) is None


@pytest.mark.asyncio
async def test_help_inside_a_dialog_is_html_and_self_deleting(bot, dispatcher, session, make_user):
    """/help during a chat must not touch the partner card, and it is HTML."""
    from datetime import date

    from app.database.models import Gender

    for tg, gender in ((9301, Gender.male), (9302, Gender.female)):
        await make_user(tg, gender=gender, city=None, birth_date=date(1996, 5, 5))
        await send_text(bot, dispatcher, tg, "/start")
    await press(bot, dispatcher, 9301, "search:start")
    await press(bot, dispatcher, 9302, "search:start")
    live_before = bot.session.live_count(9301)

    await send_text(bot, dispatcher, 9301, "/help")
    sent = [m for m in bot.session.sent if isinstance(m, SendMessage)]
    assert sent and sent[-1].parse_mode == "HTML"
    assert bot.session.live_count(9301) >= live_before, "the partner card must stay"

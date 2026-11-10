"""Every inline button the bot can draw must lead somewhere.

The catch-all callback handler answers "Кнопка устарела": that is the right
answer for an old message, but it must never be the answer for a button the bot
has just rendered. This test renders the keyboards, fires every callback and
fails if any of them falls through to the catch-all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from aiogram.fsm.storage.base import StorageKey

from app.flows.states import ProfileEdit, Registration
from app.keyboards.admin import (
    admin_menu_kb,
    back_menu_kb,
    complaint_card_kb,
    complaints_list_kb,
    dialogs_kb,
    promos_kb,
    user_card_kb,
)
from app.keyboards.payment import payment_kb
from app.keyboards.profile import delete_confirm_kb, legal_kb, profile_kb
from app.keyboards.rating import complaint_prompt_kb, rating_kb
from app.keyboards.search import searching_kb
from app.keyboards.start import back_kb, city_kb, consent_kb, gender_kb, seek_gender_kb
from tests.conftest import press

FALLBACK_ANSWER = "Кнопка устарела"


def _data(markup) -> list[str]:
    out: list[str] = []
    for row in markup.inline_keyboard:
        for button in row:
            if button.callback_data:
                out.append(button.callback_data)
    return out


def _keyboards() -> list[object]:
    return [
        admin_menu_kb(role="admin", open_complaints=1),
        back_menu_kb(),
        complaints_list_kb(items=[(1, 'спам', '01.01 10:00')], page=1, has_next=True),
        complaint_card_kb(complaint_id=1, accused_tg=42),
        user_card_kb(tg_id=42, banned=False),
        dialogs_kb(tg_id=42, dialog_ids=[1, 2], page=1),
        promos_kb([(1, True)]),
        payment_kb("https://t.me/$test"),
        profile_kb(premium=False, payments_enabled=False, premium_mode="chat"),
        profile_kb(premium=True, payments_enabled=True, premium_mode="appearance"),
        legal_kb(),
        delete_confirm_kb(),
        rating_kb("rate:chat"),
        rating_kb("rate:app"),
        complaint_prompt_kb(),
        searching_kb(),
        back_kb(),
        city_kb(),
        consent_kb(),
        gender_kb("reg:gender"),
        seek_gender_kb("reg:seek"),
        seek_gender_kb("pfseek"),
        city_kb("profile:city:global"),
    ]


def _sample_callbacks() -> list[str]:
    """Every literal callback the keyboards build, plus the ones handlers build inline."""
    found: set[str] = set()
    for markup in _keyboards():
        found.update(_data(markup))
    for path in Path("app").rglob("*.py"):
        for literal in re.findall(r'callback_data="([a-z0-9:_-]{3,64})"', path.read_text(encoding="utf-8")):
            found.add(literal)
    return sorted(found)


# Mirrors the state filters on the handlers in app/handlers/commands.py.
STATES = {
    "consent:accept": Registration.consent.state,
    "consent:back": Registration.consent.state,
    "reg:gender": Registration.gender.state,
    "reg:city:global": Registration.city.state,
    "reg:seek": Registration.seek.state,
    "profile:city:global": ProfileEdit.city.state,
}

# Deleting the account removes the very user this test drives, so it is covered
# by the e2e flow instead.
SKIP = {"account:delete_confirm", "account:delete_ask"}


def _state_for(data: str) -> str | None:
    if data in STATES:
        return STATES[data]
    for prefix, state in STATES.items():
        if data.startswith(prefix + ":"):
            return state
    return None


@pytest.mark.asyncio
async def test_no_button_falls_into_the_catch_all(bot, dispatcher, session, redis, make_user):
    from datetime import date

    from app.database.models import Gender

    # A real registered user, so menus/permissions behave like production.
    tg = 9777
    await make_user(tg, gender=Gender.male, city="Москва", birth_date=date(1994, 3, 3))
    from tests.conftest import send_text

    await send_text(bot, dispatcher, tg, "/start")
    key = StorageKey(bot_id=bot.id, chat_id=tg, user_id=tg)

    dead: list[str] = []
    for data in _sample_callbacks():
        if data in SKIP or data.startswith("noop") or data.startswith("adm:"):
            # Admin screens need a second staff identity; covered by
            # test_admin_flow.py instead.
            continue
        state = _state_for(data)
        await dispatcher.storage.set_state(key, state)
        bot.session.answers.clear()
        await press(bot, dispatcher, tg, data)
        if FALLBACK_ANSWER in bot.session.answers:
            dead.append(data)
        await dispatcher.storage.set_state(key, None)

    assert not dead, f"buttons that lead nowhere: {dead}"


@pytest.mark.asyncio
async def test_admin_buttons_all_lead_somewhere(bot, dispatcher, session, redis, make_user):
    from app.database.models import Gender
    from tests.conftest import send_text

    tg = 1  # in ADMINS (see conftest settings)
    await make_user(tg, gender=Gender.male, city="Москва")
    await make_user(4242, gender=Gender.female, city="Москва")
    await send_text(bot, dispatcher, tg, "/admin")

    dead: list[str] = []
    for data in _sample_callbacks():
        if not data.startswith("adm:"):
            continue
        bot.session.answers.clear()
        await press(bot, dispatcher, tg, data)
        if FALLBACK_ANSWER in bot.session.answers:
            dead.append(data)

    assert not dead, f"admin buttons that lead nowhere: {dead}"

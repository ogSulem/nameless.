from __future__ import annotations

import os
from datetime import date

import pytest
import pytest_asyncio

os.environ.setdefault("BOT_TOKEN", "123456789:TEST-TOKEN")
os.environ.setdefault("ADMINS", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import fakeredis.aioredis  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.config import Settings  # noqa: E402
from app.database.models import Base, Gender, User  # noqa: E402


@pytest.fixture
def settings() -> Settings:
    return Settings(
        BOT_TOKEN="123456789:TEST-TOKEN",
        ADMINS="1",
        MODERATORS="2",
        SUPPORT="3",
        ALERTS_CHAT_ID=0,
        SUBSCRIPTION_PRICE_RUB=499,
        SUBSCRIPTION_DAYS=30,
        MIN_AGE=18,
        ONLINE_TTL_S=900,
        DIALOG_MAX_HOURS=12,
        RATING_MIN_COUNT=3,
        PREMIUM_MIN_RATING=7.0,
        PAYMENTS_PROVIDER_TOKEN="",
        YOOKASSA_SHOP_ID="",
        YOOKASSA_SECRET_KEY="",
        VISION_ENABLED=False,
    )


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def session(session_factory):
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest_asyncio.fixture
async def make_user(session):
    async def _make(
        tg_id: int,
        *,
        gender: Gender = Gender.male,
        city: str | None = "Москва",
        rating: float = 0.0,
        ratings_count: int = 0,
        banned: bool = False,
        birth_date: date = date(1995, 1, 1),
    ) -> User:
        user = User(
            telegram_id=tg_id,
            gender=gender,
            birth_date=birth_date,
            city=city,
            season_rating_chat=rating,
            chat_ratings_count=ratings_count,
            is_banned=banned,
            is_deleted=False,
            is_under_review=False,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user

    return _make


# ---------------------------------------------------------------- e2e harness
from datetime import UTC, datetime  # noqa: E402

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.fsm.storage.redis import RedisStorage  # noqa: E402
from aiogram.types import CallbackQuery, Chat, Message, Update  # noqa: E402
from aiogram.types import User as TgUser  # noqa: E402

from app.middlewares.app_context import AppContextMiddleware  # noqa: E402
from app.middlewares.error_boundary import ErrorBoundaryMiddleware  # noqa: E402
from tests.fake_telegram import FakeSession, fresh_router  # noqa: E402

_counter = {"n": 100}


def _next_id() -> int:
    _counter["n"] += 1
    return _counter["n"]


@pytest.fixture
def bot():
    bot = Bot(token="123456789:E2E-TEST-TOKEN")
    bot.session = FakeSession()
    return bot


@pytest.fixture
def dispatcher(bot, session_factory, settings, redis):
    from aiogram import BaseMiddleware

    dp = Dispatcher(storage=RedisStorage(redis=redis))
    dp.update.middleware(AppContextMiddleware(settings=settings, redis=redis))

    class Ses(BaseMiddleware):
        async def __call__(self, handler, event, data):
            async with session_factory() as session:
                data["session"] = session
                return await handler(event, data)

    dp.update.middleware(Ses())
    dp.update.middleware(ErrorBoundaryMiddleware(settings=settings, redis=redis))
    dp.include_router(fresh_router())
    return dp


async def send_text(bot, dispatcher, tg_id: int, text: str):
    update = Update(
        update_id=_next_id(),
        message=Message(
            message_id=_next_id(),
            date=datetime.now(tz=UTC),
            chat=Chat(id=tg_id, type="private"),
            from_user=TgUser(id=tg_id, is_bot=False, first_name=f"U{tg_id}"),
            text=text,
        ).as_(bot),
    )
    return await dispatcher.feed_update(bot, update)


async def press(bot, dispatcher, tg_id: int, data: str):
    update = Update(
        update_id=_next_id(),
        callback_query=CallbackQuery(
            id=f"cb{_next_id()}",
            from_user=TgUser(id=tg_id, is_bot=False, first_name=f"U{tg_id}"),
            chat_instance="ci",
            data=data,
            message=Message(
                message_id=1,
                date=datetime.now(tz=UTC),
                chat=Chat(id=tg_id, type="private"),
                text="ui",
            ).as_(bot),
        ).as_(bot),
    )
    return await dispatcher.feed_update(bot, update)

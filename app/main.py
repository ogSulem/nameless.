from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError
from aiogram.fsm.storage.redis import RedisStorage

from app.config import Settings
from app.database.session import create_engine, create_sessionmaker
from app.handlers.router import build_router
from app.logging.setup import setup_logging
from app.middlewares.logging import LoggingMiddleware
from app.middlewares.app_context import AppContextMiddleware
from app.middlewares.callback_dedupe import CallbackDedupeMiddleware
from app.middlewares.message_dedupe import MessageDedupeMiddleware
from app.middlewares.db_session import DbSessionMiddleware
from app.middlewares.error_boundary import ErrorBoundaryMiddleware
from app.redis.client import create_redis


logger = logging.getLogger(__name__)


async def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level)

    redis = create_redis(
        settings.redis_host, 
        settings.redis_port, 
        settings.redis_db, 
        settings.redis_username,
        settings.redis_password,
        settings.redis_url
    )

    engine = create_engine(settings.database_dsn)
    session_factory = create_sessionmaker(engine)

    bot = Bot(token=settings.bot_token)
    
    # Ensure RedisStorage uses the same redis client with authentication
    storage = RedisStorage(redis=redis)
    dp = Dispatcher(storage=storage)

    dp.include_router(build_router())

    dp.update.middleware(LoggingMiddleware())
    dp.update.middleware(AppContextMiddleware(settings=settings, redis=redis))
    dp.update.middleware(CallbackDedupeMiddleware(redis=redis))
    dp.update.middleware(MessageDedupeMiddleware(redis=redis))
    dp.update.middleware(ErrorBoundaryMiddleware(settings=settings, redis=redis))
    dp.update.middleware(DbSessionMiddleware(session_factory))

    logger.info("bot_start")

    delay_s = 1
    while True:
        try:
            await dp.start_polling(bot)
            logger.info("polling_stopped")
            delay_s = 1
        except TelegramConflictError as e:
            # Happens when multiple bot instances use long polling (getUpdates) at once.
            # In production, you must ensure only one instance is running.
            logger.exception("telegram_conflict_error: %s", e)
            delay_s = max(delay_s, 15)
        except (TelegramNetworkError, ConnectionResetError) as e:
            logger.exception("telegram_network_error: %s", e)
        except Exception as e:
            logger.exception("polling_crashed: %s", e)

        try:
            await bot.session.close()
        except Exception:
            pass

        await asyncio.sleep(delay_s)
        delay_s = min(delay_s * 2, 60)
        bot = Bot(token=settings.bot_token)


if __name__ == "__main__":
    asyncio.run(main())

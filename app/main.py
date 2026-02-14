from __future__ import annotations

import asyncio
import logging
import secrets

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError
from aiogram.fsm.storage.redis import RedisStorage

from app.config import Settings
from app.database.session import create_engine, create_sessionmaker
from app.handlers.router import build_router
from app.health_server import start_health_server, stop_health_server
from app.logging.setup import setup_logging
from app.middlewares.logging import LoggingMiddleware
from app.middlewares.app_context import AppContextMiddleware
from app.middlewares.callback_dedupe import CallbackDedupeMiddleware
from app.middlewares.message_dedupe import MessageDedupeMiddleware
from app.middlewares.db_session import DbSessionMiddleware
from app.middlewares.error_boundary import ErrorBoundaryMiddleware
from app.metrics import log_snapshot_and_reset
from app.redis.client import create_redis


logger = logging.getLogger(__name__)


async def _close_bot(bot: Bot | None) -> None:
    if bot is None:
        return
    try:
        await bot.session.close()
    except Exception:
        pass


async def _acquire_singleton_lock(redis) -> tuple[bool, str]:
    key = "lock:singleton:bot"
    value = secrets.token_hex(16)
    try:
        ok = await redis.set(key, value, nx=True, ex=30)
        return (bool(ok), value)
    except Exception:
        # If Redis is down, we can't guarantee singleton; keep running.
        return (True, value)


async def _refresh_singleton_lock(redis, value: str) -> None:
    key = "lock:singleton:bot"
    # Extend TTL only if we still own the lock.
    lua = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('expire', KEYS[1], ARGV[2])
    end
    return 0
    """
    while True:
        try:
            await asyncio.sleep(10)
            await redis.eval(lua, 1, key, value, "30")
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("singleton_lock_refresh_failed")


async def _release_singleton_lock(redis, value: str) -> None:
    key = "lock:singleton:bot"
    lua = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('del', KEYS[1])
    end
    return 0
    """
    try:
        await redis.eval(lua, 1, key, value)
    except Exception:
        pass


async def _close_redis(redis) -> None:
    if redis is None:
        return
    try:
        await redis.close()
    except Exception:
        pass
    try:
        await redis.connection_pool.disconnect(inuse_connections=True)
    except Exception:
        pass


async def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level)

    health_server = None
    try:
        health_server = await start_health_server(host="0.0.0.0", port=int(settings.port))
    except Exception:
        logger.exception("health_server_start_failed port=%s", getattr(settings, "port", None))

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

    lock_value = ""
    lock_refresh_task: asyncio.Task | None = None
    got_lock, lock_value = await _acquire_singleton_lock(redis)
    if not got_lock:
        logger.error("singleton_lock_not_acquired: another instance is running")
        try:
            await stop_health_server(health_server)
        except Exception:
            pass
        await _close_bot(bot)
        await _close_redis(redis)
        try:
            await engine.dispose()
        except Exception:
            pass
        return

    try:
        lock_refresh_task = asyncio.create_task(_refresh_singleton_lock(redis, lock_value))
    except Exception:
        lock_refresh_task = None

    async def _metrics_loop() -> None:
        while True:
            try:
                await asyncio.sleep(60)
                await log_snapshot_and_reset()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("metrics_loop_failed")

    metrics_task = asyncio.create_task(_metrics_loop())

    bot: Bot | None = Bot(token=settings.bot_token)
    delay_s = 1
    try:
        while True:
            try:
                await dp.start_polling(bot)
                logger.info("polling_stopped")
                delay_s = 1
                break
            except asyncio.CancelledError:
                logger.info("polling_cancelled")
                break
            except TelegramConflictError as e:
                # Happens when multiple bot instances use long polling (getUpdates) at once.
                # In production, you must ensure only one instance is running.
                logger.exception("telegram_conflict_error: %s", e)
                delay_s = max(delay_s, 15)
            except (TelegramNetworkError, ConnectionResetError) as e:
                logger.exception("telegram_network_error: %s", e)
            except Exception as e:
                logger.exception("polling_crashed: %s", e)

            await _close_bot(bot)
            bot = None

            await asyncio.sleep(delay_s)
            delay_s = min(delay_s * 2, 60)
            bot = Bot(token=settings.bot_token)
    finally:
        try:
            metrics_task.cancel()
            await metrics_task
        except Exception:
            pass

        try:
            if lock_refresh_task is not None:
                lock_refresh_task.cancel()
                await lock_refresh_task
        except Exception:
            pass

        try:
            if lock_value:
                await _release_singleton_lock(redis, lock_value)
        except Exception:
            pass
        await _close_bot(bot)
        try:
            await storage.close()
        except Exception:
            pass
        await _close_redis(redis)
        try:
            await engine.dispose()
        except Exception:
            pass
        await stop_health_server(health_server)


if __name__ == "__main__":
    asyncio.run(main())

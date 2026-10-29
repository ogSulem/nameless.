from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import sys
import warnings

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramConflictError, TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand
from aiohttp import web
from redis.asyncio import Redis
from sqlalchemy import select, text

from app.config import Settings
from app.database.models import Payment
from app.database.session import create_engine, create_sessionmaker
from app.handlers.router import build_router
from app.health_server import HealthServer, start_health_server, stop_health_server
from app.logging.setup import setup_logging
from app.metrics import log_snapshot_and_reset
from app.middlewares.app_context import AppContextMiddleware, set_settings
from app.middlewares.db_session import DbSessionMiddleware
from app.middlewares.dedupe import DedupeMiddleware
from app.middlewares.error_boundary import ErrorBoundaryMiddleware
from app.middlewares.gates import BanGateMiddleware
from app.middlewares.logging import LoggingMiddleware
from app.middlewares.presence import PresenceMiddleware
from app.redis import keys
from app.redis.client import create_redis
from app.services import admin as admin_service
from app.services.dialog import DialogService
from app.services.matchmaking import MatchmakingService
from app.services.payments import PaymentService

logger = logging.getLogger(__name__)

LOCK_KEY = "lock:singleton:bot"
LOCK_TTL_S = 30
SWEEP_INTERVAL_S = 300
RETENTION_INTERVAL_S = 3600

COMMANDS = [
    BotCommand(command="start", description="Профиль и поиск собеседника"),
    BotCommand(command="stop", description="Завершить поиск или диалог"),
    BotCommand(command="premium", description="Подписка Premium"),
    BotCommand(command="invite", description="Позвать друга и получить Premium"),
    BotCommand(command="promo", description="Активировать промокод"),
    BotCommand(command="help", description="Как всё работает"),
    BotCommand(command="privacy", description="Политика обработки данных"),
    BotCommand(command="terms", description="Условия использования"),
    BotCommand(command="delete_me", description="Удалить профиль и данные"),
]


# ------------------------------------------------------------------- singleton
async def acquire_lock(redis: Redis) -> str | None:
    value = secrets.token_hex(16)
    try:
        if await redis.set(LOCK_KEY, value, nx=True, ex=LOCK_TTL_S):
            return value
    except Exception:
        logger.warning("singleton_lock_unavailable", exc_info=True)
        return value  # Redis down: don't block startup, polling will conflict anyway
    return None


async def release_lock(redis: Redis, value: str | None) -> None:
    if not value:
        return
    lua = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('del', KEYS[1])
    end
    return 0
    """
    try:
        await redis.eval(lua, 1, LOCK_KEY, value)
    except Exception:
        logger.debug("release_lock_failed", exc_info=True)


async def refresh_lock(redis: Redis, value: str) -> None:
    lua = """
    if redis.call('get', KEYS[1]) == ARGV[1] then
      return redis.call('expire', KEYS[1], ARGV[2])
    end
    return 0
    """
    while True:
        try:
            await asyncio.sleep(LOCK_TTL_S / 3)
            await redis.eval(lua, 1, LOCK_KEY, value, str(LOCK_TTL_S))
        except asyncio.CancelledError:
            return
        except Exception:
            logger.warning("lock_refresh_failed", exc_info=True)


# -------------------------------------------------------------------- jobs
async def sweeper_loop(settings: Settings, redis: Redis, session_factory) -> None:
    """Close abandoned dialogs and enforce retention — the privacy policy promise."""
    last_retention = 0.0
    while True:
        try:
            await asyncio.sleep(SWEEP_INTERVAL_S)
            async with session_factory() as session:
                service = DialogService(redis, settings)
                closed = await service.sweep(session)
                for _dialog_id, telegram_ids in closed:
                    for tg in telegram_ids:
                        try:
                            await redis.delete(keys.active_dialog(tg), keys.online_key(tg))
                        except Exception:
                            pass

            now = asyncio.get_running_loop().time()
            if now - last_retention >= RETENTION_INTERVAL_S:
                last_retention = now
                async with session_factory() as session:
                    report = await admin_service.run_maintenance(session, settings)
                if report.deleted_messages or report.deleted_photos:
                    logger.info(
                        "retention_applied messages=%s photos=%s complaints=%s",
                        report.deleted_messages, report.deleted_photos, report.deleted_complaints,
                    )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("sweeper_loop_failed")


async def metrics_loop() -> None:
    while True:
        try:
            await asyncio.sleep(60)
            await log_snapshot_and_reset()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("metrics_loop_failed")


# ---------------------------------------------------------------------- main
async def run() -> int:
    settings = Settings()
    setup_logging(settings.log_level)
    set_settings(settings)
    logger.info(
        "bot_start admins=%s moderators=%s support=%s payments=%s vision=%s",
        len(settings.admin_ids), len(settings.moderator_ids), len(settings.support_ids),
        settings.payments_enabled, settings.vision_enabled,
    )

    redis = create_redis(
        settings.redis_host,
        settings.redis_port,
        settings.redis_db,
        settings.redis_username,
        settings.redis_password,
        settings.redis_url,
        socket_timeout=settings.redis_socket_timeout,
        connect_timeout=settings.redis_connect_timeout,
        max_connections=settings.redis_max_connections,
    )
    engine = create_engine(
        settings.database_dsn,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle_s,
    )
    session_factory = create_sessionmaker(engine)

    lock_value = await acquire_lock(redis)
    if lock_value is None:
        logger.error("singleton_lock_busy: another instance is polling. Exiting with code 1 so the "
                     "platform can restart/route correctly.")
        await engine.dispose()
        await _close_redis(redis)
        return 1

    async def check_db() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def check_redis() -> None:
        await redis.ping()

    # A hung Telegram call must not pin a worker forever.
    bot_session = AiohttpSession(timeout=aiohttp.ClientTimeout(total=settings.telegram_timeout_s))
    bot = Bot(token=settings.bot_token, parse_mode=None, session=bot_session)
    routes = _webhook_routes(settings, redis, session_factory, bot)

    health: HealthServer | None = None
    try:
        health = await start_health_server(
            host="0.0.0.0",
            port=int(settings.port),
            check_db=check_db,
            check_redis=check_redis,
            routes=routes,
        )
    except Exception:
        logger.exception("health_server_start_failed port=%s", settings.port)

    storage = RedisStorage(redis=redis)
    dp = Dispatcher(storage=storage)

    dp.update.middleware(LoggingMiddleware())
    dp.update.middleware(AppContextMiddleware(settings=settings, redis=redis))
    dp.update.middleware(DedupeMiddleware(redis=redis))
    dp.update.middleware(PresenceMiddleware(redis, settings, session_factory))
    dp.update.middleware(ErrorBoundaryMiddleware(settings=settings, redis=redis))
    dp.update.middleware(DbSessionMiddleware(session_factory))
    dp.update.middleware(BanGateMiddleware(settings, session_factory))
    dp.include_router(build_router())

    tasks = [
        asyncio.create_task(refresh_lock(redis, lock_value)),
        asyncio.create_task(metrics_loop()),
        asyncio.create_task(sweeper_loop(settings, redis, session_factory)),
    ]

    try:
        await bot.set_my_commands(COMMANDS)
    except Exception:
        logger.warning("set_my_commands_failed", exc_info=True)

    backoff = 1
    try:
        while True:
            try:
                await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
                logger.info("polling_stopped")
                return 0
            except asyncio.CancelledError:
                logger.info("polling_cancelled")
                return 0
            except TelegramUnauthorizedError:
                logger.error("bot_token_rejected: check BOT_TOKEN")
                return 2
            except TelegramConflictError:
                logger.error("telegram_conflict: another instance is polling with the same token")
                backoff = max(backoff, 15)
            except (TelegramNetworkError, ConnectionResetError, TimeoutError):
                logger.warning("telegram_network_error", exc_info=True)
            except Exception:
                logger.exception("polling_crashed")

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
    finally:
        for task in tasks:
            task.cancel()
            with contextlib.suppress(Exception):
                await task
        with contextlib.suppress(Exception):
            await storage.close()
        with contextlib.suppress(Exception):
            await bot.session.close()
        await release_lock(redis, lock_value)
        await stop_health_server(health)
        await _close_redis(redis)
        await engine.dispose()
        logger.info("bot_stopped")


def _webhook_routes(settings: Settings, redis: Redis, session_factory, bot: Bot) -> list:
    """ЮKassa notification endpoint.

    The body is never trusted: we re-read the payment from ЮKassa and compare the
    amount, then credit the subscription exactly once. That makes retries and
    spoofed requests harmless.
    """
    if not (settings.yookassa_shop_id and settings.yookassa_secret_key):
        return []

    async def yookassa_webhook(request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")
        if not isinstance(body, dict):
            return web.Response(status=400, text="bad payload")

        event = str(body.get("event") or "")
        payment_id = str((body.get("object") or {}).get("id") or "")
        if not payment_id:
            return web.Response(status=200, text="ignored")
        if event not in {"payment.succeeded", "payment.canceled", "payment.waiting_for_capture"}:
            return web.Response(status=200, text="ignored")

        try:
            async with session_factory() as session:
                service = PaymentService(settings, redis)
                state, until = await service.apply_yookassa_notification(session, payment_id)
                if state == "paid":
                    tg_id = (
                        await session.execute(
                            select(Payment.telegram_id).where(Payment.external_id == payment_id)
                        )
                    ).scalar_one_or_none()
                    if tg_id:
                        await MatchmakingService(redis, settings).invalidate_premium(int(tg_id))
                    await notify_payment_success(bot, settings, payment_id, until)
        except Exception:
            logger.exception("yookassa_webhook_failed id=%s", payment_id)
            # 200 so ЮKassa does not hammer us; the payment is re-checked on the next poll.
            return web.Response(status=200, text="error logged")
        return web.Response(status=200, text="ok")

    return [("POST", "/yookassa/webhook", yookassa_webhook)]


async def notify_payment_success(bot: Bot, settings: Settings, payment_id: str, until) -> None:
    if not settings.alerts_chat_id or until is None:
        return
    try:
        await bot.send_message(
            int(settings.alerts_chat_id),
            f"💰 <b>Оплата Premium</b>\n<code>{payment_id}</code> · до {until:%d.%m.%Y}",
            parse_mode="HTML",
        )
    except Exception:
        logger.warning("payment_alert_failed", exc_info=True)


async def _close_redis(redis: Redis) -> None:
    try:
        if hasattr(redis, "aclose"):
            await redis.aclose()
        else:  # pragma: no cover - older redis-py
            await redis.close()
    except Exception:
        logger.debug("redis_close_failed", exc_info=True)


def main() -> None:
    warnings.filterwarnings("ignore", message=".*protected namespace.*")
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(0)


if __name__ == "__main__":
    main()

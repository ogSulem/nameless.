from __future__ import annotations

import logging

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


async def rate_limit(redis: Redis, *, key: str, ttl_s: int) -> bool:
    """Simple "once per ttl" guard. Returns True when the action is allowed.

    Availability-first: if Redis is down we allow the action (a broken cache must
    not brick the bot), but we log it once per call site.
    """
    try:
        ok = await redis.set(key, "1", nx=True, ex=int(ttl_s))
        return bool(ok)
    except Exception:
        logger.warning("rate_limit_redis_failed key=%s", key, exc_info=True)
        return True


async def hit(redis: Redis, *, key: str, limit: int, window_s: int) -> tuple[bool, int]:
    """Fixed-window counter. Returns (allowed, remaining).

    Used for chat anti-flood: the previous version had no limit at all on relayed
    messages, so one user could spam the partner (and the bot's API quota) freely.
    """
    if limit <= 0:
        return True, limit
    try:
        pipe = redis.pipeline(transaction=True)
        pipe.incr(key, 1)
        pipe.expire(key, int(window_s))
        current, _ = await pipe.execute()
    except Exception:
        logger.warning("hit_redis_failed key=%s", key, exc_info=True)
        return True, limit

    try:
        current_int = int(current)
    except (TypeError, ValueError):
        return True, limit
    return current_int <= limit, max(0, limit - current_int)

"""Tiny product funnel: how many searches actually turn into conversations.

This is the number that tells an operator whether the product works or whether the
queue is simply empty — you cannot improve a cold start without it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

TTL_S = 60 * 60 * 24 * 3


def _key(event: str, day: str) -> str:
    return f"stat:daily:{event}:{day}"


def _today() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%d")


async def bump(redis: Redis, event: str, amount: int = 1) -> None:
    """Count an event for today. Never breaks the user flow if Redis hiccups."""
    try:
        key = _key(event, _today())
        value = await redis.incrby(key, int(amount))
        if value == amount:
            await redis.expire(key, TTL_S)
    except Exception:
        logger.debug("activity_bump_failed event=%s", event, exc_info=True)


async def today(redis: Redis, event: str) -> int:
    try:
        raw = await redis.get(_key(event, _today()))
        return int(raw or 0)
    except Exception:
        return 0


def conversion(searches: int, matches: int) -> int:
    if searches <= 0:
        return 0
    return min(100, round(matches * 100 / searches))

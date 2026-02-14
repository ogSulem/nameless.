from __future__ import annotations

from redis.asyncio import Redis


async def rate_limit(redis: Redis, *, key: str, ttl_s: int) -> bool:
    """Return True if allowed, False if rate-limited.

    Uses Redis SET NX EX. If Redis is unavailable, returns True (availability-first).
    """
    try:
        ok = await redis.set(key, "1", nx=True, ex=int(ttl_s))
        return bool(ok)
    except Exception:
        return True

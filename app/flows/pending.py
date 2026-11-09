"""Pending-rating state.

The rating flow spans two updates (dialog finished → user taps 0–10), so the
"who am I rating" data lives in Redis with a TTL. It is deliberately cleared as
soon as the profile screen is shown: a bare "5" typed in the profile must never
silently rate somebody from a dialog that ended minutes ago.
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.redis import keys

RATING_TTL_S = 60 * 60

PENDING_KEYS = (
    "pending_rating",
    "pending_rating_partner",
    "pending_rating_step",
    "pending_rating_has_photos",
    "pending_rating_action",
)


async def clear_rating(redis: Redis, tg_id: int) -> None:
    try:
        pipe = redis.pipeline(transaction=False)
        for name in PENDING_KEYS:
            pipe.delete(getattr(keys, name)(int(tg_id)))
        await pipe.execute()
    except Exception:
        pass

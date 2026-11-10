from __future__ import annotations

from redis.asyncio import Redis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry


def create_redis(
    host: str | None = None,
    port: int | None = None,
    db: int = 0,
    username: str | None = None,
    password: str | None = None,
    url: str | None = None,
    *,
    socket_timeout: float = 5,
    connect_timeout: float = 5,
    max_connections: int = 50,
) -> Redis:
    """Redis that fails fast instead of hanging the whole bot.

    With `socket_timeout=None` a half-open connection blocks the handler forever:
    the user sees a frozen bot, the worker holds a DB session and the queue grows.
    """
    common = {
        "decode_responses": True,
        "socket_timeout": float(socket_timeout),
        "socket_connect_timeout": float(connect_timeout),
        "health_check_interval": 30,
        # Two fast retries: most Redis blips are sub-second; long ones must fail.
        "retry": Retry(ExponentialBackoff(cap=1.0, base=0.05), 2),
        "max_connections": max(1, int(max_connections)),
    }
    if url:
        return Redis.from_url(url, **common)
    return Redis(
        host=host,
        port=port,
        db=db,
        username=username,
        password=password,
        **common,
    )

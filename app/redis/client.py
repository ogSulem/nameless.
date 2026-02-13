from __future__ import annotations

from redis.asyncio import Redis


def create_redis(host: str | None = None, port: int | None = None, db: int = 0, password: str | None = None, url: str | None = None) -> Redis:
    if url:
        return Redis.from_url(url, decode_responses=True)
    return Redis(host=host, port=port, db=db, password=password, decode_responses=True)

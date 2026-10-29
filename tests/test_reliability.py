from __future__ import annotations

import asyncio

import pytest
from redis.asyncio import Redis

from app.config import Settings
from app.database import session as db_session
from app.redis import client as redis_client


class _EngineSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, dsn: str, **kwargs):
        self.calls.append((dsn, kwargs))
        return object()


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql+asyncpg://u:p@db:5432/nameless",
    ],
)
def test_postgres_engine_has_bounded_pool_and_timeouts(monkeypatch, dsn):
    spy = _EngineSpy()
    monkeypatch.setattr(db_session, "create_async_engine", spy)

    db_session.create_engine(
        dsn, pool_size=7, max_overflow=3, pool_timeout=4, pool_recycle=600
    )

    _, kwargs = spy.calls[0]
    assert kwargs["pool_size"] == 7
    assert kwargs["max_overflow"] == 3
    assert kwargs["pool_timeout"] == 4
    assert kwargs["pool_recycle"] == 600
    assert kwargs["pool_pre_ping"] is True
    # A hung query must not hold a connection forever.
    assert kwargs["connect_args"]["command_timeout"] == 30


def test_sqlite_engine_ignores_pool_kwargs(monkeypatch):
    spy = _EngineSpy()
    monkeypatch.setattr(db_session, "create_async_engine", spy)

    db_session.create_engine("sqlite+aiosqlite:///:memory:", pool_size=7)

    _, kwargs = spy.calls[0]
    assert "pool_size" not in kwargs
    assert kwargs["pool_pre_ping"] is True


def test_engine_rejects_silly_values(monkeypatch):
    spy = _EngineSpy()
    monkeypatch.setattr(db_session, "create_async_engine", spy)

    db_session.create_engine(
        "postgresql+asyncpg://u:p@db/db", pool_size=0, max_overflow=-5, pool_timeout=0, pool_recycle=1
    )

    _, kwargs = spy.calls[0]
    assert kwargs["pool_size"] == 1
    assert kwargs["max_overflow"] == 0
    assert kwargs["pool_timeout"] == 1
    assert kwargs["pool_recycle"] == 60


def test_redis_has_timeouts_and_connection_cap():
    client = redis_client.create_redis(
        "localhost", 6379, 0, None, None, None, socket_timeout=2, connect_timeout=3, max_connections=11
    )

    assert isinstance(client, Redis)
    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 2.0
    assert kwargs["socket_connect_timeout"] == 3.0
    assert kwargs["health_check_interval"] == 30
    assert client.connection_pool.max_connections == 11


def test_redis_from_url_also_gets_timeouts():
    client = redis_client.create_redis(url="redis://localhost:6379/0", socket_timeout=2)

    kwargs = client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 2.0


def test_reliability_settings_have_sane_defaults():
    s = Settings(BOT_TOKEN="1:x", ADMINS="1")

    assert s.db_pool_size >= 5
    assert s.db_pool_timeout >= 1
    assert s.redis_socket_timeout >= 1
    assert s.redis_connect_timeout >= 1
    assert s.telegram_timeout_s >= 5


def test_redis_timeout_does_not_hang_forever():
    """A dead Redis must raise quickly, not block the handler."""
    client = Redis(host="127.0.0.1", port=1, socket_timeout=0.3, socket_connect_timeout=0.3)

    async def call() -> None:
        await client.get("key")

    import redis.exceptions

    with pytest.raises((redis.exceptions.ConnectionError, OSError, TimeoutError)):
        asyncio.run(asyncio.wait_for(call(), timeout=3))

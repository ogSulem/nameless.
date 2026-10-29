from __future__ import annotations

import pytest

from app.utils.ratelimit import hit, rate_limit


@pytest.mark.asyncio
async def test_hit_allows_up_to_limit(redis):
    allowed = []
    for _ in range(5):
        ok, remaining = await hit(redis, key="rl:test", limit=3, window_s=10)
        allowed.append((ok, remaining))
    assert [a[0] for a in allowed] == [True, True, True, False, False]
    assert allowed[2][1] == 0


@pytest.mark.asyncio
async def test_hit_with_zero_limit_is_unlimited(redis):
    ok, _ = await hit(redis, key="rl:test", limit=0, window_s=10)
    assert ok is True


@pytest.mark.asyncio
async def test_rate_limit_is_once_per_ttl(redis):
    assert await rate_limit(redis, key="rl:once", ttl_s=5) is True
    assert await rate_limit(redis, key="rl:once", ttl_s=5) is False

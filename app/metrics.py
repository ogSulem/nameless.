from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MetricsSnapshot:
    ts: float
    window_s: float
    updates_total: int
    updates_ok: int
    updates_failed: int
    slow_updates: int
    duration_ms_sum: float
    duration_ms_max: float


@dataclass(slots=True)
class _Metrics:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    window_started_at: float = field(default_factory=time.time)
    updates_total: int = 0
    updates_ok: int = 0
    updates_failed: int = 0
    slow_updates: int = 0
    duration_ms_sum: float = 0.0
    duration_ms_max: float = 0.0

    async def record(self, *, ok: bool, duration_ms: float, slow: bool) -> None:
        async with self.lock:
            self.updates_total += 1
            if ok:
                self.updates_ok += 1
            else:
                self.updates_failed += 1
            if slow:
                self.slow_updates += 1
            self.duration_ms_sum += float(duration_ms)
            if duration_ms > self.duration_ms_max:
                self.duration_ms_max = float(duration_ms)

    async def snapshot_and_reset(self) -> MetricsSnapshot:
        async with self.lock:
            now = time.time()
            window_s = max(0.001, now - self.window_started_at)
            snap = MetricsSnapshot(
                ts=now,
                window_s=window_s,
                updates_total=self.updates_total,
                updates_ok=self.updates_ok,
                updates_failed=self.updates_failed,
                slow_updates=self.slow_updates,
                duration_ms_sum=self.duration_ms_sum,
                duration_ms_max=self.duration_ms_max,
            )

            self.window_started_at = now
            self.updates_total = 0
            self.updates_ok = 0
            self.updates_failed = 0
            self.slow_updates = 0
            self.duration_ms_sum = 0.0
            self.duration_ms_max = 0.0
            return snap


_METRICS = _Metrics()


async def record_update(*, ok: bool, duration_ms: float, slow: bool) -> None:
    await _METRICS.record(ok=ok, duration_ms=duration_ms, slow=slow)


async def log_snapshot_and_reset() -> None:
    snap = await _METRICS.snapshot_and_reset()
    rps = snap.updates_total / snap.window_s
    avg_ms = (snap.duration_ms_sum / snap.updates_total) if snap.updates_total else 0.0

    logger.info(
        "slo_snapshot window_s=%.1f rps=%.2f total=%s ok=%s failed=%s slow=%s avg_ms=%.1f max_ms=%.1f",
        snap.window_s,
        rps,
        snap.updates_total,
        snap.updates_ok,
        snap.updates_failed,
        snap.slow_updates,
        avg_ms,
        snap.duration_ms_max,
    )

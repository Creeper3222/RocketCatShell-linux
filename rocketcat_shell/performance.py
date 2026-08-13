from __future__ import annotations

import asyncio
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return float(ordered[index])


class RollingMetric:
    """Fixed-memory latency/size metric safe for event-loop and worker threads."""

    def __init__(self, *, capacity: int = 2048) -> None:
        self._samples: deque[float] = deque(maxlen=max(32, int(capacity)))
        self._count = 0
        self._total = 0.0
        self._maximum = 0.0
        self._lock = threading.Lock()

    def observe(self, value: float | int) -> None:
        sample = max(0.0, float(value))
        with self._lock:
            self._samples.append(sample)
            self._count += 1
            self._total += sample
            self._maximum = max(self._maximum, sample)

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            samples = list(self._samples)
            count = self._count
            total = self._total
            maximum = self._maximum
        return {
            "count": count,
            "sample_count": len(samples),
            "mean": round(total / count, 3) if count else 0.0,
            "p50": round(_percentile(samples, 0.50), 3),
            "p95": round(_percentile(samples, 0.95), 3),
            "p99": round(_percentile(samples, 0.99), 3),
            "max": round(maximum, 3),
        }

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()
            self._count = 0
            self._total = 0.0
            self._maximum = 0.0

    def maximum(self) -> float:
        """Return the all-time maximum without sorting the rolling samples."""
        with self._lock:
            return float(self._maximum)


class RuntimeMetrics:
    """Small process-local registry that never stores payload or identity data."""

    def __init__(self, *, sample_capacity: int = 2048) -> None:
        self._sample_capacity = max(32, int(sample_capacity))
        self._timings: dict[str, RollingMetric] = {}
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._high_water: dict[str, float] = {}
        self._lock = threading.Lock()

    def observe(self, name: str, value: float | int) -> None:
        key = str(name)
        with self._lock:
            metric = self._timings.get(key)
            if metric is None:
                metric = RollingMetric(capacity=self._sample_capacity)
                self._timings[key] = metric
        metric.observe(value)

    def increment(self, name: str, amount: int = 1) -> int:
        key = str(name)
        with self._lock:
            value = self._counters.get(key, 0) + int(amount)
            self._counters[key] = value
            return value

    def set_gauge(self, name: str, value: float | int, *, high_water: bool = False) -> None:
        key = str(name)
        normalized = float(value)
        with self._lock:
            self._gauges[key] = normalized
            if high_water:
                self._high_water[key] = max(self._high_water.get(key, normalized), normalized)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            timings = dict(self._timings)
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            high_water = dict(self._high_water)
        return {
            "timings": {name: metric.snapshot() for name, metric in timings.items()},
            "counters": counters,
            "gauges": {
                name: int(value) if value.is_integer() else round(value, 3)
                for name, value in gauges.items()
            },
            "high_water": {
                name: int(value) if value.is_integer() else round(value, 3)
                for name, value in high_water.items()
            },
        }


@dataclass(slots=True)
class Timer:
    metrics: RuntimeMetrics
    name: str
    started_at: float = 0.0

    def __enter__(self) -> "Timer":
        self.started_at = time.perf_counter()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.metrics.observe(self.name, (time.perf_counter() - self.started_at) * 1000.0)


class EventLoopLagMonitor:
    def __init__(self, *, interval_seconds: float = 0.1, capacity: int = 4096) -> None:
        self.interval_seconds = max(0.02, float(interval_seconds))
        self.metric = RollingMetric(capacity=capacity)
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="RocketCatEventLoopLag")

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        expected = loop.time() + self.interval_seconds
        while self._running:
            await asyncio.sleep(max(0.0, expected - loop.time()))
            now = loop.time()
            self.metric.observe(max(0.0, now - expected) * 1000.0)
            expected = now + self.interval_seconds

    def snapshot(self) -> dict[str, float | int]:
        return self.metric.snapshot()

    def current_max(self) -> float:
        return self.metric.maximum()

    def reset(self) -> None:
        self.metric.reset()

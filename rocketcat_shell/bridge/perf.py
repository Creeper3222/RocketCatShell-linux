from __future__ import annotations

import os
import time
from contextlib import contextmanager, nullcontext
from typing import Any, Iterator

from rocketcat_shell.logger import logger


def perf_enabled(raw_config: Any = None) -> bool:
    env_value = str(os.getenv("ROCKETCAT_PERF_TRACE", "") or "").strip().lower()
    if env_value in {"1", "true", "yes", "on"}:
        return True
    if env_value in {"0", "false", "no", "off"}:
        return False

    if isinstance(raw_config, dict):
        value = raw_config.get("perf_trace_enabled")
    else:
        value = getattr(raw_config, "perf_trace_enabled", None)
    if value is None:
        return False
    return bool(value)


class PerfTrace:
    def __init__(self, label: str, *, tags: dict[str, Any] | None = None):
        self.label = label
        self.tags = dict(tags or {})
        self._started_at = time.perf_counter()
        self._stages: list[tuple[str, float]] = []
        self._closed = False

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started_at = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            self._stages.append((str(name), elapsed_ms))

    def snapshot(self) -> dict[str, Any]:
        total_ms = (time.perf_counter() - self._started_at) * 1000.0
        return {
            "label": self.label,
            "tags": dict(self.tags),
            "total_ms": total_ms,
            "stages": [
                {"name": name, "elapsed_ms": elapsed_ms}
                for name, elapsed_ms in self._stages
            ],
        }

    def finish(self, **extra_tags: Any) -> dict[str, Any]:
        payload = self.snapshot()
        if self._closed:
            return payload

        payload["tags"].update({key: value for key, value in extra_tags.items() if value is not None})
        parts = [
            f"label={payload['label']}",
            f"total_ms={payload['total_ms']:.3f}",
        ]
        for stage in payload["stages"]:
            parts.append(f"{stage['name']}_ms={stage['elapsed_ms']:.3f}")
        for key, value in payload["tags"].items():
            parts.append(f"{key}={value}")
        logger.info("[RocketCatPerf] " + " | ".join(parts))
        self._closed = True
        return payload


def maybe_trace(enabled: bool, label: str, *, tags: dict[str, Any] | None = None) -> PerfTrace | None:
    if not enabled:
        return None
    return PerfTrace(label, tags=tags)


def perf_stage(trace: PerfTrace | None, name: str):
    if trace is None:
        return nullcontext()
    return trace.stage(name)
from __future__ import annotations

import logging
import sys
import threading
import time
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


LOGGER_NAME = "rocketcat"
logger = logging.getLogger(LOGGER_NAME)


class _DispatchHandler(logging.Handler):
    def __init__(self, dispatcher: "_AsyncLogDispatcher") -> None:
        super().__init__(logging.NOTSET)
        self._dispatcher = dispatcher

    def emit(self, record: logging.LogRecord) -> None:
        self._dispatcher.submit(record)


class _AsyncLogDispatcher:
    def __init__(
        self,
        handlers: list[logging.Handler],
        *,
        normal_capacity: int = 4096,
        critical_capacity: int = 256,
    ) -> None:
        self.normal_capacity = max(1, int(normal_capacity))
        self.critical_capacity = max(1, int(critical_capacity))
        self._normal: deque[logging.LogRecord] = deque()
        self._critical: deque[logging.LogRecord] = deque()
        self._handlers = list(handlers)
        self._condition = threading.Condition()
        self._running = True
        self._high_water_normal = 0
        self._high_water_critical = 0
        self._dropped = {"debug": 0, "info": 0, "warning": 0, "error": 0}
        self._last_error = ""
        self.handler = _DispatchHandler(self)
        self._thread = threading.Thread(
            target=self._run,
            name="RocketCatLogListener",
            daemon=True,
        )
        self._thread.start()

    def submit(self, record: logging.LogRecord) -> None:
        with self._condition:
            if not self._running:
                self._count_drop(record)
                return
            if record.levelno >= logging.WARNING:
                self._submit_critical(record)
            else:
                self._submit_normal(record)
            self._condition.notify()

    def _submit_normal(self, record: logging.LogRecord) -> None:
        if len(self._normal) >= self.normal_capacity:
            if record.levelno <= logging.DEBUG:
                self._dropped["debug"] += 1
                return
            debug_index = next(
                (index for index, queued in enumerate(self._normal) if queued.levelno <= logging.DEBUG),
                None,
            )
            if debug_index is None:
                self._dropped["info"] += 1
                return
            del self._normal[debug_index]
            self._dropped["debug"] += 1
        self._normal.append(record)
        self._high_water_normal = max(self._high_water_normal, len(self._normal))

    def _submit_critical(self, record: logging.LogRecord) -> None:
        if len(self._critical) >= self.critical_capacity:
            if record.levelno >= logging.ERROR:
                warning_index = next(
                    (
                        index
                        for index, queued in enumerate(self._critical)
                        if queued.levelno < logging.ERROR
                    ),
                    None,
                )
                if warning_index is not None:
                    del self._critical[warning_index]
                    self._dropped["warning"] += 1
                else:
                    self._critical.popleft()
                    self._dropped["error"] += 1
            else:
                self._dropped["warning"] += 1
                return
        self._critical.append(record)
        self._high_water_critical = max(self._high_water_critical, len(self._critical))

    def _count_drop(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            self._dropped["error"] += 1
        elif record.levelno >= logging.WARNING:
            self._dropped["warning"] += 1
        elif record.levelno <= logging.DEBUG:
            self._dropped["debug"] += 1
        else:
            self._dropped["info"] += 1

    def add_handler(self, handler: logging.Handler) -> None:
        with self._condition:
            if handler not in self._handlers:
                self._handlers.append(handler)

    def remove_handler(self, handler: logging.Handler) -> None:
        with self._condition:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._running and not self._critical and not self._normal:
                    self._condition.wait(timeout=0.5)
                if not self._running and not self._critical and not self._normal:
                    break
                record = self._critical.popleft() if self._critical else self._normal.popleft()
                handlers = tuple(self._handlers)
            for handler in handlers:
                if record.levelno < handler.level:
                    continue
                try:
                    handler.handle(record)
                except Exception as exc:  # pragma: no cover - defensive logging path
                    self._last_error = repr(exc)
                    try:
                        print(f"RocketCat log sink failed: {exc!r}", file=sys.stderr)
                    except Exception:
                        pass
        for handler in tuple(self._handlers):
            try:
                handler.flush()
            except Exception:
                pass

    def stop(self, *, timeout: float = 10.0) -> bool:
        with self._condition:
            self._running = False
            self._condition.notify_all()
        self._thread.join(timeout=max(0.0, float(timeout)))
        drained = not self._thread.is_alive()
        if drained:
            for handler in tuple(self._handlers):
                try:
                    handler.close()
                except Exception:
                    pass
        return drained

    def snapshot(self) -> dict[str, Any]:
        with self._condition:
            return {
                "listener_alive": self._thread.is_alive(),
                "normal_depth": len(self._normal),
                "normal_capacity": self.normal_capacity,
                "normal_high_water": self._high_water_normal,
                "critical_depth": len(self._critical),
                "critical_capacity": self.critical_capacity,
                "critical_high_water": self._high_water_critical,
                "dropped": dict(self._dropped),
                "last_error": self._last_error,
            }


_dispatcher: _AsyncLogDispatcher | None = None
_fallback_sinks: list[logging.Handler] = []
_dispatcher_lock = threading.Lock()


def configure_logging(
    log_file: Path,
    *,
    level_name: str = "INFO",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    global _dispatcher
    level = getattr(logging, str(level_name or "INFO").upper(), logging.INFO)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    shutdown_logging(timeout=5.0)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max(1, int(max_bytes)),
        backupCount=max(0, int(backup_count)),
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    with _dispatcher_lock:
        dispatcher = _AsyncLogDispatcher(
            [file_handler, console_handler, *_fallback_sinks],
            normal_capacity=4096,
            critical_capacity=256,
        )
        _dispatcher = dispatcher
    root_logger.addHandler(dispatcher.handler)
    logger.setLevel(level)


def register_log_sink(handler: logging.Handler) -> None:
    with _dispatcher_lock:
        if handler not in _fallback_sinks:
            _fallback_sinks.append(handler)
        dispatcher = _dispatcher
    if dispatcher is not None:
        dispatcher.add_handler(handler)


def unregister_log_sink(handler: logging.Handler) -> None:
    with _dispatcher_lock:
        if handler in _fallback_sinks:
            _fallback_sinks.remove(handler)
        dispatcher = _dispatcher
    if dispatcher is not None:
        dispatcher.remove_handler(handler)


def logging_diagnostic_snapshot() -> dict[str, Any]:
    with _dispatcher_lock:
        dispatcher = _dispatcher
    if dispatcher is None:
        return {
            "listener_alive": False,
            "normal_depth": 0,
            "normal_capacity": 4096,
            "normal_high_water": 0,
            "critical_depth": 0,
            "critical_capacity": 256,
            "critical_high_water": 0,
            "dropped": {"debug": 0, "info": 0, "warning": 0, "error": 0},
            "last_error": "",
        }
    return dispatcher.snapshot()


def shutdown_logging(*, timeout: float = 10.0) -> bool:
    global _dispatcher
    with _dispatcher_lock:
        dispatcher = _dispatcher
        _dispatcher = None
    if dispatcher is None:
        return True
    root_logger = logging.getLogger()
    if dispatcher.handler in root_logger.handlers:
        root_logger.removeHandler(dispatcher.handler)
    started_at = time.monotonic()
    drained = dispatcher.stop(timeout=timeout)
    _ = time.monotonic() - started_at
    return drained

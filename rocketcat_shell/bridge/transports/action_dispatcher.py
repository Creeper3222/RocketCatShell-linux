from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from rocketcat_shell.logger import logger
from rocketcat_shell.performance import RuntimeMetrics

from .base import ActionHandler
from .codec import OneBotMessageCodec


Responder = Callable[[dict[str, Any]], Awaitable[None]]
ACTION_WORKER_COUNT = 8
ACTION_QUEUE_CAPACITY = 256
ACTION_TIMEOUT_SECONDS = 60.0
ACTION_WORKER_IDLE_SECONDS = 60.0


@dataclass(slots=True)
class ActionRequest:
    action: str
    params: dict[str, Any]
    echo: Any
    responder: Responder
    enqueued_at: float


class OneBotActionDispatcher:
    def __init__(
        self,
        action_handler: ActionHandler,
        *,
        codec: OneBotMessageCodec,
        owner: str,
    ):
        self._action_handler = action_handler
        self._codec = codec
        self._owner = owner
        self.queue: asyncio.Queue[ActionRequest] = asyncio.Queue(
            maxsize=ACTION_QUEUE_CAPACITY
        )
        self.workers: set[asyncio.Task[Any]] = set()
        self.active_count = 0
        self.metrics = RuntimeMetrics()
        self._target_locks: dict[str, asyncio.Lock] = {}
        self._target_lock_users: dict[str, int] = {}
        self._running = False
        self._worker_sequence = 0

    def start(self) -> None:
        self._running = True

    async def stop(self, *, drain_timeout: float = 10.0) -> None:
        self._running = False
        try:
            async with asyncio.timeout(max(0.1, float(drain_timeout))):
                await self.queue.join()
        except asyncio.TimeoutError:
            logger.warning(
                "[RocketCatShell] OneBot action drain timed out | owner=%s | remaining=%s",
                self._owner,
                self.queue.qsize(),
            )
        workers = list(self.workers)
        self.workers.clear()
        for task in workers:
            if not task.done():
                task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self.discard_pending()
        self._target_locks.clear()
        self._target_lock_users.clear()

    async def submit(
        self,
        action: str,
        params: dict[str, Any] | None,
        echo: Any,
        responder: Responder,
    ) -> bool:
        request = ActionRequest(
            action=str(action or ""),
            params=self._codec.normalize_action_params(dict(params or {})),
            echo=echo,
            responder=responder,
            enqueued_at=time.perf_counter(),
        )
        try:
            self.queue.put_nowait(request)
        except asyncio.QueueFull:
            self.metrics.increment("busy_rejected")
            await responder(
                self.response_payload(
                    {
                        "status": "failed",
                        "retcode": 1503,
                        "data": None,
                        "wording": "RocketCatShell OneBot action queue is busy; retry later",
                    },
                    echo,
                )
            )
            return False
        self.metrics.increment("accepted")
        self.metrics.set_gauge("queue_depth", self.queue.qsize(), high_water=True)
        self._ensure_workers()
        return True

    async def execute(
        self,
        action: str,
        params: dict[str, Any] | None,
        echo: Any,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()

        async def resolve(payload: dict[str, Any]) -> None:
            if not future.done():
                future.set_result(payload)

        await self.submit(action, params, echo, resolve)
        return await future

    def discard_pending(self) -> int:
        count = 0
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.queue.task_done()
            count += 1
        return count

    async def _worker_loop(self) -> None:
        while True:
            try:
                request = await asyncio.wait_for(
                    self.queue.get(), timeout=ACTION_WORKER_IDLE_SECONDS
                )
            except asyncio.TimeoutError:
                return
            started_at = time.perf_counter()
            self.metrics.observe(
                "queue_wait_ms", (started_at - request.enqueued_at) * 1000.0
            )
            self.active_count += 1
            try:
                await self._handle_request(request)
                self.metrics.increment("processed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.metrics.increment("failed")
                logger.warning(
                    "[RocketCatShell] OneBot action worker request failed | owner=%s | action=%s | error=%r",
                    self._owner,
                    request.action,
                    exc,
                )
            finally:
                self.active_count = max(0, self.active_count - 1)
                self.metrics.observe(
                    "execution_ms", (time.perf_counter() - started_at) * 1000.0
                )
                self.queue.task_done()

    async def _handle_request(self, request: ActionRequest) -> None:
        target_key = self._target_key(request.action, request.params)
        lock = self._target_locks.setdefault(target_key, asyncio.Lock())
        self._target_lock_users[target_key] = self._target_lock_users.get(target_key, 0) + 1
        try:
            async with lock:
                try:
                    async with asyncio.timeout(ACTION_TIMEOUT_SECONDS):
                        response = await self._action_handler(
                            request.action, request.params
                        )
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    self.metrics.increment("timed_out")
                    response = {
                        "status": "failed",
                        "retcode": 1504,
                        "data": None,
                        "wording": "RocketCatShell OneBot action timed out after 60 seconds",
                    }
                except Exception as exc:
                    logger.exception(
                        "[RocketCatShell] OneBot action failed | owner=%s | action=%s",
                        self._owner,
                        request.action,
                    )
                    response = {
                        "status": "failed",
                        "retcode": 1500,
                        "data": None,
                        "wording": repr(exc),
                    }
                await request.responder(self.response_payload(response, request.echo))
        finally:
            remaining = self._target_lock_users.get(target_key, 1) - 1
            if remaining <= 0:
                self._target_lock_users.pop(target_key, None)
                self._target_locks.pop(target_key, None)
            else:
                self._target_lock_users[target_key] = remaining

    def diagnostic_snapshot(self) -> dict[str, Any]:
        snapshot = self.metrics.snapshot()
        timings = snapshot["timings"]
        counters = snapshot["counters"]
        return {
            "depth": self.queue.qsize(),
            "capacity": self.queue.maxsize,
            "high_water": snapshot["high_water"].get("queue_depth", 0),
            "active": self.active_count,
            "workers": len(self.workers),
            "worker_limit": ACTION_WORKER_COUNT,
            "wait_p95_ms": timings.get("queue_wait_ms", {}).get("p95", 0.0),
            "wait_p99_ms": timings.get("queue_wait_ms", {}).get("p99", 0.0),
            "execution_p95_ms": timings.get("execution_ms", {}).get("p95", 0.0),
            "execution_p99_ms": timings.get("execution_ms", {}).get("p99", 0.0),
            "accepted": counters.get("accepted", 0),
            "processed": counters.get("processed", 0),
            "busy_rejected": counters.get("busy_rejected", 0),
            "timed_out": counters.get("timed_out", 0),
        }

    def _on_worker_done(self, task: asyncio.Task[Any]) -> None:
        self.workers.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(
                "[RocketCatShell] OneBot action worker exited unexpectedly | owner=%s | error=%r",
                self._owner,
                error,
            )
        if self._running and not self.queue.empty():
            self._ensure_workers()

    def _ensure_workers(self) -> None:
        if not self._running:
            return
        desired = min(
            ACTION_WORKER_COUNT,
            max(1, self.queue.qsize() + self.active_count),
        )
        while len(self.workers) < desired:
            self._worker_sequence += 1
            task = asyncio.create_task(
                self._worker_loop(),
                name=(
                    f"RocketCatOneBotActionWorker:{self._owner}:"
                    f"{self._worker_sequence}"
                ),
            )
            self.workers.add(task)
            task.add_done_callback(self._on_worker_done)

    def response_payload(self, response: dict[str, Any], echo: Any) -> dict[str, Any]:
        return {
            "status": response.get("status", "ok"),
            "retcode": response.get("retcode", 0),
            "data": self._codec.encode_response_data(response.get("data")),
            "wording": response.get("wording", ""),
            "echo": echo,
        }

    @staticmethod
    def _target_key(action: str, params: dict[str, Any]) -> str:
        for field in ("group_id", "user_id", "message_id", "id"):
            value = params.get(field)
            if value is not None:
                return f"{field}:{value}"
        return f"action:{action}"

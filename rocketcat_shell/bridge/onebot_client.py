from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

from rocketcat_shell.logger import logger
from rocketcat_shell.performance import RuntimeMetrics

from .config import BridgeConfig
from .json_codec import json_dumps, json_loads


_ONEBOT_WS_MIN_MSG_SIZE = 8 * 1024 * 1024
_ONEBOT_WS_MEDIA_ENVELOPE_EXTRA = 8 * 1024 * 1024


ActionHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
_ONEBOT_UPSTREAM_RETRY_DELAY_SECONDS = 5.0
_ONEBOT_ACTION_WORKER_COUNT = 8
_ONEBOT_ACTION_QUEUE_CAPACITY = 256
_ONEBOT_ACTION_TIMEOUT_SECONDS = 60.0


@dataclass(slots=True)
class _ActionRequest:
    websocket: aiohttp.ClientWebSocketResponse
    action: str
    params: dict[str, Any]
    echo: Any
    enqueued_at: float


class OneBotReverseWsClient:
    def __init__(
        self,
        config: BridgeConfig,
        action_handler: ActionHandler,
    ):
        self.config = config
        self._action_handler = action_handler
        self._http_session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._sender_task: asyncio.Task | None = None
        self._outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=max(
                1,
                int(
                    getattr(
                        config,
                        "onebot_outgoing_queue_max_entries",
                        512,
                    )
                    or 512
                ),
            )
        )
        self._pending_payload: dict[str, Any] | None = None
        self._send_lock = asyncio.Lock()
        self._action_locks: dict[str, asyncio.Lock] = {}
        self._action_lock_users: dict[str, int] = {}
        self._action_queue: asyncio.Queue[_ActionRequest] = asyncio.Queue(
            maxsize=_ONEBOT_ACTION_QUEUE_CAPACITY
        )
        self._action_workers: set[asyncio.Task[Any]] = set()
        self._active_action_count = 0
        self._action_metrics = RuntimeMetrics()
        self._consecutive_reconnect_failures = 0
        self._waiting_for_upstream = False
        self._dropped_event_count = 0

    def _max_ws_msg_size(self) -> int:
        media_limit = max(0, int(getattr(self.config, "remote_media_max_size", 0) or 0))
        encoded_limit = ((media_limit + 2) // 3) * 4 if media_limit else 0
        return max(_ONEBOT_WS_MIN_MSG_SIZE, encoded_limit + _ONEBOT_WS_MEDIA_ENVELOPE_EXTRA)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        connector = aiohttp.TCPConnector(
            limit=16,
            limit_per_host=8,
            ttl_dns_cache=300,
            keepalive_timeout=30,
        )
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=45.0),
            connector=connector,
            json_serialize=json_dumps,
        )
        self._start_action_workers()
        self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._sender_task is not None:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass
            self._sender_task = None
        try:
            async with asyncio.timeout(10.0):
                await self._action_queue.join()
        except asyncio.TimeoutError:
            logger.warning(
                "[RocketCatShell] OneBot action drain timed out | remaining=%s",
                self._action_queue.qsize(),
            )
        await self._stop_action_workers()
        self._discard_action_queue()
        self._action_locks.clear()
        self._action_lock_users.clear()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._http_session is not None:
            await self._http_session.close()
        self._http_session = None
        self._discard_queued_events()
        self._consecutive_reconnect_failures = 0
        self._waiting_for_upstream = False

    async def emit_event(self, payload: dict[str, Any]) -> None:
        if not self._running or not self.connected:
            self._dropped_event_count += 1
            return
        try:
            self._outgoing.put_nowait(payload)
        except asyncio.QueueFull:
            self._dropped_event_count += 1

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def _run_forever(self) -> None:
        if self._http_session is None:
            raise RuntimeError("OneBot HTTP session 尚未初始化")
        while self._running:
            try:
                headers = {
                    "X-Self-ID": str(self.config.onebot_self_id),
                    "X-Client-Role": "Universal",
                }
                if self.config.onebot_access_token:
                    headers["Authorization"] = f"Bearer {self.config.onebot_access_token}"
                async with self._http_session.ws_connect(
                    self.config.onebot_ws_url,
                    headers=headers,
                    heartbeat=30.0,
                    autoping=True,
                    max_msg_size=self._max_ws_msg_size(),
                ) as ws:
                    self._ws = ws
                    recovered = self._waiting_for_upstream
                    await self._send_lifecycle_connect(ws)
                    self._consecutive_reconnect_failures = 0
                    self._waiting_for_upstream = False
                    if recovered:
                        logger.info(
                            "[RocketChatOneBotBridge] OneBot 上游连接已恢复，后续新事件将继续实时发送。"
                        )
                    else:
                        logger.info("[RocketChatOneBotBridge] 已连接 AstrBot OneBot reverse WebSocket。")
                    self._sender_task = asyncio.create_task(self._sender_loop())
                    await self._listen_loop(ws)
                    if self._running:
                        close_code = getattr(ws, "close_code", None)
                        if close_code == 1009:
                            self._mark_upstream_unavailable(
                                "收到超出传输上限的消息，可能是媒体上传超过 bot 远程媒体大小上限: "
                                f"bot_id={self.config.bot_id or '-'} "
                                f"bot_name={self.config.display_name or '-'} "
                                f"remote_media_max_size={self.config.remote_media_max_size} "
                                f"max_msg_size={self._max_ws_msg_size()}",
                                error=True,
                            )
                        else:
                            self._mark_upstream_unavailable(
                                f"连接已断开 (close_code={close_code})"
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                self._consecutive_reconnect_failures += 1
                self._mark_upstream_unavailable(
                    f"第 {self._consecutive_reconnect_failures} 次连接失败: {exc!r}"
                )
            finally:
                self._ws = None
                if self._sender_task is not None:
                    self._sender_task.cancel()
                    await asyncio.gather(self._sender_task, return_exceptions=True)
                    self._sender_task = None
                self._discard_queued_events()
            if self._running:
                await asyncio.sleep(_ONEBOT_UPSTREAM_RETRY_DELAY_SECONDS)

    def _mark_upstream_unavailable(self, reason: str, *, error: bool = False) -> None:
        if self._waiting_for_upstream:
            logger.debug(
                "[RocketChatOneBotBridge] OneBot 上游仍未连接，将继续后台等待: %s",
                reason,
            )
            return
        self._waiting_for_upstream = True
        log = logger.error if error else logger.info
        log(
            "[RocketChatOneBotBridge] OneBot 上游暂未连接；Rocket.Chat Bot 保持启用，"
            "将以 %.1fs 独立间隔在后台持续等待。期间新事件不会积压或延后重放。原因: %s",
            _ONEBOT_UPSTREAM_RETRY_DELAY_SECONDS,
            reason,
        )

    def _discard_queued_events(self) -> int:
        discarded = int(self._pending_payload is not None)
        self._pending_payload = None
        while True:
            try:
                self._outgoing.get_nowait()
            except asyncio.QueueEmpty:
                break
            discarded += 1
        self._dropped_event_count += discarded
        return discarded

    async def _sender_loop(self) -> None:
        while self._running and self._ws is not None and not self._ws.closed:
            if self._pending_payload is None:
                self._pending_payload = await self._outgoing.get()
            if self._ws is None or self._ws.closed:
                break
            async with self._send_lock:
                await self._ws.send_str(json_dumps(self._pending_payload))
            self._pending_payload = None

    async def _send_lifecycle_connect(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        payload = {
            "time": int(time.time()),
            "self_id": self.config.onebot_self_id,
            "post_type": "meta_event",
            "meta_event_type": "lifecycle",
            "sub_type": "connect",
        }
        async with self._send_lock:
            await ws.send_str(json_dumps(payload))
        logger.info("[RocketChatOneBotBridge] 已上报 OneBot lifecycle.connect 元事件。")

    async def _listen_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for raw in ws:
            if raw.type != aiohttp.WSMsgType.TEXT:
                if raw.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                }:
                    break
                continue
            data = json_loads(raw.data)
            action = data.get("action")
            if not action:
                continue
            params = data.get("params") or {}
            if not isinstance(params, dict):
                params = {}
            echo = data.get("echo")
            request = _ActionRequest(
                websocket=ws,
                action=str(action),
                params=params,
                echo=echo,
                enqueued_at=time.perf_counter(),
            )
            try:
                self._action_queue.put_nowait(request)
            except asyncio.QueueFull:
                self._action_metrics.increment("busy_rejected")
                await self._send_action_response(
                    ws,
                    {
                        "status": "failed",
                        "retcode": 1503,
                        "data": None,
                        "wording": "RocketCatShell OneBot action queue is busy; retry later",
                    },
                    echo,
                )
                continue
            self._action_metrics.increment("accepted")
            self._action_metrics.set_gauge(
                "queue_depth",
                self._action_queue.qsize(),
                high_water=True,
            )

    def _start_action_workers(self) -> None:
        if self._action_workers:
            return
        for index in range(_ONEBOT_ACTION_WORKER_COUNT):
            task = asyncio.create_task(
                self._action_worker_loop(),
                name=f"RocketCatOneBotActionWorker:{index + 1}",
            )
            self._action_workers.add(task)
            task.add_done_callback(self._on_action_worker_done)

    async def _stop_action_workers(self) -> None:
        workers = list(self._action_workers)
        self._action_workers.clear()
        for task in workers:
            if not task.done():
                task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    def _on_action_worker_done(self, task: asyncio.Task[Any]) -> None:
        self._action_workers.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            logger.error(
                "[RocketCatShell] OneBot action worker exited unexpectedly | owner=%s | error=%r",
                self.config.bot_id or self.config.display_name or "unknown",
                error,
            )

    async def _action_worker_loop(self) -> None:
        while True:
            request = await self._action_queue.get()
            started_at = time.perf_counter()
            self._action_metrics.observe(
                "queue_wait_ms",
                (started_at - request.enqueued_at) * 1000.0,
            )
            self._active_action_count += 1
            try:
                await self._handle_action_frame(
                    request.websocket,
                    request.action,
                    request.params,
                    request.echo,
                )
                self._action_metrics.increment("processed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._action_metrics.increment("failed")
                logger.warning(
                    "[RocketCatShell] OneBot action worker request failed | action=%s | error=%r",
                    request.action,
                    exc,
                )
            finally:
                self._active_action_count = max(0, self._active_action_count - 1)
                self._action_metrics.observe(
                    "execution_ms",
                    (time.perf_counter() - started_at) * 1000.0,
                )
                self._action_queue.task_done()

    def _discard_action_queue(self) -> int:
        discarded = 0
        while True:
            try:
                self._action_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._action_queue.task_done()
            discarded += 1
        return discarded

    async def _handle_action_frame(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        action: str,
        params: dict[str, Any],
        echo: Any,
    ) -> None:
        target_key = self._action_target_key(action, params)
        lock = self._action_locks.setdefault(target_key, asyncio.Lock())
        self._action_lock_users[target_key] = self._action_lock_users.get(target_key, 0) + 1
        try:
            async with lock:
                try:
                    async with asyncio.timeout(_ONEBOT_ACTION_TIMEOUT_SECONDS):
                        response = await self._action_handler(action, params)
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    self._action_metrics.increment("timed_out")
                    response = {
                        "status": "failed",
                        "retcode": 1504,
                        "data": None,
                        "wording": "RocketCatShell OneBot action timed out after 60 seconds",
                    }
                except Exception as exc:
                    logger.exception(
                        "[RocketChatOneBotBridge] OneBot action 处理失败: action=%s",
                        action,
                    )
                    response = {
                        "status": "failed",
                        "retcode": 1500,
                        "data": None,
                        "wording": repr(exc),
                    }
                await self._send_action_response(ws, response, echo)
        finally:
            remaining = self._action_lock_users.get(target_key, 1) - 1
            if remaining <= 0:
                self._action_lock_users.pop(target_key, None)
                self._action_locks.pop(target_key, None)
            else:
                self._action_lock_users[target_key] = remaining

    async def _send_action_response(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        response: dict[str, Any],
        echo: Any,
    ) -> None:
        response_payload = {
            "status": response.get("status", "ok"),
            "retcode": response.get("retcode", 0),
            "data": response.get("data"),
            "wording": response.get("wording", ""),
            "echo": echo,
        }
        if not ws.closed:
            async with self._send_lock:
                await ws.send_str(json_dumps(response_payload))

    @staticmethod
    def _action_target_key(action: str, params: dict[str, Any]) -> str:
        for field in ("group_id", "user_id", "message_id", "id"):
            value = params.get(field)
            if value is not None:
                return f"{field}:{value}"
        return f"action:{action}"

    def build_diagnostic_snapshot(self) -> dict[str, Any]:
        action_metrics = self._action_metrics.snapshot()
        action_timings = action_metrics["timings"]
        action_counters = action_metrics["counters"]
        return {
            "onebot_connected": self.connected,
            "onebot_waiting_for_upstream": self._waiting_for_upstream,
            "onebot_reconnect_failures": self._consecutive_reconnect_failures,
            "onebot_retry_delay_seconds": _ONEBOT_UPSTREAM_RETRY_DELAY_SECONDS,
            "onebot_dropped_event_count": self._dropped_event_count,
            "outgoing_queue_depth": self._outgoing.qsize()
            + int(self._pending_payload is not None),
            "outgoing_queue_max_entries": self._outgoing.maxsize,
            "active_action_count": self._active_action_count,
            "performance": {
                "onebot_actions": {
                    "depth": self._action_queue.qsize(),
                    "capacity": self._action_queue.maxsize,
                    "high_water": action_metrics["high_water"].get("queue_depth", 0),
                    "active": self._active_action_count,
                    "wait_p95_ms": action_timings.get("queue_wait_ms", {}).get("p95", 0.0),
                    "wait_p99_ms": action_timings.get("queue_wait_ms", {}).get("p99", 0.0),
                    "execution_p95_ms": action_timings.get("execution_ms", {}).get("p95", 0.0),
                    "execution_p99_ms": action_timings.get("execution_ms", {}).get("p99", 0.0),
                    "accepted": action_counters.get("accepted", 0),
                    "processed": action_counters.get("processed", 0),
                    "busy_rejected": action_counters.get("busy_rejected", 0),
                    "timed_out": action_counters.get("timed_out", 0),
                }
            },
        }

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

import aiohttp

from rocketcat_shell.logger import logger

from .config import BridgeConfig
from .json_codec import json_dumps, json_loads


_ONEBOT_WS_MIN_MSG_SIZE = 8 * 1024 * 1024
_ONEBOT_WS_MEDIA_ENVELOPE_EXTRA = 8 * 1024 * 1024


ActionHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
FailureCallback = Callable[[str, int, str], Awaitable[None]]


class OneBotReverseWsClient:
    def __init__(
        self,
        config: BridgeConfig,
        action_handler: ActionHandler,
        on_reconnect_exhausted: FailureCallback | None = None,
    ):
        self.config = config
        self._action_handler = action_handler
        self._on_reconnect_exhausted = on_reconnect_exhausted
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
        self._action_semaphore = asyncio.Semaphore(8)
        self._action_locks: dict[str, asyncio.Lock] = {}
        self._action_lock_users: dict[str, int] = {}
        self._action_tasks: set[asyncio.Task[Any]] = set()
        self._consecutive_reconnect_failures = 0

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
        for task in tuple(self._action_tasks):
            task.cancel()
        if self._action_tasks:
            await asyncio.gather(*self._action_tasks, return_exceptions=True)
        self._action_tasks.clear()
        self._action_locks.clear()
        self._action_lock_users.clear()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._http_session is not None:
            await self._http_session.close()
        self._http_session = None
        self._consecutive_reconnect_failures = 0

    async def emit_event(self, payload: dict[str, Any]) -> None:
        if not self._running:
            return
        await self._outgoing.put(payload)

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
                    self._consecutive_reconnect_failures = 0
                    logger.info("[RocketChatOneBotBridge] 已连接 AstrBot OneBot reverse WebSocket。")
                    await self._send_lifecycle_connect(ws)
                    self._sender_task = asyncio.create_task(self._sender_loop())
                    await self._listen_loop(ws)
                    if self._running:
                        close_code = getattr(ws, "close_code", None)
                        if close_code == 1009:
                            logger.error(
                                "[RocketChatOneBotBridge] OneBot reverse WS 收到超出传输上限的消息，"
                                "可能是媒体上传超过 bot 远程媒体大小上限: "
                                "bot_id=%s bot_name=%s remote_media_max_size=%s max_msg_size=%s，%.1fs 后重连。",
                                self.config.bot_id or "-",
                                self.config.display_name or "-",
                                self.config.remote_media_max_size,
                                self._max_ws_msg_size(),
                                self.config.reconnect_delay,
                            )
                        else:
                            logger.warning(
                                f"[RocketChatOneBotBridge] OneBot reverse WS 已断开 (close_code={close_code})，{self.config.reconnect_delay:.1f}s 后重连。"
                            )
                        await asyncio.sleep(self.config.reconnect_delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                self._consecutive_reconnect_failures += 1
                if self._should_stop_reconnect():
                    await self._handle_reconnect_exhausted(exc)
                    break
                logger.warning(
                    f"[RocketChatOneBotBridge] OneBot reverse WS 重连失败第 {self._consecutive_reconnect_failures} 次: {exc!r}，{self.config.reconnect_delay:.1f}s 后继续重连。"
                )
                await asyncio.sleep(self.config.reconnect_delay)
            finally:
                self._ws = None
                if self._sender_task is not None:
                    self._sender_task.cancel()
                    try:
                        await self._sender_task
                    except asyncio.CancelledError:
                        pass
                    self._sender_task = None

    def _should_stop_reconnect(self) -> bool:
        max_attempts = self.config.max_reconnect_attempts
        return max_attempts > 0 and self._consecutive_reconnect_failures >= max_attempts

    async def _handle_reconnect_exhausted(self, exc: Exception) -> None:
        self._running = False
        logger.error("[RocketChatOneBotBridge] 连接失败，已自动关闭rocketchat桥接器，请检查网络或目标服务器状态")
        if self._on_reconnect_exhausted is not None:
            await self._on_reconnect_exhausted(
                "OneBot reverse WebSocket",
                self._consecutive_reconnect_failures,
                repr(exc),
            )

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
            echo = data.get("echo")
            await self._action_semaphore.acquire()
            task = asyncio.create_task(
                self._handle_action_frame(
                    ws,
                    str(action),
                    params,
                    echo,
                ),
                name=f"RocketCatOneBotAction:{action}",
            )
            self._action_tasks.add(task)
            task.add_done_callback(self._on_action_task_done)

    def _on_action_task_done(self, task: asyncio.Task[Any]) -> None:
        self._action_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.exception()
        except Exception:
            pass

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
                    response = await self._action_handler(action, params)
                except asyncio.CancelledError:
                    raise
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
        finally:
            remaining = self._action_lock_users.get(target_key, 1) - 1
            if remaining <= 0:
                self._action_lock_users.pop(target_key, None)
                self._action_locks.pop(target_key, None)
            else:
                self._action_lock_users[target_key] = remaining
            self._action_semaphore.release()

    @staticmethod
    def _action_target_key(action: str, params: dict[str, Any]) -> str:
        for field in ("group_id", "user_id", "message_id", "id"):
            value = params.get(field)
            if value is not None:
                return f"{field}:{value}"
        return f"action:{action}"

    def build_diagnostic_snapshot(self) -> dict[str, int]:
        return {
            "outgoing_queue_depth": self._outgoing.qsize()
            + int(self._pending_payload is not None),
            "outgoing_queue_max_entries": self._outgoing.maxsize,
            "active_action_count": len(self._action_tasks),
        }

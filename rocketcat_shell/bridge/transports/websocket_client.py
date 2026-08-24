from __future__ import annotations

import asyncio
from typing import Any, Mapping

import aiohttp

from rocketcat_shell.logger import logger

from ..json_codec import json_dumps, json_loads
from .action_dispatcher import OneBotActionDispatcher
from .base import OneBotTransport, heartbeat_event, lifecycle_event
from .codec import SerializedOneBotFrame
from .spec import TransportCardField, TransportFieldSpec, TransportSpec


_MIN_MESSAGE_SIZE = 8 * 1024 * 1024
_MEDIA_ENVELOPE_EXTRA = 8 * 1024 * 1024
_WEBSOCKET_SEND_TIMEOUT_SECONDS = 10.0


def _legacy_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    return default


def reconcile_legacy_settings(
    settings: Mapping[str, Any],
    legacy: Mapping[str, Any],
    shell_defaults: Any,
    has_explicit_transport: bool,
) -> Mapping[str, Any]:
    """Overlay fields editable by v0.2.2 without losing v0.2.3-only settings."""

    merged = dict(settings)
    default_skip = bool(getattr(shell_defaults, "default_skip_own_messages", True))
    default_debug = bool(getattr(shell_defaults, "default_debug", False))

    if "onebot_ws_url" in legacy:
        merged["url"] = str(legacy.get("onebot_ws_url") or "").strip()
    if "onebot_access_token" in legacy:
        merged["access_token"] = str(legacy.get("onebot_access_token") or "")
    if "skip_own_messages" in legacy:
        merged["report_self_message"] = not _legacy_bool(
            legacy.get("skip_own_messages"),
            default_skip,
        )
    if "debug" in legacy:
        merged["debug"] = _legacy_bool(legacy.get("debug"), default_debug)

    if not has_explicit_transport:
        if "message_post_format" in legacy:
            merged["message_post_format"] = str(
                legacy.get("message_post_format") or "array"
            ).strip().lower()
        if "onebot_reconnect_delay" in legacy:
            try:
                merged["reconnect_interval_ms"] = max(
                    100,
                    int(float(legacy.get("onebot_reconnect_delay")) * 1000),
                )
            except (TypeError, ValueError):
                merged["reconnect_interval_ms"] = 5000
        if "onebot_heartbeat_interval_ms" in legacy:
            try:
                merged["heartbeat_interval_ms"] = int(
                    legacy.get("onebot_heartbeat_interval_ms") or 0
                )
            except (TypeError, ValueError):
                merged["heartbeat_interval_ms"] = 30000
    return merged


class WebsocketClientTransport(OneBotTransport):
    def __init__(self, config: Any, action_handler: Any):
        super().__init__(config, action_handler)
        self._http_session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[Any] | None = None
        self._sender_task: asyncio.Task[Any] | None = None
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._outgoing: asyncio.Queue[SerializedOneBotFrame] = asyncio.Queue(
            maxsize=max(
                1,
                int(
                    getattr(config, "onebot_outgoing_queue_max_entries", 512)
                    or 512
                ),
            )
        )
        self._pending_payload: SerializedOneBotFrame | None = None
        self._send_lock = asyncio.Lock()
        self._dispatcher = OneBotActionDispatcher(
            action_handler,
            codec=self.codec,
            owner=str(config.bot_id or config.display_name or "websocket-client"),
        )
        self._consecutive_reconnect_failures = 0
        self._waiting_for_upstream = False
        self._dropped_event_count = 0

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def _action_queue(self):
        return self._dispatcher.queue

    @property
    def _action_workers(self):
        return self._dispatcher.workers

    @property
    def _active_action_count(self) -> int:
        return self._dispatcher.active_count

    @property
    def _action_metrics(self):
        return self._dispatcher.metrics

    def _start_action_workers(self) -> None:
        self._dispatcher.start()

    async def _stop_action_workers(self) -> None:
        await self._dispatcher.stop()

    def _max_ws_msg_size(self) -> int:
        media_limit = max(
            0, int(getattr(self.config, "remote_media_max_size", 0) or 0)
        )
        encoded_limit = ((media_limit + 2) // 3) * 4 if media_limit else 0
        return max(_MIN_MESSAGE_SIZE, encoded_limit + _MEDIA_ENVELOPE_EXTRA)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._dispatcher.start()
        connector = aiohttp.TCPConnector(
            limit=16,
            limit_per_host=8,
            ttl_dns_cache=300,
            keepalive_timeout=30,
        )
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=45.0,
                connect=2.0,
                sock_connect=2.0,
            ),
            connector=connector,
            json_serialize=json_dumps,
        )
        self._task = asyncio.create_task(
            self._run_forever(),
            name=f"RocketCatOneBotWebsocketClient:{self.config.bot_id or '-'}",
        )

    async def stop(self) -> None:
        self._running = False
        for task_name in ("_task", "_sender_task", "_heartbeat_task"):
            task = getattr(self, task_name)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                setattr(self, task_name, None)
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        await self._dispatcher.stop()
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
        frame = self.codec.serialize_event(payload)
        try:
            self._outgoing.put_nowait(frame)
        except asyncio.QueueFull:
            self._dropped_event_count += 1

    async def _run_forever(self) -> None:
        if self._http_session is None:
            raise RuntimeError("OneBot HTTP session 尚未初始化")
        retry_seconds = max(
            0.1, float(self.settings.get("reconnect_interval_ms", 5000)) / 1000.0
        )
        heartbeat_ms = max(0, int(self.settings.get("heartbeat_interval_ms", 30000)))
        while self._running:
            try:
                headers = {
                    "Authorization": (
                        f"Bearer {str(self.settings.get('access_token') or '')}"
                    ),
                    "X-Self-ID": str(self.config.onebot_self_id),
                    "X-Client-Role": "Universal",
                    "User-Agent": "OneBot/11",
                }
                async with self._http_session.ws_connect(
                    str(self.settings.get("url") or self.config.onebot_ws_url),
                    headers=headers,
                    heartbeat=(heartbeat_ms / 1000.0) if heartbeat_ms > 0 else None,
                    autoping=True,
                    max_msg_size=self._max_ws_msg_size(),
                ) as websocket:
                    self._ws = websocket
                    recovered = self._waiting_for_upstream
                    await self._send_payload(lifecycle_event(self.config.onebot_self_id))
                    self._consecutive_reconnect_failures = 0
                    self._waiting_for_upstream = False
                    self._last_error = ""
                    if recovered:
                        logger.info(
                            "[RocketCatShell] OneBot Websocket客户端连接已恢复 | bot_id=%s",
                            self.config.bot_id or "-",
                        )
                    else:
                        logger.info(
                            "[RocketCatShell] OneBot Websocket客户端已连接 | bot_id=%s",
                            self.config.bot_id or "-",
                        )
                    self._sender_task = asyncio.create_task(self._sender_loop())
                    if heartbeat_ms > 0:
                        self._heartbeat_task = asyncio.create_task(
                            self._heartbeat_loop(heartbeat_ms)
                        )
                    await self._listen_loop(websocket)
                    if self._running:
                        self._mark_upstream_unavailable(
                            f"连接已断开 (close_code={websocket.close_code})",
                            retry_seconds=retry_seconds,
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                self._consecutive_reconnect_failures += 1
                self._last_error = repr(exc)
                self._mark_upstream_unavailable(
                    f"第 {self._consecutive_reconnect_failures} 次连接失败: {exc!r}",
                    retry_seconds=retry_seconds,
                )
            finally:
                self._ws = None
                for task_name in ("_sender_task", "_heartbeat_task"):
                    task = getattr(self, task_name)
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        setattr(self, task_name, None)
                self._discard_queued_events()
            if self._running:
                await asyncio.sleep(retry_seconds)

    def _mark_upstream_unavailable(self, reason: str, *, retry_seconds: float) -> None:
        if self._waiting_for_upstream:
            logger.debug(
                "[RocketCatShell] OneBot Websocket客户端仍未连接 | bot_id=%s | reason=%s",
                self.config.bot_id or "-",
                reason,
            )
            return
        self._waiting_for_upstream = True
        logger.info(
            "[RocketCatShell] OneBot Websocket客户端暂未连接；Rocket.Chat Bot 保持启用，"
            "将以 %.1fs 独立间隔后台等待 | bot_id=%s | reason=%s",
            retry_seconds,
            self.config.bot_id or "-",
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
            else:
                self._outgoing.task_done()
                discarded += 1
        self._dropped_event_count += discarded
        return discarded

    async def _sender_loop(self) -> None:
        while self._running and self.connected:
            if self._pending_payload is None:
                self._pending_payload = await self._outgoing.get()
            try:
                await self._send_payload(self._pending_payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._dropped_event_count += 1
                websocket = self._ws
                if websocket is not None and not websocket.closed:
                    await websocket.close(code=1011)
                raise
            finally:
                self._pending_payload = None
                self._outgoing.task_done()

    async def _heartbeat_loop(self, interval_ms: int) -> None:
        while self._running and self.connected:
            await asyncio.sleep(interval_ms / 1000.0)
            if self.connected:
                await self._send_payload(
                    heartbeat_event(self.config.onebot_self_id, interval_ms)
                )

    async def _send_payload(
        self, payload: dict[str, Any] | SerializedOneBotFrame
    ) -> None:
        websocket = self._ws
        if websocket is None or websocket.closed:
            raise ConnectionError("OneBot WebSocket is not connected")
        await self._send_payload_for_session(websocket, payload)

    async def _send_payload_for_session(
        self,
        websocket: aiohttp.ClientWebSocketResponse,
        payload: dict[str, Any] | SerializedOneBotFrame,
    ) -> None:
        frame = (
            payload
            if isinstance(payload, SerializedOneBotFrame)
            else self.codec.serialize_payload(payload)
        )
        async with self._send_lock:
            if self._ws is not websocket or websocket.closed:
                raise ConnectionError("OneBot WebSocket session has changed")
            async with asyncio.timeout(_WEBSOCKET_SEND_TIMEOUT_SECONDS):
                await websocket.send_str(frame.text)

    async def _listen_loop(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        async for raw in websocket:
            if raw.type != aiohttp.WSMsgType.TEXT:
                if raw.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                }:
                    break
                continue
            try:
                data = json_loads(raw.data)
            except Exception:
                await self._send_payload(
                    {
                        "status": "failed",
                        "retcode": 1400,
                        "data": None,
                        "wording": "json解析失败,请检查数据格式",
                        "echo": None,
                    }
                )
                continue
            if not isinstance(data, dict) or not data.get("action"):
                continue

            async def respond(payload: dict[str, Any]) -> None:
                if self._ws is not websocket or websocket.closed:
                    return
                try:
                    await self._send_payload_for_session(websocket, payload)
                except asyncio.CancelledError:
                    raise
                except ConnectionError:
                    return
                except Exception as exc:
                    self._last_error = repr(exc)
                    if self._ws is websocket and not websocket.closed:
                        await websocket.close(code=1011)

            await self._dispatcher.submit(
                str(data.get("action") or ""),
                data.get("params") if isinstance(data.get("params"), dict) else {},
                data.get("echo"),
                respond,
            )

    def build_diagnostic_snapshot(self) -> dict[str, Any]:
        retry_seconds = max(
            0.1, float(self.settings.get("reconnect_interval_ms", 5000)) / 1000.0
        )
        payload = self._base_diagnostics(
            status=("已连接" if self.connected else "等待上游"),
            endpoint=str(self.settings.get("url") or ""),
        )
        payload.update(
            {
                "onebot_waiting_for_upstream": self._waiting_for_upstream,
                "onebot_reconnect_failures": self._consecutive_reconnect_failures,
                "onebot_retry_delay_seconds": retry_seconds,
                "onebot_dropped_event_count": self._dropped_event_count,
                "outgoing_queue_depth": self._outgoing.qsize()
                + int(self._pending_payload is not None),
                "outgoing_queue_max_entries": self._outgoing.maxsize,
                "active_action_count": self._dispatcher.active_count,
                "onebot_listener_active": False,
                "onebot_client_count": int(self.connected),
                "onebot_event_subscriber_count": int(self.connected),
                "performance": {
                    "onebot_actions": self._dispatcher.diagnostic_snapshot()
                },
            }
        )
        return payload


SPEC = TransportSpec(
    type_id="websocket-client",
    label="Websocket客户端",
    description="RocketCat 主动连接 OneBot WebSocket 上游，并接收 action、推送事件。",
    fields=(
        TransportFieldSpec(
            "url",
            "Websocket 地址",
            "url",
            "ws://127.0.0.1:6199/ws/",
            required=True,
            span=2,
            schemes=("ws", "wss"),
            shell_default="default_onebot_ws_url",
        ),
        TransportFieldSpec(
            "message_post_format",
            "消息上报格式",
            "select",
            "array",
            options=(("array", "Array"), ("string", "String / CQ码")),
        ),
        TransportFieldSpec(
            "report_self_message",
            "上报自身消息",
            "boolean",
            False,
            shell_default="default_skip_own_messages",
            invert_shell_default=True,
        ),
        TransportFieldSpec(
            "reconnect_interval_ms",
            "重连间隔（毫秒）",
            "number",
            5000,
            minimum=100,
            maximum=3_600_000,
            step=100,
        ),
        TransportFieldSpec(
            "heartbeat_interval_ms",
            "心跳间隔（毫秒）",
            "number",
            30000,
            minimum=0,
            maximum=3_600_000,
            step=1000,
            help_text="设为 0 可关闭 OneBot 心跳元事件。",
        ),
        TransportFieldSpec(
            "access_token",
            "Access Token",
            "password",
            "",
            span=2,
            shell_default="default_onebot_access_token",
        ),
        TransportFieldSpec(
            "debug",
            "调试日志",
            "boolean",
            False,
            shell_default="default_debug",
        ),
    ),
    card_fields=(
        TransportCardField("url", "WS URL", "code"),
        TransportCardField("message_post_format", "格式", "choice"),
        TransportCardField("reconnect_interval_ms", "重连", "milliseconds"),
        TransportCardField("heartbeat_interval_ms", "心跳", "milliseconds"),
    ),
    factory=WebsocketClientTransport,
    aliases=("ws-client", "websocket client", "websocket客户端"),
    legacy_settings_reconciler=reconcile_legacy_settings,
)

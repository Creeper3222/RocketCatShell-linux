from __future__ import annotations

from typing import Any

from aiohttp import web

from rocketcat_shell.logger import logger

from .action_dispatcher import OneBotActionDispatcher
from .base import OneBotTransport
from .http_common import endpoint_label
from .server_runner import AiohttpServerRunner
from .spec import TransportCardField, TransportFieldSpec, TransportSpec
from .websocket_peer import WebsocketPeerHub


class WebsocketServerTransport(OneBotTransport):
    def __init__(self, config: Any, action_handler: Any):
        super().__init__(config, action_handler)
        self._dispatcher = OneBotActionDispatcher(
            action_handler,
            codec=self.codec,
            owner=str(config.bot_id or config.display_name or "websocket-server"),
        )
        self._runner: AiohttpServerRunner | None = None
        self._hub: WebsocketPeerHub | None = None

    @property
    def connected(self) -> bool:
        return bool(self._runner and self._runner.listening)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._dispatcher.start()
        host = str(self.settings.get("host") or "127.0.0.1")
        port = int(self.settings.get("port") or 3001)
        self._hub = WebsocketPeerHub(
            owner=str(self.config.bot_id or "websocket-server"),
            token=str(self.settings.get("access_token") or ""),
            self_id=self.config.onebot_self_id,
            heartbeat_interval_ms=int(
                self.settings.get("heartbeat_interval_ms") or 0
            ),
            queue_capacity=int(
                getattr(self.config, "onebot_outgoing_queue_max_entries", 512)
            ),
            codec=self.codec,
            dispatcher=self._dispatcher,
            max_msg_size=self._max_message_size(),
        )
        app = web.Application(client_max_size=self._max_message_size())
        app.router.add_route("*", "/{tail:.*}", self._handle_request)
        self._runner = AiohttpServerRunner(app, host=host, port=port)
        try:
            await self._runner.start()
            self._last_error = ""
            logger.info(
                "[RocketCatShell] OneBot Websocket服务器开始监听 | bot_id=%s | endpoint=%s",
                self.config.bot_id or "-",
                endpoint_label(host, port, "ws"),
            )
        except Exception as exc:
            self._last_error = repr(exc)
            logger.error(
                "[RocketCatShell] OneBot Websocket服务器监听失败；Bot 保持启用 | bot_id=%s | endpoint=%s | error=%r",
                self.config.bot_id or "-",
                endpoint_label(host, port, "ws"),
                exc,
            )

    async def stop(self) -> None:
        self._running = False
        if self._hub is not None:
            await self._hub.stop()
            self._hub = None
        if self._runner is not None:
            await self._runner.stop()
            self._runner = None
        await self._dispatcher.stop()

    async def emit_event(self, payload: dict[str, Any]) -> None:
        if self._hub is not None:
            await self._hub.emit_event(payload)

    async def _handle_request(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() != "websocket":
            return web.json_response(
                {
                    "status": "failed",
                    "retcode": 1400,
                    "wording": "该端点仅接受 WebSocket 连接",
                },
                status=426,
            )
        if self._hub is None:
            raise web.HTTPServiceUnavailable()
        return await self._hub.handle(request)

    def _max_message_size(self) -> int:
        media_limit = max(
            0, int(getattr(self.config, "remote_media_max_size", 0) or 0)
        )
        return max(8 * 1024 * 1024, media_limit * 2 + 8 * 1024 * 1024)

    def build_diagnostic_snapshot(self) -> dict[str, Any]:
        host = str(self.settings.get("host") or "127.0.0.1")
        port = int(self.settings.get("port") or 3001)
        clients = self._hub.client_count if self._hub else 0
        subscribers = self._hub.event_client_count if self._hub else 0
        if self._last_error:
            status = "端口错误"
        elif clients:
            status = "已有客户端"
        elif self.connected:
            status = "监听中"
        else:
            status = "未监听"
        payload = self._base_diagnostics(
            status=status,
            endpoint=endpoint_label(host, port, "ws"),
        )
        if not self._last_error and self._hub is not None:
            payload["onebot_last_error"] = self._hub.last_error
        payload.update(
            {
                "onebot_listener_active": self.connected,
                "onebot_client_count": clients,
                "onebot_event_subscriber_count": subscribers,
                "onebot_waiting_for_upstream": False,
                "onebot_reconnect_failures": 0,
                "onebot_retry_delay_seconds": 0,
                "onebot_dropped_event_count": self._hub.dropped_event_count if self._hub else 0,
                "outgoing_queue_depth": self._hub.queue_depth if self._hub else 0,
                "outgoing_queue_max_entries": int(
                    getattr(self.config, "onebot_outgoing_queue_max_entries", 512)
                ),
                "active_action_count": self._dispatcher.active_count,
                "performance": {"onebot_actions": self._dispatcher.diagnostic_snapshot()},
            }
        )
        return payload


SPEC = TransportSpec(
    type_id="websocket-server",
    label="Websocket服务器",
    description="RocketCat 监听 OneBot WebSocket；/api 只处理 action，其余路径同时推送事件。",
    fields=(
        TransportFieldSpec("host", "监听 Host", "text", "127.0.0.1", required=True),
        TransportFieldSpec("port", "监听 Port", "number", 3001, minimum=1, maximum=65535),
        TransportFieldSpec(
            "message_post_format", "消息上报格式", "select", "array",
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
        # NapCat currently persists and exposes this exact field name but does
        # not consume it at runtime. Keep the wire key for lossless parity.
        TransportFieldSpec("enableForcePushEvent", "强制推送事件", "boolean", True),
        TransportFieldSpec(
            "heartbeat_interval_ms", "心跳间隔（毫秒）", "number", 30000,
            minimum=0, maximum=3_600_000, step=1000,
        ),
        TransportFieldSpec(
            "access_token",
            "Access Token",
            "password",
            "",
            span=2,
            random_token_on_create=True,
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
        TransportCardField("host", "Host", "code"),
        TransportCardField("port", "Port", "number"),
        TransportCardField("heartbeat_interval_ms", "心跳", "milliseconds"),
        TransportCardField("message_post_format", "格式", "choice"),
    ),
    factory=WebsocketServerTransport,
    server=True,
    aliases=("ws-server", "websocket server", "websocket服务器"),
)

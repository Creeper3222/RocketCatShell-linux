from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from rocketcat_shell.logger import logger

from .action_dispatcher import OneBotActionDispatcher
from .base import OneBotTransport
from .codec import SerializedOneBotFrame
from .http_common import (
    apply_cors_headers,
    endpoint_label,
    handle_http_action,
    is_authorized,
    token_failure,
)
from .server_runner import AiohttpServerRunner
from .spec import TransportCardField, TransportFieldSpec, TransportSpec
from .websocket_peer import WebsocketPeerHub


_SSE_KEEPALIVE_SECONDS = 15.0
@dataclass(eq=False, slots=True)
class SseClient:
    queue: asyncio.Queue[SerializedOneBotFrame]
    close_event: asyncio.Event


class HttpSseServerTransport(OneBotTransport):
    def __init__(self, config: Any, action_handler: Any):
        super().__init__(config, action_handler)
        self._dispatcher = OneBotActionDispatcher(
            action_handler,
            codec=self.codec,
            owner=str(config.bot_id or config.display_name or "http-sse-server"),
        )
        self._runner: AiohttpServerRunner | None = None
        self._hub: WebsocketPeerHub | None = None
        self._sse_clients: set[SseClient] = set()
        self._dropped_event_count = 0

    @property
    def connected(self) -> bool:
        return bool(self._runner and self._runner.listening)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._dispatcher.start()
        host = str(self.settings.get("host") or "127.0.0.1")
        port = int(self.settings.get("port") or 3000)
        app = web.Application(client_max_size=self._max_request_size())
        if bool(self.settings.get("enable_websocket")):
            self._hub = WebsocketPeerHub(
                owner=str(self.config.bot_id or "http-sse-server"),
                token=str(self.settings.get("access_token") or ""),
                self_id=self.config.onebot_self_id,
                heartbeat_interval_ms=30000,
                queue_capacity=int(
                    getattr(self.config, "onebot_outgoing_queue_max_entries", 512)
                ),
                codec=self.codec,
                dispatcher=self._dispatcher,
                max_msg_size=self._max_request_size(),
            )
        app.router.add_route("*", "/{tail:.*}", self._handle_request)
        self._runner = AiohttpServerRunner(app, host=host, port=port)
        try:
            await self._runner.start()
            self._last_error = ""
            logger.info(
                "[RocketCatShell] OneBot HTTP SSE服务器开始监听 | bot_id=%s | endpoint=%s/_events",
                self.config.bot_id or "-",
                endpoint_label(host, port),
            )
        except Exception as exc:
            self._last_error = repr(exc)
            logger.error(
                "[RocketCatShell] OneBot HTTP SSE服务器监听失败；Bot 保持启用 | bot_id=%s | endpoint=%s | error=%r",
                self.config.bot_id or "-",
                endpoint_label(host, port),
                exc,
            )

    async def stop(self) -> None:
        self._running = False
        for client in tuple(self._sse_clients):
            client.close_event.set()
        if self._hub is not None:
            await self._hub.stop()
            self._hub = None
        if self._runner is not None:
            await self._runner.stop()
            self._runner = None
        self._sse_clients.clear()
        await self._dispatcher.stop()

    async def emit_event(self, payload: dict[str, Any]) -> None:
        frame = self.codec.serialize_event(payload)
        for client in tuple(self._sse_clients):
            try:
                client.queue.put_nowait(frame)
            except asyncio.QueueFull:
                self._dropped_event_count += 1
                client.close_event.set()
        if self._hub is not None:
            self._hub.emit_frame(frame)

    async def _handle_request(self, request: web.Request) -> web.StreamResponse:
        cors_enabled = bool(self.settings.get("enable_cors", True))
        if request.method.upper() == "OPTIONS":
            response = web.Response(status=204)
            if cors_enabled:
                apply_cors_headers(response)
            return response
        if not is_authorized(request, str(self.settings.get("access_token") or "")):
            response = token_failure()
            if cors_enabled:
                apply_cors_headers(response)
            return response
        if request.headers.get("Upgrade", "").lower() == "websocket":
            if self._hub is None:
                response = web.json_response(
                    {"status": "failed", "retcode": 1404, "wording": "WebSocket 未启用"},
                    status=400,
                )
                if cors_enabled:
                    apply_cors_headers(response)
                return response
            return await self._hub.handle(request)
        if request.path.rstrip("/") == "/_events":
            return await self._handle_sse(request, cors_enabled=cors_enabled)
        response = await handle_http_action(request, self._dispatcher)
        if cors_enabled:
            apply_cors_headers(response)
        return response

    async def _handle_sse(
        self,
        request: web.Request,
        *,
        cors_enabled: bool,
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        if cors_enabled:
            apply_cors_headers(response)
        await response.prepare(request)
        client = SseClient(
            queue=asyncio.Queue(
                maxsize=max(
                    1,
                    int(
                        getattr(
                            self.config, "onebot_outgoing_queue_max_entries", 512
                        )
                    ),
                )
            ),
            close_event=asyncio.Event(),
        )
        self._sse_clients.add(client)
        try:
            await response.write(b": RocketCatShell OneBot SSE connected\n\n")
            while self._running:
                queue_task = asyncio.create_task(client.queue.get())
                close_task = asyncio.create_task(client.close_event.wait())
                done, pending = await asyncio.wait(
                    {queue_task, close_task},
                    timeout=_SSE_KEEPALIVE_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if close_task in done and close_task.result():
                    if queue_task in done:
                        client.queue.task_done()
                    break
                if queue_task not in done:
                    request_transport = request.transport
                    if request_transport is None or request_transport.is_closing():
                        break
                    await response.write(b": keepalive\n\n")
                    continue
                frame = queue_task.result()
                try:
                    await response.write(frame.sse)
                finally:
                    client.queue.task_done()
        except asyncio.CancelledError:
            raise
        except (ConnectionResetError, RuntimeError):
            pass
        finally:
            self._sse_clients.discard(client)
        return response

    def _max_request_size(self) -> int:
        media_limit = max(
            0, int(getattr(self.config, "remote_media_max_size", 0) or 0)
        )
        return max(8 * 1024 * 1024, media_limit * 2 + 8 * 1024 * 1024)

    def build_diagnostic_snapshot(self) -> dict[str, Any]:
        host = str(self.settings.get("host") or "127.0.0.1")
        port = int(self.settings.get("port") or 3000)
        websocket_clients = self._hub.client_count if self._hub else 0
        subscribers = len(self._sse_clients) + (
            self._hub.event_client_count if self._hub else 0
        )
        if self._last_error:
            status = "端口错误"
        elif subscribers:
            status = "已有客户端"
        elif self.connected:
            status = "无事件订阅者"
        else:
            status = "未监听"
        payload = self._base_diagnostics(
            status=status,
            endpoint=f"{endpoint_label(host, port)}/_events",
        )
        if not self._last_error and self._hub is not None:
            payload["onebot_last_error"] = self._hub.last_error
        payload.update(
            {
                "onebot_listener_active": self.connected,
                "onebot_client_count": len(self._sse_clients) + websocket_clients,
                "onebot_event_subscriber_count": subscribers,
                "onebot_waiting_for_upstream": False,
                "onebot_reconnect_failures": 0,
                "onebot_retry_delay_seconds": 0,
                "onebot_dropped_event_count": self._dropped_event_count
                + (self._hub.dropped_event_count if self._hub else 0),
                "outgoing_queue_depth": sum(
                    client.queue.qsize() for client in self._sse_clients
                ) + (self._hub.queue_depth if self._hub else 0),
                "outgoing_queue_max_entries": int(
                    getattr(self.config, "onebot_outgoing_queue_max_entries", 512)
                ),
                "active_action_count": self._dispatcher.active_count,
                "performance": {"onebot_actions": self._dispatcher.diagnostic_snapshot()},
            }
        )
        return payload


SPEC = TransportSpec(
    type_id="http-sse-server",
    label="HTTP SSE服务器",
    description="RocketCat 提供 OneBot HTTP action API，并通过 /_events 的 SSE 流推送事件。",
    fields=(
        TransportFieldSpec("host", "监听 Host", "text", "127.0.0.1", required=True),
        TransportFieldSpec("port", "监听 Port", "number", 3000, minimum=1, maximum=65535),
        TransportFieldSpec("enable_cors", "启用 CORS", "boolean", True),
        TransportFieldSpec("enable_websocket", "启用同端口 WebSocket", "boolean", False),
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
        TransportCardField("enable_websocket", "WebSocket", "boolean"),
        TransportCardField("report_self_message", "自身消息", "boolean"),
    ),
    factory=HttpSseServerTransport,
    server=True,
    aliases=("http sse server", "http sse服务器"),
)

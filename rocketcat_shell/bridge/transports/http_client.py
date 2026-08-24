from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from typing import Any

import aiohttp

from rocketcat_shell.logger import logger

from ..json_codec import json_loads
from .action_dispatcher import OneBotActionDispatcher
from .base import OneBotTransport
from .codec import SerializedOneBotFrame
from .spec import TransportCardField, TransportFieldSpec, TransportSpec


class HttpClientTransport(OneBotTransport):
    def __init__(self, config: Any, action_handler: Any):
        super().__init__(config, action_handler)
        self._dispatcher = OneBotActionDispatcher(
            action_handler,
            codec=self.codec,
            owner=str(config.bot_id or config.display_name or "http-client"),
        )
        self._session: aiohttp.ClientSession | None = None
        self._sender_task: asyncio.Task[Any] | None = None
        self._outgoing: asyncio.Queue[SerializedOneBotFrame] = asyncio.Queue(
            maxsize=max(
                1,
                int(
                    getattr(config, "onebot_outgoing_queue_max_entries", 512)
                    or 512
                ),
            )
        )
        self._dropped_event_count = 0
        self._delivery_success_count = 0
        self._delivery_failure_count = 0
        self._unsupported_quick_action_count = 0
        self._last_delivery_status = "尚未投递"
        self._last_delivery_at = 0.0
        self._last_failure_warning_at = 0.0
        self._suppressed_failure_warnings = 0

    @property
    def connected(self) -> bool:
        return self._running and self._session is not None and not self._session.closed

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._dispatcher.start()
        connector = aiohttp.TCPConnector(
            limit=8,
            limit_per_host=4,
            ttl_dns_cache=300,
            keepalive_timeout=30,
        )
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30.0),
            connector=connector,
        )
        self._sender_task = asyncio.create_task(
            self._sender_loop(),
            name=f"RocketCatOneBotHttpClient:{self.config.bot_id or '-'}",
        )

    async def stop(self) -> None:
        self._running = False
        try:
            async with asyncio.timeout(10.0):
                await self._outgoing.join()
        except asyncio.TimeoutError:
            logger.warning(
                "[RocketCatShell] OneBot HTTP客户端事件排空超时 | bot_id=%s | remaining=%s",
                self.config.bot_id or "-",
                self._outgoing.qsize(),
            )
        if self._sender_task is not None:
            self._sender_task.cancel()
            await asyncio.gather(self._sender_task, return_exceptions=True)
            self._sender_task = None
        while True:
            try:
                self._outgoing.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._outgoing.task_done()
        await self._dispatcher.stop()
        if self._session is not None:
            await self._session.close()
        self._session = None

    async def emit_event(self, payload: dict[str, Any]) -> None:
        if not self._running:
            self._dropped_event_count += 1
            return
        frame = self.codec.serialize_event(payload)
        try:
            self._outgoing.put_nowait(frame)
        except asyncio.QueueFull:
            self._dropped_event_count += 1

    async def _sender_loop(self) -> None:
        while True:
            event = await self._outgoing.get()
            try:
                await self._deliver(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._delivery_failure_count += 1
                self._last_delivery_status = f"投递失败：{exc}"
                self._last_delivery_at = time.time()
                self._last_error = repr(exc)
                self._log_delivery_failure(exc)
            finally:
                self._outgoing.task_done()

    def _log_delivery_failure(self, error: Exception) -> None:
        now = time.monotonic()
        if now - self._last_failure_warning_at < 30.0:
            self._suppressed_failure_warnings += 1
            return
        logger.warning(
            "[RocketCatShell] OneBot HTTP客户端投递失败；Bot 保持启用 | bot_id=%s | url=%s | suppressed=%s | error=%r",
            self.config.bot_id or "-",
            self.settings.get("url") or "-",
            self._suppressed_failure_warnings,
            error,
        )
        self._last_failure_warning_at = now
        self._suppressed_failure_warnings = 0

    async def _deliver(self, event: SerializedOneBotFrame) -> None:
        if self._session is None:
            raise RuntimeError("HTTP client session is not initialized")
        body = event.utf8
        headers = {
            "Content-Type": "application/json",
            "X-Self-ID": str(self.config.onebot_self_id),
            "User-Agent": "OneBot/11",
        }
        token = str(self.settings.get("access_token") or "")
        if token:
            signature = hmac.new(
                token.encode("utf-8"), body, hashlib.sha1
            ).hexdigest()
            headers["X-Signature"] = f"sha1={signature}"
        async with self._session.post(
            str(self.settings.get("url") or ""), data=body, headers=headers
        ) as response:
            response.raise_for_status()
            response_text = await response.text()
        self._delivery_success_count += 1
        self._last_delivery_status = f"投递成功（HTTP {response.status}）"
        self._last_delivery_at = time.time()
        self._last_error = ""
        if response_text.strip():
            parsed = json_loads(response_text)
            if isinstance(parsed, dict):
                await self._handle_quick_action(event, parsed)

    async def _handle_quick_action(
        self,
        event: SerializedOneBotFrame,
        operation: dict[str, Any],
    ) -> None:
        reply = operation.get("reply")
        if reply is not None and reply != "" and reply != []:
            params: dict[str, Any] = {
                "message": reply,
                "message_type": event.message_type,
            }
            if event.group_id is not None:
                params["group_id"] = event.group_id
            elif event.user_id is not None:
                params["user_id"] = event.user_id

            async def discard_response(_payload: dict[str, Any]) -> None:
                return None

            await self._dispatcher.submit(
                "send_msg", params, None, discard_response
            )

        unsupported = [
            key
            for key in ("delete", "kick", "ban", "approve")
            if operation.get(key)
        ]
        action = str(operation.get("action") or "").strip()
        if action and action != "send_msg":
            unsupported.append(action)
        if unsupported:
            self._unsupported_quick_action_count += len(unsupported)
            logger.warning(
                "[RocketCatShell] OneBot HTTP客户端快速操作未实现（1404） | bot_id=%s | actions=%s",
                self.config.bot_id or "-",
                ",".join(unsupported),
            )

    def build_diagnostic_snapshot(self) -> dict[str, Any]:
        payload = self._base_diagnostics(
            status=self._last_delivery_status,
            endpoint=str(self.settings.get("url") or ""),
        )
        payload.update(
            {
                "onebot_listener_active": False,
                "onebot_client_count": int(self.connected),
                "onebot_event_subscriber_count": int(self.connected),
                "onebot_waiting_for_upstream": False,
                "onebot_reconnect_failures": 0,
                "onebot_retry_delay_seconds": 0,
                "onebot_dropped_event_count": self._dropped_event_count,
                "onebot_last_delivery_status": self._last_delivery_status,
                "onebot_last_delivery_at": self._last_delivery_at,
                "onebot_delivery_success_count": self._delivery_success_count,
                "onebot_delivery_failure_count": self._delivery_failure_count,
                "onebot_unsupported_quick_action_count": self._unsupported_quick_action_count,
                "outgoing_queue_depth": self._outgoing.qsize(),
                "outgoing_queue_max_entries": self._outgoing.maxsize,
                "active_action_count": self._dispatcher.active_count,
                "performance": {"onebot_actions": self._dispatcher.diagnostic_snapshot()},
            }
        )
        return payload


SPEC = TransportSpec(
    type_id="http-client",
    label="HTTP客户端",
    description="RocketCat 将 OneBot 事件 POST 到目标 URL，并执行响应中的受支持快速回复。",
    fields=(
        TransportFieldSpec(
            "url", "上报 URL", "url", "http://localhost:8080",
            required=True, span=2, schemes=("http", "https"),
        ),
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
            "Access Token / HMAC 密钥",
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
        TransportCardField("url", "HTTP URL", "code"),
        TransportCardField("message_post_format", "格式", "choice"),
        TransportCardField("report_self_message", "自身消息", "boolean"),
    ),
    factory=HttpClientTransport,
    aliases=("http client", "http客户端"),
)

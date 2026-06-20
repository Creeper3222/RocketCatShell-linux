from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlparse

import aiohttp

from rocketcat_shell.logger import logger

from .config import BridgeConfig
from .json_codec import json_dumps, json_dumps_compact, json_loads
from .media import RocketChatMediaBridge
from .media_publication import MediaPublicationService
from .rocketchat_compat import (
    RocketChatCapabilities,
    RocketChatHTTPError,
    RocketChatMethodError,
    UnsupportedRocketChatVersionError,
)
from .rocketchat_e2ee import RocketChatE2EEManager


MessageCallback = Callable[[dict[str, Any]], Awaitable[None]]
FailureCallback = Callable[[str, int, str], Awaitable[None]]


class RocketChatClient:
    _INBOUND_SIGNATURE_IGNORED_KEYS = {"_updatedAt", "reactions"}
    _INBOUND_WORKER_COUNT = 2
    _INBOUND_QUEUE_MAX_SIZE = 512
    _SERVER_BRANDING_CACHE_TTL_SECONDS = 300.0
    _WEBSOCKET_HEARTBEAT_SECONDS = 30.0
    _WEBSOCKET_READ_TIMEOUT_SECONDS = 90.0
    _DDP_CONNECT_TIMEOUT_SECONDS = 20.0
    _DDP_LOGIN_TIMEOUT_SECONDS = 20.0
    _INBOUND_SIGNATURE_FIELD_NAMES = (
        "_id",
        "rid",
        "msg",
        "text",
        "tmid",
        "t",
        "groupable",
        "editedAt",
        "e2e",
    )
    _HTML_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
    _HTML_APP_NAME_RE = re.compile(
        r"<meta[^>]+name=[\"']application-name[\"'][^>]+content=[\"'](.*?)[\"']",
        re.IGNORECASE | re.DOTALL,
    )

    def __init__(
        self,
        config: BridgeConfig,
        media_publication_service: MediaPublicationService | None = None,
        media_cache_dir: str | os.PathLike[str] | None = None,
        on_message: MessageCallback | None = None,
        on_reconnect_exhausted: FailureCallback | None = None,
    ):
        self.config = config
        self._on_message = on_message
        self._on_reconnect_exhausted = on_reconnect_exhausted
        self._http_session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._pending_ddp_results: dict[str, asyncio.Future] = {}
        self._ddp_call_id = 0
        self._method_call_id = 0
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._inbound_queues: list[asyncio.Queue[dict[str, Any]]] = []
        self._inbound_workers: set[asyncio.Task[Any]] = set()
        self._subscribed_rooms: set[str] = set()
        self._room_info_cache: dict[str, dict[str, Any]] = {}
        self._room_info_cache_expires_at: dict[str, float] = {}
        self._room_type_cache: dict[str, str] = {}
        self._room_name_cache: dict[str, str] = {}
        self._user_cache: dict[str, dict[str, Any]] = {}
        self._server_branding_cache: dict[str, str] | None = None
        self._server_branding_cache_expires_at = 0.0
        self._seen_inbound_message_signatures: dict[str, str] = {}
        self._recent_self_messages: dict[str, list[dict[str, Any]]] = {}
        self._pending_self_message_waiters: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
        self.auth_token: str | None = None
        self.user_id: str | None = None
        self.bot_username: str | None = None
        self.bot_profile: dict[str, Any] = {}
        self.server_info: dict[str, Any] = {}
        self.cloud_workspace_id = ""
        self.capabilities = RocketChatCapabilities.unknown()
        self._method_transport = "rest"
        self._method_rest_fallbacks = 0
        self.e2ee = RocketChatE2EEManager(
            client=self,
            enabled=bool(self.config.e2ee_password.strip()),
            password=self.config.e2ee_password.strip(),
        )
        self.media = RocketChatMediaBridge(
            self,
            cache_dir=media_cache_dir,
            media_publication_service=media_publication_service,
        )
        self._consecutive_reconnect_failures = 0
        self._last_rest_login_at = 0.0
        self._last_websocket_activity_at = 0.0
        self._last_inbound_message_at = 0.0
        self._last_outbound_message_at = 0.0
        self._last_disconnect_reason = ""

    async def start(self, *, start_realtime: bool = True) -> None:
        if self._running:
            return
        self._running = True
        connector = aiohttp.TCPConnector(
            limit=64,
            limit_per_host=16,
            ttl_dns_cache=300,
            keepalive_timeout=30,
        )
        self._http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=45.0),
            connector=connector,
            json_serialize=json_dumps,
        )
        try:
            try:
                await self.media.start()
            except Exception as exc:
                logger.warning(
                    "[RocketChatOneBotBridge][E2EE] 媒体缓存目录初始化失败，"
                    "RocketChat 媒体可能无法上报给 OneBot: %r",
                    exc,
                )
            await self._detect_server_capabilities()
            await self._rest_login()
        except UnsupportedRocketChatVersionError:
            self._running = False
            await self.media.stop()
            await self._http_session.close()
            self._http_session = None
            raise
        except Exception:
            self._running = False
            await self.media.stop()
            await self._http_session.close()
            self._http_session = None
            raise
        if start_realtime:
            await self.start_realtime()

    async def start_realtime(self) -> None:
        if not self._running:
            raise RuntimeError("Rocket.Chat 客户端尚未初始化")
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_forever(initial_login_complete=True))

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._cancel_background_tasks()
        await self._stop_inbound_workers()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._http_session is not None:
            await self._http_session.close()
        self._http_session = None
        await self.media.stop()
        self._seen_inbound_message_signatures.clear()
        self._server_branding_cache = None
        self._server_branding_cache_expires_at = 0.0
        self._clear_self_message_waiters()
        self._consecutive_reconnect_failures = 0
        self.bot_profile = {}

    async def _run_forever(self, *, initial_login_complete: bool = False) -> None:
        login_ready = bool(initial_login_complete and self.auth_token and self.user_id)
        while self._running:
            try:
                if not login_ready:
                    await self._rest_login()
                login_ready = False
                await self.e2ee.initialize()
                await self._ws_connect_and_listen()
                if self._running:
                    self._last_disconnect_reason = "websocket disconnected"
                    logger.warning(
                        f"[RocketChatOneBotBridge] Rocket.Chat 连接已断开，{self.config.reconnect_delay:.1f}s 后重连。"
                    )
                    await asyncio.sleep(self.config.reconnect_delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    break
                self.auth_token = None
                self.user_id = None
                login_ready = False
                self._last_disconnect_reason = repr(exc)
                self._consecutive_reconnect_failures += 1
                if self._should_stop_reconnect():
                    await self._handle_reconnect_exhausted(exc)
                    break
                logger.warning(
                    f"[RocketChatOneBotBridge] Rocket.Chat 重连失败第 {self._consecutive_reconnect_failures} 次: {exc!r}，{self.config.reconnect_delay:.1f}s 后继续重连。"
                )
                await asyncio.sleep(self.config.reconnect_delay)

    def _should_stop_reconnect(self) -> bool:
        max_attempts = self.config.max_reconnect_attempts
        return max_attempts > 0 and self._consecutive_reconnect_failures >= max_attempts

    async def _handle_reconnect_exhausted(self, exc: Exception) -> None:
        self._running = False
        self._last_disconnect_reason = repr(exc)
        logger.error("[RocketChatOneBotBridge] 连接失败，已自动关闭rocketchat桥接器，请检查网络或目标服务器状态")
        if self._on_reconnect_exhausted is not None:
            await self._on_reconnect_exhausted(
                "Rocket.Chat",
                self._consecutive_reconnect_failures,
                repr(exc),
            )

    def build_diagnostic_snapshot(self) -> dict[str, Any]:
        authenticated = bool(self.auth_token and self.user_id)
        if authenticated:
            auth_state = "authenticated"
        elif self.auth_token or self.user_id:
            auth_state = "partial"
        else:
            auth_state = "disconnected"
        return {
            "authenticated": authenticated,
            "auth_state": auth_state,
            "reconnect_failures": self._consecutive_reconnect_failures,
            "last_rest_login_at": self._last_rest_login_at or None,
            "last_websocket_activity_at": self._last_websocket_activity_at or None,
            "last_inbound_message_at": self._last_inbound_message_at or None,
            "last_outbound_message_at": self._last_outbound_message_at or None,
            "last_disconnect_reason": self._last_disconnect_reason,
            "server_version": self.capabilities.version_text,
            "compatibility_status": self.capabilities.compatibility_status,
            "upload_endpoint": self.media.active_plain_upload_endpoint,
            "method_transport": self._method_transport,
            "method_rest_fallbacks": self._method_rest_fallbacks,
            "local_media_base_url": self.media.local_media_base_url,
        }

    def _mark_websocket_activity(self) -> None:
        self._last_websocket_activity_at = time.time()

    def _mark_inbound_message_activity(self) -> None:
        now = time.time()
        self._last_inbound_message_at = now
        self._last_websocket_activity_at = now

    def _mark_outbound_message_activity(self) -> None:
        self._last_outbound_message_at = time.time()

    @staticmethod
    def _parse_retry_after(
        headers: dict[str, str],
        data: Any,
        *,
        default: float,
    ) -> float:
        retry_after = str(
            next(
                (
                    value
                    for key, value in headers.items()
                    if str(key).lower() == "retry-after"
                ),
                "",
            )
        ).strip()
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), 30.0))
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(retry_after)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    return max(
                        0.0,
                        min((retry_at - datetime.now(timezone.utc)).total_seconds(), 30.0),
                    )
                except (TypeError, ValueError, OverflowError):
                    pass

        if isinstance(data, dict):
            candidates = [
                data.get("timeToReset"),
                (data.get("details") or {}).get("timeToReset")
                if isinstance(data.get("details"), dict)
                else None,
                (data.get("errorDetails") or {}).get("timeToReset")
                if isinstance(data.get("errorDetails"), dict)
                else None,
            ]
            for candidate in candidates:
                try:
                    value = float(candidate)
                except (TypeError, ValueError):
                    continue
                if value > 10_000_000_000:
                    value = (value / 1000.0) - time.time()
                elif value > 10_000_000:
                    value -= time.time()
                elif value > 1000:
                    value /= 1000.0
                return max(0.0, min(value, 30.0))
        return max(0.0, min(default, 30.0))

    async def _request_json(self, method: str, url: str, **kwargs) -> dict[str, Any]:
        if self._http_session is None:
            raise RuntimeError("Rocket.Chat HTTP session 尚未初始化")

        normalized_method = str(method or "GET").upper()
        idempotent = normalized_method in {"GET", "HEAD", "OPTIONS"}
        max_attempts = 3 if idempotent else 1

        for attempt in range(1, max_attempts + 1):
            async with self._http_session.request(normalized_method, url, **kwargs) as resp:
                response_text = await resp.text()
                data: Any = None
                if response_text:
                    try:
                        data = json_loads(response_text)
                    except Exception:
                        data = None
                headers = {str(key): str(value) for key, value in resp.headers.items()}

                if resp.status < 400:
                    if not isinstance(data, dict):
                        raise RuntimeError(
                            f"Rocket.Chat 返回了非 JSON 对象: {normalized_method} {url} "
                            f"status={resp.status} content_type={resp.headers.get('Content-Type', '-')}"
                        )
                    return data

                error = RocketChatHTTPError(
                    method=normalized_method,
                    url=url,
                    status=resp.status,
                    data=data,
                    headers=headers,
                    response_text=response_text,
                )
                retryable = resp.status == 429 or 500 <= resp.status <= 599
                if not (idempotent and retryable and attempt < max_attempts):
                    raise error

                default_delay = 0.5 * (2 ** (attempt - 1))
                delay = self._parse_retry_after(headers, data, default=default_delay)
                logger.warning(
                    "[RocketChatOneBotBridge] Rocket.Chat REST 请求准备重试: "
                    "method=%s status=%s attempt=%s/%s delay=%.2fs url=%s",
                    normalized_method,
                    resp.status,
                    attempt,
                    max_attempts,
                    delay,
                    url,
                )
                await asyncio.sleep(delay)

        raise RuntimeError(f"Rocket.Chat REST 请求意外结束: {normalized_method} {url}")

    async def _detect_server_capabilities(self) -> None:
        try:
            data = await self._request_json(
                "GET",
                f"{self.config.server_url}/api/info",
            )
        except Exception as exc:
            self.capabilities = RocketChatCapabilities.unknown()
            logger.warning(
                "[RocketChatOneBotBridge] 无法读取 Rocket.Chat /api/info，"
                "将使用能力探测模式: server=%s error=%r",
                self.config.server_url,
                exc,
            )
            return

        self.server_info = dict(data)
        self.cloud_workspace_id = str(data.get("cloudWorkspaceId") or "").strip()
        self.capabilities = RocketChatCapabilities.from_version(data.get("version"))
        if self.capabilities.compatibility_status == "unsupported":
            assert self.capabilities.version is not None
            raise UnsupportedRocketChatVersionError(self.capabilities.version)

        logger.info(
            "[RocketChatOneBotBridge] Rocket.Chat 版本能力已识别: "
            "server=%s version=%s compatibility=%s legacy_upload_fallback=%s "
            "ddp_method_fallback=%s strict_rest=%s secure_upload=%s",
            self.config.server_url,
            self.capabilities.version_text,
            self.capabilities.compatibility_status,
            self.capabilities.allows_legacy_upload_fallback,
            self.capabilities.allows_ddp_method_fallback,
            self.capabilities.strict_rest_validation,
            self.capabilities.secure_upload_validation,
        )

    async def _request_text(self, method: str, url: str, **kwargs) -> str:
        if self._http_session is None:
            raise RuntimeError("Rocket.Chat HTTP session 尚未初始化")
        async with self._http_session.request(method, url, **kwargs) as resp:
            return await resp.text()

    async def _cancel_background_tasks(self) -> None:
        if not self._background_tasks:
            return
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        try:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        finally:
            self._background_tasks.clear()

    def _start_inbound_workers(self) -> None:
        if self._inbound_workers:
            return
        self._inbound_queues = [
            asyncio.Queue(maxsize=self._INBOUND_QUEUE_MAX_SIZE)
            for _ in range(self._INBOUND_WORKER_COUNT)
        ]
        for index in range(self._INBOUND_WORKER_COUNT):
            task = asyncio.create_task(
                self._inbound_worker_loop(self._inbound_queues[index]),
                name=f"RocketChatInboundWorker:{index + 1}",
            )
            self._inbound_workers.add(task)
            task.add_done_callback(self._inbound_workers.discard)

    async def _stop_inbound_workers(self) -> None:
        workers = list(self._inbound_workers)
        self._inbound_workers.clear()
        for task in workers:
            if not task.done():
                task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._inbound_queues = []

    async def _drain_inbound_queue(self) -> None:
        if not self._inbound_queues:
            return
        await asyncio.gather(*(queue_obj.join() for queue_obj in self._inbound_queues))

    async def _enqueue_incoming_message(self, raw_msg: dict[str, Any]) -> None:
        if not self._inbound_queues:
            await self._handle_incoming_message(raw_msg)
            return
        room_id = str(raw_msg.get("rid") or "")
        bucket = int.from_bytes(
            hashlib.blake2b(room_id.encode("utf-8"), digest_size=2).digest(),
            "big",
        ) % len(self._inbound_queues)
        queue_obj = self._inbound_queues[bucket]
        await queue_obj.put(raw_msg)

    async def _inbound_worker_loop(self, queue_obj: asyncio.Queue[dict[str, Any]]) -> None:
        while self._running:
            raw_msg = await queue_obj.get()
            try:
                await self._handle_incoming_message(raw_msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "[RocketChatOneBotBridge] Rocket.Chat inbound worker 处理消息异常: %r",
                    exc,
                )
            finally:
                queue_obj.task_done()

    def _clear_self_message_waiters(self) -> None:
        for waiters in self._pending_self_message_waiters.values():
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
        self._pending_self_message_waiters.clear()
        self._recent_self_messages.clear()

    def _remember_self_message(self, raw_msg: dict[str, Any]) -> None:
        room_id = str(raw_msg.get("rid") or "")
        if not room_id:
            return

        loop = asyncio.get_running_loop()
        now = loop.time()
        bucket = self._recent_self_messages.setdefault(room_id, [])
        bucket.append({"ts": now, "raw": raw_msg})
        bucket[:] = [entry for entry in bucket if now - float(entry.get("ts") or 0.0) <= 10.0]

        waiters = self._pending_self_message_waiters.get(room_id) or []
        while waiters and waiters[0].done():
            waiters.pop(0)
        if waiters:
            waiter = waiters.pop(0)
            if not waiter.done():
                waiter.set_result(raw_msg)
        if not waiters:
            self._pending_self_message_waiters.pop(room_id, None)

    def _pop_recent_self_message(self, room_id: str) -> dict[str, Any] | None:
        bucket = self._recent_self_messages.get(room_id)
        if not bucket:
            return None

        now = asyncio.get_running_loop().time()
        bucket[:] = [entry for entry in bucket if now - float(entry.get("ts") or 0.0) <= 10.0]
        if not bucket:
            self._recent_self_messages.pop(room_id, None)
            return None

        entry = bucket.pop(0)
        if not bucket:
            self._recent_self_messages.pop(room_id, None)
        raw = entry.get("raw")
        return raw if isinstance(raw, dict) else None

    async def await_sent_message_echo(
        self,
        room_id: str,
        *,
        timeout: float = 2.0,
    ) -> dict[str, Any] | None:
        cached = self._pop_recent_self_message(room_id)
        if cached is not None:
            return cached

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        waiters = self._pending_self_message_waiters.setdefault(room_id, [])
        waiters.append(future)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            room_waiters = self._pending_self_message_waiters.get(room_id)
            if room_waiters and future in room_waiters:
                room_waiters.remove(future)
                if not room_waiters:
                    self._pending_self_message_waiters.pop(room_id, None)

    async def _rest_login(self) -> None:
        data = await self._request_json(
            "POST",
            f"{self.config.server_url}/api/v1/login",
            json={"user": self.config.username, "password": self.config.password},
        )
        if data.get("status") != "success":
            raise RuntimeError(f"Rocket.Chat REST 登录失败: {data}")
        login_data = data["data"]
        self.auth_token = login_data["authToken"]
        self.user_id = login_data["userId"]
        self._last_rest_login_at = time.time()
        self._last_disconnect_reason = ""
        me = login_data.get("me") if isinstance(login_data.get("me"), dict) else {}
        self.bot_username = me.get("username") or self.config.username
        self.bot_profile = dict(me)
        if self.user_id:
            self.bot_profile.setdefault("_id", self.user_id)
            self.bot_profile.setdefault("username", self.bot_username)
            self._user_cache[str(self.user_id)] = dict(self.bot_profile)
        logger.info(
            f"[RocketChatOneBotBridge] Rocket.Chat 登录成功 | userId={self.user_id} | username={self.bot_username}"
        )
        if self.config.e2ee_password.strip():
            logger.info("[RocketChatOneBotBridge] E2EE密钥已设置")
        else:
            logger.info("[RocketChatOneBotBridge] 未设置E2EE密钥")

    def _auth_headers(self) -> dict[str, str]:
        if not self.auth_token or not self.user_id:
            raise RuntimeError("Rocket.Chat 尚未登录")
        return {
            "X-Auth-Token": self.auth_token,
            "X-User-Id": self.user_id,
            "Content-Type": "application/json",
        }

    async def get_room_info(self, room_id: str, refresh: bool = False) -> dict[str, Any]:
        if not refresh:
            cached_room = self._get_cached_room_info(room_id)
            if cached_room is not None:
                return cached_room

        data = await self._request_json(
            "GET",
            f"{self.config.server_url}/api/v1/rooms.info?roomId={room_id}",
            headers=self._auth_headers(),
        )
        room = data.get("room", {}) if data.get("success") else {}
        fallback = {"_id": room_id, "t": self._room_type_cache.get(room_id, "c") or "c"}
        final_room = room or fallback
        self._cache_room_info(final_room)
        return self._room_info_cache.get(room_id, final_room)

    def _get_cached_room_info(self, room_id: str) -> dict[str, Any] | None:
        cached_room = self._room_info_cache.get(room_id)
        if cached_room is None:
            return None

        expires_at = self._room_info_cache_expires_at.get(room_id)
        if expires_at is not None and time.monotonic() >= expires_at:
            self._room_info_cache.pop(room_id, None)
            self._room_info_cache_expires_at.pop(room_id, None)
            return None
        return cached_room

    async def get_room_type(self, room_id: str) -> str:
        room = await self.get_room_info(room_id)
        return str(room.get("t", "c"))

    async def get_user_info(self, user_id: str, refresh: bool = False) -> dict[str, Any]:
        if not refresh and user_id in self._user_cache:
            return self._user_cache[user_id]
        data = await self._request_json(
            "GET",
            f"{self.config.server_url}/api/v1/users.info?userId={user_id}",
            headers=self._auth_headers(),
        )
        user = data.get("user", {}) if data.get("success") else {}
        if user:
            self._user_cache[user_id] = user
        return user

    async def get_current_user_info(self, refresh: bool = False) -> dict[str, Any]:
        current_user_id = str(self.user_id or "").strip()
        if not current_user_id:
            return dict(self.bot_profile)

        if not refresh:
            cached = self._user_cache.get(current_user_id)
            if isinstance(cached, dict) and cached:
                return dict(cached)
            if self.bot_profile:
                return dict(self.bot_profile)

        user = await self.get_user_info(current_user_id, refresh=refresh)
        if user:
            self.bot_profile = dict(user)
            self.bot_profile.setdefault("_id", current_user_id)
            self.bot_profile.setdefault("username", self.bot_username or self.config.username)
            return dict(self.bot_profile)
        return dict(self.bot_profile)

    def build_avatar_url(self, username: str | None = None) -> str:
        normalized_username = str(username or self.bot_username or self.config.username or "").strip()
        normalized_server = str(self.config.server_url or "").strip().rstrip("/")
        if not normalized_username or not normalized_server:
            return ""
        return f"{normalized_server}/avatar/{quote(normalized_username, safe='')}"

    def resolve_avatar_url(
        self,
        user_info: dict[str, Any] | None = None,
        username: str | None = None,
    ) -> str:
        if isinstance(user_info, dict):
            avatar_url = str(user_info.get("avatarUrl") or "").strip()
            if avatar_url:
                return avatar_url
        return self.build_avatar_url(username)

    def build_server_logo_url(self) -> str:
        normalized_server = str(self.config.server_url or "").strip().rstrip("/")
        if not normalized_server:
            return ""
        return f"{normalized_server}/assets/logo_dark.png"

    async def get_server_branding_summary(self, refresh: bool = False) -> dict[str, str] | None:
        normalized_server = str(self.config.server_url or "").strip().rstrip("/")
        if not normalized_server:
            return None

        now = time.monotonic()
        if (
            not refresh
            and self._server_branding_cache is not None
            and now < self._server_branding_cache_expires_at
        ):
            return dict(self._server_branding_cache)

        display_name = ""
        try:
            html = await self._request_text("GET", normalized_server)
        except Exception:
            html = ""

        if html:
            meta_match = self._HTML_APP_NAME_RE.search(html)
            title_match = self._HTML_TITLE_RE.search(html)
            display_name = str(
                (meta_match.group(1) if meta_match else "")
                or (title_match.group(1) if title_match else "")
            ).strip()
            display_name = unescape(display_name).strip()

        if not display_name:
            display_name = str(urlparse(normalized_server).netloc or normalized_server).strip()

        summary = {
            "display_name": display_name,
            "avatar_url": self.build_server_logo_url(),
        }
        self._server_branding_cache = dict(summary)
        self._server_branding_cache_expires_at = now + self._SERVER_BRANDING_CACHE_TTL_SECONDS
        return summary

    async def get_room_members(self, room_id: str) -> list[dict[str, Any]]:
        room_type = await self.get_room_type(room_id)
        if room_type == "d":
            return []
        endpoint = "channels.members" if room_type == "c" else "groups.members"
        data = await self._request_json(
            "GET",
            f"{self.config.server_url}/api/v1/{endpoint}?roomId={room_id}",
            headers=self._auth_headers(),
        )
        members = data.get("members", []) if data.get("success") else []
        for member in members:
            member_id = member.get("_id")
            if member_id:
                self._user_cache[str(member_id)] = member
        return [member for member in members if isinstance(member, dict)]

    async def get_or_create_direct_room(self, user_source_id: str) -> str:
        existing = None
        if user_source_id in self._user_cache:
            existing = self._user_cache[user_source_id]
        if existing is None:
            existing = await self.get_user_info(user_source_id)
        username = existing.get("username") if isinstance(existing, dict) else None
        if not username:
            raise RuntimeError(f"无法为用户 {user_source_id} 创建私聊：缺少 username")
        data = await self._request_json(
            "POST",
            f"{self.config.server_url}/api/v1/im.create",
            headers=self._auth_headers(),
            json={"username": username},
        )
        room = data.get("room", {}) if data.get("success") else {}
        room_id = room.get("rid") or room.get("_id")
        if not room_id:
            raise RuntimeError(f"创建 Rocket.Chat 私聊失败: {data}")
        self._cache_room_info({"_id": room_id, "t": "d", "name": username, "fname": existing.get("name")})
        return str(room_id)

    async def fetch_message_by_id(self, message_id: str) -> dict[str, Any] | None:
        data = await self._request_json(
            "GET",
            f"{self.config.server_url}/api/v1/chat.getMessage?msgId={message_id}",
            headers=self._auth_headers(),
        )
        if not data.get("success"):
            return None
        message = data.get("message")
        if not isinstance(message, dict):
            return None
        return await self.e2ee.maybe_decrypt_message(message)

    async def set_message_reaction(
        self,
        message_id: str,
        emoji: str,
        *,
        should_react: bool,
    ) -> bool:
        normalized_emoji = str(emoji or "").strip()
        if not normalized_emoji:
            raise ValueError("emoji 不能为空")

        data = await self._request_json(
            "POST",
            f"{self.config.server_url}/api/v1/chat.react",
            headers=self._auth_headers(),
            json={
                "messageId": str(message_id),
                "emoji": normalized_emoji,
                "shouldReact": bool(should_react),
            },
        )
        if not data.get("success"):
            logger.error(
                "[RocketChatOneBotBridge] Rocket.Chat 贴表情失败: message_id=%s emoji=%s should_react=%s data=%s",
                message_id,
                normalized_emoji,
                should_react,
                data,
            )
            return False
        return True

    async def set_room_typing(
        self,
        room_id: str,
        *,
        is_typing: bool,
    ) -> bool:
        normalized_room_id = str(room_id or "").strip()
        if not normalized_room_id:
            raise ValueError("room_id 不能为空")

        actor_name = str(
            self.bot_profile.get("name")
            or self.bot_username
            or self.config.username
            or ""
        ).strip()
        if not actor_name:
            logger.error("[RocketChatOneBotBridge] Rocket.Chat typing 发送失败: 缺少 bot 身份名称")
            return False

        activities = ["user-typing"] if is_typing else []
        try:
            await self.call_realtime_method(
                "stream-notify-room",
                [f"{normalized_room_id}/user-activity", actor_name, activities, {}],
                timeout=10.0,
            )
        except Exception as exc:
            logger.error(
                "[RocketChatOneBotBridge] Rocket.Chat typing 发送失败: room_id=%s is_typing=%s err=%r",
                normalized_room_id,
                is_typing,
                exc,
            )
            return False
        return True

    async def _post_json_message(self, url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        data = await self._request_json(
            "POST",
            url,
            headers=self._auth_headers(),
            json=payload,
        )
        if not data.get("success"):
            logger.error(f"[RocketChatOneBotBridge] Rocket.Chat 发送失败: {data}")
            return None
        self._mark_outbound_message_activity()
        return data

    async def _send_structured_message(
        self,
        room_id: str,
        text: str = "",
        *,
        attachments: list[dict[str, Any]] | None = None,
        tmid: str | None = None,
        e2e_mentions: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        room_info = await self.get_room_info(room_id)
        room_is_e2ee = bool(room_info.get("encrypted") and room_info.get("t") in {"d", "p"})
        if room_is_e2ee and not await self.e2ee.should_encrypt_room(room_info):
            logger.warning(
                f"[RocketChatOneBotBridge][E2EE] 加密房间消息发送失败，E2EE 未就绪 room_id={room_id!r}"
            )
            return None

        if room_is_e2ee:
            payload = await self.e2ee.build_send_message(
                room_id,
                text=text,
                attachments=attachments,
                tmid=tmid,
                e2e_mentions=e2e_mentions,
            )
            if not payload:
                return None
            data = await self._post_json_message(
                f"{self.config.server_url}/api/v1/chat.sendMessage",
                payload,
            )
        else:
            payload = {"roomId": room_id, "text": text}
            if attachments:
                payload["attachments"] = attachments
            if tmid:
                payload["tmid"] = tmid
            data = await self._post_json_message(
                f"{self.config.server_url}/api/v1/chat.postMessage",
                payload,
            )
        return (data or {}).get("message") or data

    async def _normalize_media_url(self, media_url: str) -> str:
        url = media_url
        if not (url.startswith("http://") or url.startswith("https://")):
            if url.startswith("/"):
                url = f"{self.config.server_url}{url}"
            else:
                url = f"{self.config.server_url}/{url}"

        if self.user_id and self.auth_token and url.startswith(self.config.server_url):
            if "rc_uid=" not in url and "rc_token=" not in url:
                delimiter = "&" if "?" in url else "?"
                url = f"{url}{delimiter}rc_uid={self.user_id}&rc_token={self.auth_token}"
        return url

    async def _build_e2ee_mentions(
        self,
        room_id: str,
        mention_usernames: list[str] | None,
        reply_mention_username: str | None = None,
    ) -> dict[str, Any] | None:
        room_info = await self.get_room_info(room_id)
        if not (room_info.get("encrypted") and room_info.get("t") == "p"):
            return None

        normalized_mentions: list[str] = []
        seen: set[str] = set()
        for username in [reply_mention_username, *(mention_usernames or [])]:
            normalized = str(username or "").strip().lstrip("@")
            if not normalized or normalized == "all" or normalized in seen:
                continue
            seen.add(normalized)
            normalized_mentions.append(f"@{normalized}")

        if not normalized_mentions:
            return None
        return {
            "e2eUserMentions": normalized_mentions,
            "e2eChannelMentions": [],
        }

    async def _build_explicit_reply_mention(
        self,
        room_id: str,
        mention_username: str | None,
    ) -> str | None:
        room_info = await self.get_room_info(room_id)
        if not (mention_username and room_info.get("encrypted") and room_info.get("t") == "p"):
            return None

        normalized = str(mention_username or "").strip().lstrip("@")
        bot_username = str(self.bot_username or self.config.username or "").strip().lstrip("@")
        if not normalized or normalized == bot_username:
            return None
        return f"@{normalized}"

    def _strip_leading_reply_mention(self, text: str, mention_username: str | None) -> str:
        normalized = str(mention_username or "").strip().lstrip("@")
        if not normalized:
            return text

        prefix = f"@{normalized}"
        stripped = text.lstrip()
        if not stripped.startswith(prefix):
            return text

        remainder = stripped[len(prefix):].lstrip()
        return remainder

    async def send_text(
        self,
        room_id: str,
        text: str,
        tmid: str | None = None,
        mention_usernames: list[str] | None = None,
        reply_mention_username: str | None = None,
    ) -> dict[str, Any] | None:
        if not text:
            raise ValueError("send_text 需要非空文本")
        mention_text = await self._build_explicit_reply_mention(room_id, reply_mention_username)
        final_text = text
        if mention_text:
            final_text = self._strip_leading_reply_mention(final_text, reply_mention_username)
            final_text = f"{mention_text} {final_text}".strip()
        return await self._send_structured_message(
            room_id,
            final_text,
            tmid=tmid,
            e2e_mentions=await self._build_e2ee_mentions(
                room_id,
                mention_usernames,
                reply_mention_username=reply_mention_username,
            ),
        )

    async def send_with_quote(
        self,
        room_id: str,
        text: str,
        quoted_message_source_id: str,
        *,
        tmid: str | None = None,
        mention_usernames: list[str] | None = None,
        reply_mention_username: str | None = None,
    ) -> dict[str, Any] | None:
        link = self._build_message_link(room_id, quoted_message_source_id)
        mention_text = await self._build_explicit_reply_mention(room_id, reply_mention_username)
        reply_line = text
        if mention_text:
            reply_line = self._strip_leading_reply_mention(reply_line, reply_mention_username)
            reply_line = f"{mention_text} {reply_line}".strip()
        final_text = f"[ ]({link})\n{reply_line}" if link and reply_line else f"[ ]({link})" if link else reply_line
        return await self._send_structured_message(
            room_id,
            final_text,
            tmid=tmid,
            e2e_mentions=await self._build_e2ee_mentions(
                room_id,
                mention_usernames,
                reply_mention_username=reply_mention_username,
            ),
        )

    async def send_image_url(
        self,
        room_id: str,
        image_url: str,
        text: str = "",
        tmid: str | None = None,
        *,
        require_mappable_message: bool = True,
    ) -> dict[str, Any] | None:
        return await self.media.send_image_url(
            room_id,
            image_url,
            text=text,
            tmid=tmid,
            require_mappable_message=require_mappable_message,
        )

    async def send_image_file(
        self,
        room_id: str,
        file_path: str,
        description: str = "",
        tmid: str | None = None,
        *,
        require_mappable_message: bool = True,
    ) -> dict[str, Any] | None:
        return await self.media.send_image_file(
            room_id,
            file_path,
            description=description,
            tmid=tmid,
            require_mappable_message=require_mappable_message,
        )

    async def send_file(
        self,
        room_id: str,
        file_path: str,
        filename: str | None = None,
        description: str = "",
        tmid: str | None = None,
        *,
        require_mappable_message: bool = True,
    ) -> dict[str, Any] | None:
        return await self.media.send_file(
            room_id,
            file_path,
            filename=filename,
            description=description,
            tmid=tmid,
            require_mappable_message=require_mappable_message,
        )

    async def send_remote_media_fallback(
        self,
        room_id: str,
        media_url: str,
        *,
        media_kind: str,
        text: str = "",
        tmid: str | None = None,
    ) -> dict[str, Any] | None:
        return await self.media.send_remote_media_fallback(
            room_id,
            media_url,
            media_kind=media_kind,
            text=text,
            tmid=tmid,
        )

    async def _download_remote_media(
        self,
        url: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        return await self.media.download_remote_media(url, default_suffix)

    def _decode_base64_media(
        self,
        file_ref: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        return self.media.decode_base64_media(file_ref, default_suffix)

    async def send_message_segments(
        self,
        room_id: str,
        segments: list[dict[str, Any]],
        *,
        thread_source_id: str | None = None,
        reply_source_id: str | None = None,
        mention_usernames: list[str] | None = None,
        reply_mention_username: str | None = None,
    ) -> list[dict[str, Any]]:
        sent_messages: list[dict[str, Any]] = []
        text_parts: list[str] = []
        current_thread_source_id = str(thread_source_id or "").strip() or None
        quote_pending = reply_source_id
        pending_mentions = list(mention_usernames or [])
        pending_reply_mention = str(reply_mention_username or "").strip() or None

        async def flush_text(force_quote: bool = False) -> None:
            nonlocal quote_pending, pending_mentions, pending_reply_mention
            text = self._normalize_outbound_text("".join(text_parts))
            text_parts.clear()
            if not text and not (force_quote and quote_pending):
                return

            if quote_pending:
                raw_message = await self.send_with_quote(
                    room_id,
                    text,
                    str(quote_pending),
                    tmid=current_thread_source_id,
                    mention_usernames=pending_mentions,
                    reply_mention_username=pending_reply_mention,
                )
                quote_pending = None
            else:
                raw_message = await self.send_text(
                    room_id,
                    text,
                    tmid=current_thread_source_id,
                    mention_usernames=pending_mentions,
                    reply_mention_username=pending_reply_mention,
                )

            pending_mentions = []
            pending_reply_mention = None

            if raw_message:
                sent_messages.append(raw_message)

        segment_index = 0
        while segment_index < len(segments):
            segment = segments[segment_index]
            segment_index += 1
            segment_type = str(segment.get("type") or "text")
            data = segment.get("data", {}) or {}

            if segment_type == "text":
                text_parts.append(str(data.get("text") or ""))
                continue

            had_pending_quote = bool(quote_pending)
            if text_parts:
                await flush_text()
            elif quote_pending:
                await flush_text(force_quote=True)

            media_description = ""
            if (
                segment_type == "image"
                and not had_pending_quote
                and not pending_mentions
                and not pending_reply_mention
            ):
                media_description, segment_index = self._collect_trailing_text_caption(
                    segments,
                    segment_index,
                )

            raw_message = await self._send_media_segment(
                room_id,
                segment_type,
                data,
                tmid=current_thread_source_id,
                description=media_description,
            )
            if raw_message:
                sent_messages.append(raw_message)

        await flush_text(force_quote=bool(quote_pending))
        return sent_messages

    def _collect_trailing_text_caption(
        self,
        segments: list[dict[str, Any]],
        start_index: int,
    ) -> tuple[str, int]:
        caption_parts: list[str] = []
        index = start_index
        while index < len(segments):
            segment = segments[index]
            segment_type = str(segment.get("type") or "text")
            if segment_type != "text":
                return "", start_index
            data = segment.get("data", {}) or {}
            caption_parts.append(str(data.get("text") or ""))
            index += 1

        caption = self._normalize_outbound_text("".join(caption_parts))
        return caption, index

    async def _send_media_segment(
        self,
        room_id: str,
        segment_type: str,
        data: dict[str, Any],
        *,
        tmid: str | None = None,
        description: str = "",
    ) -> dict[str, Any] | None:
        file_ref = str(data.get("file") or data.get("url") or "")
        if not file_ref:
            return None

        if segment_type == "image":
            if file_ref.startswith(("http://", "https://")):
                return await self.send_image_url(room_id, file_ref, text=description, tmid=tmid)
            local_path, cleanup = await self._resolve_uploadable_path(file_ref, ".png")
            if not local_path:
                return None
            try:
                return await self.send_image_file(
                    room_id,
                    local_path,
                    description=description,
                    tmid=tmid,
                )
            finally:
                if cleanup:
                    cleanup()

        default_name = {
            "record": "record.ogg",
            "video": "video.mp4",
            "file": "attachment",
        }.get(segment_type, "attachment")

        local_path, cleanup = await self._resolve_uploadable_path(
            file_ref,
            ".ogg" if segment_type == "record" else ".mp4" if segment_type == "video" else ".bin",
        )
        if not local_path:
            if file_ref.startswith(("http://", "https://")):
                return await self.send_remote_media_fallback(
                    room_id,
                    file_ref,
                    media_kind={
                        "record": "语音",
                        "video": "视频",
                        "file": "文件",
                    }.get(segment_type, "媒体"),
                    tmid=tmid,
                )
            return None

        try:
            return await self.send_file(
                room_id,
                local_path,
                filename=self._guess_filename(
                    file_ref,
                    local_path,
                    str(data.get("file_name") or data.get("name") or default_name),
                ),
                tmid=tmid,
            )
        finally:
            if cleanup:
                cleanup()

    async def _resolve_uploadable_path(
        self,
        file_ref: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        if file_ref.startswith(("http://", "https://")):
            return await self._download_remote_media(file_ref, default_suffix)
        if file_ref.startswith("base64://"):
            return self._decode_base64_media(file_ref, default_suffix)

        local_path = file_ref.replace("file:///", "").replace("file://", "")
        return (local_path or None, None)

    def _guess_filename(self, file_ref: str, local_path: str, fallback: str) -> str:
        if file_ref.startswith("base64://"):
            return fallback
        parsed = urlparse(file_ref)
        candidate = os.path.basename(parsed.path)
        if candidate:
            return candidate
        return os.path.basename(local_path) or fallback

    def _normalize_outbound_text(self, text: str) -> str:
        cleaned = text.replace("\u200b", "")
        return cleaned.strip()

    async def _get_subscriptions(self) -> list[dict[str, Any]]:
        data = await self._request_json(
            "GET",
            f"{self.config.server_url}/api/v1/subscriptions.get",
            headers=self._auth_headers(),
        )
        subscriptions = data.get("update", []) if data.get("success") else []
        for sub in subscriptions:
            if not isinstance(sub, dict):
                continue
            room_id = sub.get("rid")
            if not room_id:
                continue
            self._cache_room_info(
                {
                    "_id": room_id,
                    "t": sub.get("t", self._room_type_cache.get(room_id, "c")),
                    "name": sub.get("name"),
                    "fname": sub.get("fname"),
                    "encrypted": bool(sub.get("encrypted", False)),
                    "e2eKeyId": sub.get("e2eKeyId"),
                }
            )
        return [sub for sub in subscriptions if isinstance(sub, dict)]

    def _cache_room_info(self, room: dict[str, Any]) -> None:
        room_id = room.get("_id")
        if not room_id:
            return
        cached = dict(self._room_info_cache.get(str(room_id), {}))
        cached.update(room)
        self._room_info_cache[str(room_id)] = cached
        ttl_seconds = max(0.0, float(self.config.room_info_cache_ttl_seconds))
        if ttl_seconds > 0:
            self._room_info_cache_expires_at[str(room_id)] = time.monotonic() + ttl_seconds
        else:
            self._room_info_cache_expires_at.pop(str(room_id), None)
        room_type = cached.get("t")
        if room_type:
            self._room_type_cache[str(room_id)] = str(room_type)
        room_name = cached.get("name") or cached.get("fname")
        if room_name:
            self._room_name_cache[str(room_id)] = str(room_name)

    def _build_message_link(self, room_id: str, message_id: str) -> str:
        room_type = self._room_type_cache.get(room_id, "c")
        room_name = self._room_name_cache.get(room_id, room_id)
        if room_type == "c":
            path = f"channel/{room_name}"
        elif room_type == "p":
            path = f"group/{room_name}"
        else:
            path = f"direct/{room_id}"
        return f"{self.config.server_url}/{path}?msg={message_id}"

    async def _ws_connect_and_listen(self) -> None:
        if self._http_session is None:
            raise RuntimeError("Rocket.Chat HTTP session 尚未初始化")
        ws_url = self.config.server_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/websocket"
        self._subscribed_rooms.clear()
        async with self._http_session.ws_connect(
            ws_url,
            heartbeat=self._WEBSOCKET_HEARTBEAT_SECONDS,
            max_msg_size=8 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            try:
                self._start_inbound_workers()
                await self._ddp_connect(ws)
                await self._ddp_login(ws)
                subscriptions = await self._get_subscriptions()
                await self._ddp_subscribe_rooms(ws, subscriptions)
                await self._ddp_subscribe_user_events(ws)
                self._consecutive_reconnect_failures = 0
                self._mark_websocket_activity()
                self._last_disconnect_reason = ""
                logger.info(
                    f"[RocketChatOneBotBridge] Rocket.Chat WebSocket 就绪，共订阅 {len(subscriptions)} 个房间。"
                )
                if self.e2ee.enabled:
                    e2ee_task = asyncio.create_task(self.e2ee.on_ws_ready())
                    self._background_tasks.add(e2ee_task)
                    e2ee_task.add_done_callback(self._background_tasks.discard)
                await self._ws_listen_loop(ws)
            finally:
                if self._running:
                    await self._drain_inbound_queue()
                await self._stop_inbound_workers()
                self._ws = None

    async def _receive_ws_frame(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        *,
        timeout: float,
        stage: str,
    ) -> aiohttp.WSMessage:
        try:
            raw = await asyncio.wait_for(ws.receive(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"Rocket.Chat WebSocket {stage} 读取超时（{timeout:.1f}s）"
            ) from exc

        if raw.type == aiohttp.WSMsgType.ERROR:
            raise RuntimeError(
                f"Rocket.Chat WebSocket {stage} 异常: {ws.exception()!r}"
            )
        if raw.type in {
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.CLOSING,
        }:
            raise RuntimeError(
                f"Rocket.Chat WebSocket {stage} 已关闭 (close_code={getattr(ws, 'close_code', None)})"
            )
        self._mark_websocket_activity()
        return raw

    async def _ddp_call(
        self,
        method: str,
        params: list[Any] | None = None,
        timeout: float = 10.0,
    ) -> Any:
        if not self._ws or self._ws.closed:
            raise RuntimeError("ddp websocket not ready")

        self._ddp_call_id += 1
        call_id = f"ddp-{self._ddp_call_id}"
        future = asyncio.get_running_loop().create_future()
        self._pending_ddp_results[call_id] = future
        try:
            await self._ws.send_str(
                json_dumps(
                    {
                        "msg": "method",
                        "method": method,
                        "id": call_id,
                        "params": params or [],
                    }
                )
            )
            data = await asyncio.wait_for(future, timeout=timeout)
            if data.get("error"):
                raise RuntimeError(data["error"])
            return data.get("result")
        finally:
            self._pending_ddp_results.pop(call_id, None)

    def _parse_method_call_response(
        self,
        method: str,
        call_id: str,
        data: Any,
    ) -> Any:
        if not isinstance(data, dict):
            raise RuntimeError(
                f"Rocket.Chat method.call 返回了无效响应: method={method} type={type(data).__name__}"
            )
        encoded_message = data.get("message")
        if not isinstance(encoded_message, str) or not encoded_message:
            raise RuntimeError(
                f"Rocket.Chat method.call 响应缺少 message: method={method} data={data}"
            )
        try:
            result_frame = json_loads(encoded_message)
        except Exception as exc:
            raise RuntimeError(
                f"Rocket.Chat method.call 响应无法解析: method={method}"
            ) from exc
        if not isinstance(result_frame, dict) or result_frame.get("msg") != "result":
            raise RuntimeError(
                f"Rocket.Chat method.call 返回了非 result 帧: method={method} frame={result_frame}"
            )
        if str(result_frame.get("id") or "") != call_id:
            raise RuntimeError(
                f"Rocket.Chat method.call 响应 ID 不匹配: method={method} "
                f"expected={call_id} actual={result_frame.get('id')}"
            )
        if result_frame.get("error"):
            raise RocketChatMethodError(method, result_frame["error"])
        return result_frame.get("result")

    async def _call_method_via_rest(
        self,
        method: str,
        params: list[Any] | None = None,
    ) -> Any:
        self._method_call_id += 1
        call_id = f"rest-ddp-{self._method_call_id}"
        message = {
            "msg": "method",
            "method": method,
            "id": call_id,
            "params": params or [],
        }
        encoded_method = quote(method.replace("/", ":"), safe="")
        url = f"{self.config.server_url}/api/v1/method.call/{encoded_method}"
        try:
            data = await self._request_json(
                "POST",
                url,
                headers=self._auth_headers(),
                json={"message": json_dumps_compact(message)},
            )
        except RocketChatHTTPError as exc:
            if isinstance(exc.data, dict) and isinstance(exc.data.get("message"), str):
                return self._parse_method_call_response(method, call_id, exc.data)
            raise
        return self._parse_method_call_response(method, call_id, data)

    async def call_realtime_method(
        self,
        method: str,
        params: list[Any] | None = None,
        timeout: float = 10.0,
    ) -> Any:
        try:
            result = await asyncio.wait_for(
                self._call_method_via_rest(method, params),
                timeout=timeout,
            )
            self._method_transport = "rest"
            return result
        except RocketChatHTTPError as exc:
            if not (
                exc.endpoint_incompatible
                and self.capabilities.allows_ddp_method_fallback
            ):
                raise
            self._method_transport = "ddp-fallback"
            self._method_rest_fallbacks += 1
            logger.warning(
                "[RocketChatOneBotBridge] method.call REST 端点不可用，"
                "回退原始 DDP method: method=%s status=%s server_version=%s",
                method,
                exc.status,
                self.capabilities.version_text,
            )
            return await self._ddp_call(method, params, timeout=timeout)

    async def _ddp_connect(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await ws.send_str(json_dumps({"msg": "connect", "version": "1", "support": ["1"]}))
        while True:
            raw = await self._receive_ws_frame(
                ws,
                timeout=self._DDP_CONNECT_TIMEOUT_SECONDS,
                stage="DDP connect",
            )
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json_loads(raw.data)
            if data.get("msg") == "ping":
                await ws.send_str(json_dumps({"msg": "pong"}))
            elif data.get("msg") == "connected":
                return

    async def _ddp_login(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await ws.send_str(
            json_dumps(
                {
                    "msg": "method",
                    "method": "login",
                    "id": "ddp-login",
                    "params": [{"resume": self.auth_token}],
                }
            )
        )
        while True:
            raw = await self._receive_ws_frame(
                ws,
                timeout=self._DDP_LOGIN_TIMEOUT_SECONDS,
                stage="DDP login",
            )
            if raw.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json_loads(raw.data)
            if data.get("msg") == "ping":
                await ws.send_str(json_dumps({"msg": "pong"}))
            elif data.get("msg") == "result" and data.get("id") == "ddp-login":
                if data.get("error"):
                    raise RuntimeError(f"Rocket.Chat DDP 登录失败: {data['error']}")
                return

    async def _ddp_subscribe_rooms(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        subscriptions: list[dict[str, Any]],
    ) -> None:
        for sub in subscriptions:
            room_id = sub.get("rid")
            if not room_id:
                continue
            await ws.send_str(
                json_dumps(
                    {
                        "msg": "sub",
                        "id": f"room-{room_id}",
                        "name": "stream-room-messages",
                        "params": [room_id, False],
                    }
                )
            )
            self._subscribed_rooms.add(str(room_id))

    async def _ddp_subscribe_user_events(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        await ws.send_str(
            json_dumps(
                {
                    "msg": "sub",
                    "id": f"user-notif-{self.user_id}",
                    "name": "stream-notify-user",
                    "params": [f"{self.user_id}/rooms-changed", False],
                }
            )
        )

    async def _ws_listen_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while self._running:
            raw = await self._receive_ws_frame(
                ws,
                timeout=self._WEBSOCKET_READ_TIMEOUT_SECONDS,
                stage="listen",
            )
            if not self._running:
                break
            if raw.type == aiohttp.WSMsgType.TEXT:
                data = json_loads(raw.data)
                await self._dispatch_ddp(data, ws)

    async def _dispatch_ddp(self, data: dict[str, Any], ws: aiohttp.ClientWebSocketResponse) -> None:
        msg_type = data.get("msg")
        collection = data.get("collection", "")

        if msg_type == "ping":
            await ws.send_str(json_dumps({"msg": "pong"}))
            return

        if msg_type == "changed" and collection == "stream-room-messages":
            args = data.get("fields", {}).get("args", [])
            saw_message = False
            for raw_msg in args:
                if isinstance(raw_msg, dict):
                    saw_message = True
                    await self._enqueue_incoming_message(raw_msg)
            if saw_message:
                self._mark_inbound_message_activity()
            return

        if msg_type == "changed" and collection == "stream-notify-user":
            await self._handle_user_notification(data, ws)
            return

        if msg_type == "result":
            result_id = str(data.get("id", ""))
            future = self._pending_ddp_results.pop(result_id, None)
            if future is not None and not future.done():
                future.set_result(data)

    async def _handle_user_notification(
        self,
        data: dict[str, Any],
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        fields = data.get("fields", {})
        event_name = str(fields.get("eventName", ""))
        args = fields.get("args", [])
        if len(args) < 2 or not isinstance(args[1], dict):
            return
        event_type = args[0]
        room_payload = args[1]
        room_id = room_payload.get("_id") or room_payload.get("rid")
        if not room_id or not event_name.endswith("/rooms-changed"):
            return
        self._cache_room_info(
            {
                "_id": room_id,
                "t": room_payload.get("t", "c"),
                "name": room_payload.get("name"),
                "fname": room_payload.get("fname"),
                "encrypted": bool(room_payload.get("encrypted", False)),
                "e2eKeyId": room_payload.get("e2eKeyId"),
            }
        )
        if event_type == "inserted" and str(room_id) not in self._subscribed_rooms:
            await ws.send_str(
                json_dumps(
                    {
                        "msg": "sub",
                        "id": f"room-{room_id}",
                        "name": "stream-room-messages",
                        "params": [room_id, False],
                    }
                )
            )
            self._subscribed_rooms.add(str(room_id))

    async def _handle_incoming_message(self, raw_msg: dict[str, Any]) -> None:
        if raw_msg.get("t") and raw_msg.get("t") != "e2e":
            return
        if raw_msg.get("t") == "e2e":
            decrypted = await self.e2ee.maybe_decrypt_message(raw_msg)
            if not isinstance(decrypted, dict):
                return
            raw_msg = decrypted
        if not self._should_emit_inbound_message(raw_msg):
            return
        sender = raw_msg.get("u", {}) or {}
        sender_id = sender.get("_id")
        if sender_id:
            self._user_cache[str(sender_id)] = dict(sender)
        if sender_id and str(sender_id) == self.user_id:
            self._remember_self_message(raw_msg)
        if self.config.skip_own_messages and sender_id and str(sender_id) == self.user_id:
            return
        if self._on_message is not None:
            await self._on_message(raw_msg)

    def _should_emit_inbound_message(self, raw_msg: dict[str, Any]) -> bool:
        source_message_id = str(raw_msg.get("_id") or "").strip()
        if not source_message_id:
            return True

        signature = self._build_inbound_message_signature(raw_msg)
        previous = self._seen_inbound_message_signatures.get(source_message_id)
        self._seen_inbound_message_signatures[source_message_id] = signature
        if len(self._seen_inbound_message_signatures) > 5000:
            oldest_key = next(iter(self._seen_inbound_message_signatures))
            self._seen_inbound_message_signatures.pop(oldest_key, None)

        if previous == signature:
            logger.debug(
                "[RocketChatOneBotBridge] 忽略重复消息更新 source_id=%s",
                source_message_id,
            )
            return False
        return True

    def _build_inbound_message_signature(self, raw_msg: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in self._INBOUND_SIGNATURE_FIELD_NAMES:
            value = raw_msg.get(key)
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                value = self._stable_hash_json(value)
            parts.append(f"{key}={value}")
        for key in ("attachments", "files", "urls", "mentions"):
            value = raw_msg.get(key)
            if value:
                parts.append(f"{key}={self._stable_hash_json(value)}")
        return "\x1f".join(parts)

    def _stable_hash_json(self, value: Any) -> str:
        payload = json_dumps_compact(
            value,
            sort_keys=True,
            default=str,
        )
        return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()

    def _normalize_inbound_message_for_signature(self, value: Any) -> Any:
        if isinstance(value, dict):
            normalized: dict[str, Any] = {}
            for key, item in value.items():
                if key in self._INBOUND_SIGNATURE_IGNORED_KEYS:
                    continue
                normalized[key] = self._normalize_inbound_message_for_signature(item)
            return normalized
        if isinstance(value, list):
            return [self._normalize_inbound_message_for_signature(item) for item in value]
        return value

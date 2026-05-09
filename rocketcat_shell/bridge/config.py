from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_SERVER_URL = "http://127.0.0.1:3000"
DEFAULT_ONEBOT_WS_URL = "ws://127.0.0.1:6199/ws/"
DEFAULT_ONEBOT_SELF_ID = 910001
DEFAULT_RECONNECT_DELAY = 5.0
DEFAULT_MAX_RECONNECT_ATTEMPTS = 10
DEFAULT_ENABLE_SUBCHANNEL_SESSION_ISOLATION = True
DEFAULT_REMOTE_MEDIA_MAX_SIZE = 20 * 1024 * 1024
DEFAULT_ROOM_INFO_CACHE_TTL_SECONDS = 300.0
DEFAULT_PERF_TRACE_ENABLED = False


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def _coerce_int(value: Any, default: int) -> int:
    try:
        if value is None:
            return int(default)
        if isinstance(value, str) and not value.strip():
            return int(default)
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _coerce_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return float(default)
        if isinstance(value, str) and not value.strip():
            return float(default)
        return float(value)
    except (TypeError, ValueError):
        return float(default)


@dataclass(slots=True)
class BridgeConfig:
    enabled: bool
    server_url: str
    username: str
    password: str
    e2ee_password: str
    onebot_ws_url: str
    onebot_access_token: str
    onebot_self_id: int
    reconnect_delay: float
    max_reconnect_attempts: int
    enable_subchannel_session_isolation: bool
    remote_media_max_size: int
    room_info_cache_ttl_seconds: float
    perf_trace_enabled: bool
    skip_own_messages: bool
    debug: bool
    bot_id: str = ""
    display_name: str = ""
    transport_type: str = "websocket-client"

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "BridgeConfig":
        data = dict(payload or {})
        return cls(
            enabled=_coerce_bool(data.get("enabled", False)),
            server_url=str(data.get("server_url", DEFAULT_SERVER_URL) or DEFAULT_SERVER_URL).rstrip("/"),
            username=str(data.get("username", "") or ""),
            password=str(data.get("password", "") or ""),
            e2ee_password=str(data.get("e2ee_password", "") or ""),
            onebot_ws_url=str(data.get("onebot_ws_url", DEFAULT_ONEBOT_WS_URL) or DEFAULT_ONEBOT_WS_URL).strip(),
            onebot_access_token=str(data.get("onebot_access_token", "") or ""),
            onebot_self_id=_coerce_int(data.get("onebot_self_id", DEFAULT_ONEBOT_SELF_ID), DEFAULT_ONEBOT_SELF_ID),
            reconnect_delay=_coerce_float(data.get("reconnect_delay", DEFAULT_RECONNECT_DELAY), DEFAULT_RECONNECT_DELAY),
            max_reconnect_attempts=_coerce_int(
                data.get("max_reconnect_attempts", DEFAULT_MAX_RECONNECT_ATTEMPTS),
                DEFAULT_MAX_RECONNECT_ATTEMPTS,
            ),
            enable_subchannel_session_isolation=_coerce_bool(
                data.get(
                    "enable_subchannel_session_isolation",
                    DEFAULT_ENABLE_SUBCHANNEL_SESSION_ISOLATION,
                )
            ),
            remote_media_max_size=_coerce_int(
                data.get("remote_media_max_size", DEFAULT_REMOTE_MEDIA_MAX_SIZE),
                DEFAULT_REMOTE_MEDIA_MAX_SIZE,
            ),
            room_info_cache_ttl_seconds=_coerce_float(
                data.get("room_info_cache_ttl_seconds", DEFAULT_ROOM_INFO_CACHE_TTL_SECONDS),
                DEFAULT_ROOM_INFO_CACHE_TTL_SECONDS,
            ),
            perf_trace_enabled=_coerce_bool(
                data.get("perf_trace_enabled", DEFAULT_PERF_TRACE_ENABLED)
            ),
            skip_own_messages=_coerce_bool(data.get("skip_own_messages", True)),
            debug=_coerce_bool(data.get("debug", False)),
            bot_id=str(data.get("id") or data.get("bot_id") or "").strip(),
            display_name=str(data.get("name") or data.get("display_name") or "").strip(),
            transport_type=str(data.get("type") or data.get("transport_type") or "websocket-client").strip() or "websocket-client",
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "server_url": self.server_url,
            "username": self.username,
            "password": self.password,
            "e2ee_password": self.e2ee_password,
            "onebot_ws_url": self.onebot_ws_url,
            "onebot_access_token": self.onebot_access_token,
            "onebot_self_id": self.onebot_self_id,
            "reconnect_delay": self.reconnect_delay,
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "enable_subchannel_session_isolation": self.enable_subchannel_session_isolation,
            "remote_media_max_size": self.remote_media_max_size,
            "room_info_cache_ttl_seconds": self.room_info_cache_ttl_seconds,
            "perf_trace_enabled": self.perf_trace_enabled,
            "skip_own_messages": self.skip_own_messages,
            "debug": self.debug,
            "id": self.bot_id,
            "name": self.display_name,
            "type": self.transport_type,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.enabled:
            if not self.server_url.startswith(("http://", "https://")):
                errors.append("server_url 必须以 http:// 或 https:// 开头")
            if not self.onebot_ws_url.startswith(("ws://", "wss://")):
                errors.append("onebot_ws_url 必须以 ws:// 或 wss:// 开头")
            if not self.username:
                errors.append("enabled=true 时 username 不能为空")
            if not self.password:
                errors.append("enabled=true 时 password 不能为空")
            if self.onebot_self_id <= 0:
                errors.append("onebot_self_id 必须为正整数")
        if self.reconnect_delay < 0:
            errors.append("reconnect_delay 不能小于 0")
        if self.max_reconnect_attempts < 0:
            errors.append("max_reconnect_attempts 不能小于 0")
        if self.remote_media_max_size < 0:
            errors.append("remote_media_max_size 不能小于 0")
        if self.room_info_cache_ttl_seconds < 0:
            errors.append("room_info_cache_ttl_seconds 不能小于 0")
        return errors
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


DEFAULT_WEBUI_HOST = "127.0.0.1"
DEFAULT_WEBUI_PORT = 5751
DEFAULT_WEBUI_ACCESS_PASSWORD = "123456"
DEFAULT_MESSAGE_INDEX_MAX_ENTRIES = 1000
DEFAULT_ONEBOT_WS_URL = "ws://127.0.0.1:6199/ws/"
DEFAULT_RECONNECT_DELAY = 5.0
DEFAULT_MAX_RECONNECT_ATTEMPTS = 10
DEFAULT_ENABLE_SUBCHANNEL_SESSION_ISOLATION = True
DEFAULT_REMOTE_MEDIA_MAX_SIZE = 20 * 1024 * 1024
DEFAULT_ROOM_INFO_CACHE_TTL_SECONDS = 300.0
DEFAULT_PERF_TRACE_ENABLED = False
DEFAULT_SKIP_OWN_MESSAGES = True
DEFAULT_DEBUG = False


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
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


def _coerce_positive_int(value: Any, default: int, minimum: int = 1) -> int:
    coerced = _coerce_int(value, default)
    return coerced if coerced >= int(minimum) else int(default)


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
class ShellSettings:
    webui_host: str = DEFAULT_WEBUI_HOST
    webui_port: int = DEFAULT_WEBUI_PORT
    webui_access_password: str = DEFAULT_WEBUI_ACCESS_PASSWORD
    message_index_max_entries: int = DEFAULT_MESSAGE_INDEX_MAX_ENTRIES
    log_level: str = "INFO"
    auto_open_browser: bool = True
    default_onebot_ws_url: str = DEFAULT_ONEBOT_WS_URL
    default_onebot_access_token: str = ""
    default_reconnect_delay: float = DEFAULT_RECONNECT_DELAY
    default_max_reconnect_attempts: int = DEFAULT_MAX_RECONNECT_ATTEMPTS
    default_enable_subchannel_session_isolation: bool = DEFAULT_ENABLE_SUBCHANNEL_SESSION_ISOLATION
    default_remote_media_max_size: int = DEFAULT_REMOTE_MEDIA_MAX_SIZE
    default_skip_own_messages: bool = DEFAULT_SKIP_OWN_MESSAGES
    default_debug: bool = DEFAULT_DEBUG

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "ShellSettings":
        data = dict(payload or {})
        return cls(
            webui_host=str(data.get("webui_host", DEFAULT_WEBUI_HOST) or DEFAULT_WEBUI_HOST),
            webui_port=_coerce_int(data.get("webui_port", DEFAULT_WEBUI_PORT), DEFAULT_WEBUI_PORT),
            webui_access_password=str(
                data.get("webui_access_password", DEFAULT_WEBUI_ACCESS_PASSWORD)
                or DEFAULT_WEBUI_ACCESS_PASSWORD
            ),
            message_index_max_entries=_coerce_positive_int(
                data.get("message_index_max_entries", DEFAULT_MESSAGE_INDEX_MAX_ENTRIES),
                DEFAULT_MESSAGE_INDEX_MAX_ENTRIES,
            ),
            log_level=str(data.get("log_level", "INFO") or "INFO").upper(),
            auto_open_browser=_coerce_bool(data.get("auto_open_browser", True), True),
            default_onebot_ws_url=str(data.get("default_onebot_ws_url", DEFAULT_ONEBOT_WS_URL) or DEFAULT_ONEBOT_WS_URL),
            default_onebot_access_token=str(data.get("default_onebot_access_token", "") or ""),
            default_reconnect_delay=_coerce_float(data.get("default_reconnect_delay", DEFAULT_RECONNECT_DELAY), DEFAULT_RECONNECT_DELAY),
            default_max_reconnect_attempts=_coerce_int(
                data.get("default_max_reconnect_attempts", DEFAULT_MAX_RECONNECT_ATTEMPTS),
                DEFAULT_MAX_RECONNECT_ATTEMPTS,
            ),
            default_enable_subchannel_session_isolation=_coerce_bool(
                data.get(
                    "default_enable_subchannel_session_isolation",
                    DEFAULT_ENABLE_SUBCHANNEL_SESSION_ISOLATION,
                ),
                DEFAULT_ENABLE_SUBCHANNEL_SESSION_ISOLATION,
            ),
            default_remote_media_max_size=_coerce_int(
                data.get("default_remote_media_max_size", DEFAULT_REMOTE_MEDIA_MAX_SIZE),
                DEFAULT_REMOTE_MEDIA_MAX_SIZE,
            ),
            default_skip_own_messages=_coerce_bool(
                data.get("default_skip_own_messages", DEFAULT_SKIP_OWN_MESSAGES),
                DEFAULT_SKIP_OWN_MESSAGES,
            ),
            default_debug=_coerce_bool(data.get("default_debug", DEFAULT_DEBUG), DEFAULT_DEBUG),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "webui_host": self.webui_host,
            "webui_port": self.webui_port,
            "webui_access_password": self.webui_access_password,
            "message_index_max_entries": self.message_index_max_entries,
            "log_level": self.log_level,
            "auto_open_browser": self.auto_open_browser,
            "default_onebot_ws_url": self.default_onebot_ws_url,
            "default_onebot_access_token": self.default_onebot_access_token,
            "default_reconnect_delay": self.default_reconnect_delay,
            "default_max_reconnect_attempts": self.default_max_reconnect_attempts,
            "default_enable_subchannel_session_isolation": self.default_enable_subchannel_session_isolation,
            "default_remote_media_max_size": self.default_remote_media_max_size,
            "default_skip_own_messages": self.default_skip_own_messages,
            "default_debug": self.default_debug,
        }


@dataclass(slots=True)
class BotRecord:
    bot_id: str
    name: str
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

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any] | None,
        *,
        defaults: ShellSettings,
    ) -> "BotRecord":
        data = dict(payload or {})
        return cls(
            bot_id=str(data.get("id") or data.get("bot_id") or "").strip(),
            name=str(data.get("name") or data.get("display_name") or "").strip(),
            enabled=_coerce_bool(data.get("enabled", False), False),
            server_url=str(data.get("server_url", "") or "").strip().rstrip("/"),
            username=str(data.get("username", "") or "").strip(),
            password=str(data.get("password", "") or ""),
            e2ee_password=str(data.get("e2ee_password", "") or ""),
            onebot_ws_url=str(data.get("onebot_ws_url", defaults.default_onebot_ws_url) or defaults.default_onebot_ws_url).strip(),
            onebot_access_token=str(data.get("onebot_access_token", defaults.default_onebot_access_token) or defaults.default_onebot_access_token),
            onebot_self_id=0,
            reconnect_delay=_coerce_float(data.get("reconnect_delay", defaults.default_reconnect_delay), defaults.default_reconnect_delay),
            max_reconnect_attempts=_coerce_int(
                data.get("max_reconnect_attempts", defaults.default_max_reconnect_attempts),
                defaults.default_max_reconnect_attempts,
            ),
            enable_subchannel_session_isolation=_coerce_bool(
                data.get(
                    "enable_subchannel_session_isolation",
                    defaults.default_enable_subchannel_session_isolation,
                ),
                defaults.default_enable_subchannel_session_isolation,
            ),
            remote_media_max_size=_coerce_int(
                data.get("remote_media_max_size", defaults.default_remote_media_max_size),
                defaults.default_remote_media_max_size,
            ),
            room_info_cache_ttl_seconds=_coerce_float(
                data.get("room_info_cache_ttl_seconds", DEFAULT_ROOM_INFO_CACHE_TTL_SECONDS),
                DEFAULT_ROOM_INFO_CACHE_TTL_SECONDS,
            ),
            perf_trace_enabled=_coerce_bool(
                data.get("perf_trace_enabled", DEFAULT_PERF_TRACE_ENABLED),
                DEFAULT_PERF_TRACE_ENABLED,
            ),
            skip_own_messages=_coerce_bool(
                data.get("skip_own_messages", defaults.default_skip_own_messages),
                defaults.default_skip_own_messages,
            ),
            debug=_coerce_bool(data.get("debug", defaults.default_debug), defaults.default_debug),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.bot_id,
            "name": self.name,
            "enabled": self.enabled,
            "server_url": self.server_url,
            "username": self.username,
            "password": self.password,
            "e2ee_password": self.e2ee_password,
            "onebot_ws_url": self.onebot_ws_url,
            "onebot_access_token": self.onebot_access_token,
            "reconnect_delay": self.reconnect_delay,
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "enable_subchannel_session_isolation": self.enable_subchannel_session_isolation,
            "remote_media_max_size": self.remote_media_max_size,
            "room_info_cache_ttl_seconds": self.room_info_cache_ttl_seconds,
            "perf_trace_enabled": self.perf_trace_enabled,
            "skip_own_messages": self.skip_own_messages,
            "debug": self.debug,
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.bot_id:
            errors.append("bot id is required")
        if not self.name:
            errors.append("bot name is required")
        if self.enabled:
            if not self.server_url.startswith(("http://", "https://")):
                errors.append("enabled bot requires a valid Rocket.Chat server_url")
            if not self.onebot_ws_url.startswith(("ws://", "wss://")):
                errors.append("enabled bot requires a valid onebot_ws_url")
            if not self.username:
                errors.append("enabled bot requires username")
            if not self.password:
                errors.append("enabled bot requires password")
        if self.reconnect_delay < 0:
            errors.append("reconnect_delay must be >= 0")
        if self.max_reconnect_attempts < 0:
            errors.append("max_reconnect_attempts must be >= 0")
        if self.remote_media_max_size < 0:
            errors.append("remote_media_max_size must be >= 0")
        if self.room_info_cache_ttl_seconds < 0:
            errors.append("room_info_cache_ttl_seconds must be >= 0")
        return errors

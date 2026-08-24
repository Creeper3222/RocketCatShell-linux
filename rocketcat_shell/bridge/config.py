from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Mapping


DEFAULT_SERVER_URL = "http://127.0.0.1:3000"
DEFAULT_ONEBOT_WS_URL = "ws://127.0.0.1:6199/ws/"
DEFAULT_RECONNECT_DELAY = 5.0
DEFAULT_MAX_RECONNECT_ATTEMPTS = 10
DEFAULT_ENABLE_SUBCHANNEL_SESSION_ISOLATION = True
DEFAULT_REMOTE_MEDIA_MAX_SIZE = 20 * 1024 * 1024
DEFAULT_ROOM_INFO_CACHE_TTL_SECONDS = 300.0
DEFAULT_PERF_TRACE_ENABLED = False
DEFAULT_INBOUND_WORKER_COUNT = 0
DEFAULT_ONEBOT_OUTGOING_QUEUE_MAX_ENTRIES = 512
DEFAULT_IDENTITY_CACHE_MAX_ENTRIES = 4096
DEFAULT_MEDIA_CACHE_MAX_BYTES = 1024 * 1024 * 1024
DEFAULT_MEDIA_CACHE_MAX_AGE_HOURS = 168.0


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
    inbound_worker_count: int = DEFAULT_INBOUND_WORKER_COUNT
    onebot_outgoing_queue_max_entries: int = DEFAULT_ONEBOT_OUTGOING_QUEUE_MAX_ENTRIES
    identity_cache_max_entries: int = DEFAULT_IDENTITY_CACHE_MAX_ENTRIES
    media_cache_max_bytes: int = DEFAULT_MEDIA_CACHE_MAX_BYTES
    media_cache_max_age_hours: float = DEFAULT_MEDIA_CACHE_MAX_AGE_HOURS
    skip_own_messages: bool = True
    debug: bool = False
    bot_id: str = ""
    display_name: str = ""
    transport_type: str = "websocket-client"
    transport_label: str = "Websocket客户端"
    transport_settings: dict[str, Any] = field(default_factory=dict)
    transport_validation_error: str = ""

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "BridgeConfig":
        data = dict(payload or {})
        from .transports import (
            TransportValidationError,
            get_transport_spec,
            normalize_transport,
        )

        shell_defaults = SimpleNamespace(
            default_onebot_ws_url=str(
                data.get("onebot_ws_url", DEFAULT_ONEBOT_WS_URL)
                or DEFAULT_ONEBOT_WS_URL
            ),
            default_onebot_access_token=str(
                data.get("onebot_access_token", "") or ""
            ),
            default_skip_own_messages=_coerce_bool(
                data.get("skip_own_messages", True)
            ),
            default_debug=_coerce_bool(data.get("debug", False)),
        )
        transport_error = ""
        try:
            transport = normalize_transport(
                data.get("onebot_transport")
                if isinstance(data.get("onebot_transport"), Mapping)
                else None,
                shell_defaults=shell_defaults,
                legacy_payload=data,
            )
            transport_spec = get_transport_spec(transport["type"])
            transport_label = transport_spec.label
        except TransportValidationError as exc:
            transport_error = str(exc)
            raw_transport = data.get("onebot_transport")
            raw_transport = raw_transport if isinstance(raw_transport, Mapping) else {}
            raw_settings = raw_transport.get("settings")
            transport = {
                "type": str(raw_transport.get("type") or "websocket-client"),
                "settings": dict(raw_settings) if isinstance(raw_settings, Mapping) else {},
            }
            transport_label = str(transport["type"])
        transport_settings = dict(transport.get("settings") or {})
        transport_type = str(transport.get("type") or "websocket-client")
        legacy_ws_url = str(
            data.get("onebot_ws_url", DEFAULT_ONEBOT_WS_URL)
            or DEFAULT_ONEBOT_WS_URL
        ).strip()
        onebot_ws_url = (
            str(transport_settings.get("url") or legacy_ws_url).strip()
            if transport_type == "websocket-client"
            else legacy_ws_url
        )
        access_token = str(
            transport_settings.get("access_token")
            if "access_token" in transport_settings
            else data.get("onebot_access_token", "")
            or ""
        )
        skip_own_messages = (
            not _coerce_bool(transport_settings.get("report_self_message"))
            if "report_self_message" in transport_settings
            else _coerce_bool(data.get("skip_own_messages", True))
        )
        debug = _coerce_bool(
            transport_settings.get("debug", data.get("debug", False))
        )
        return cls(
            enabled=_coerce_bool(data.get("enabled", False)),
            server_url=str(data.get("server_url", DEFAULT_SERVER_URL) or DEFAULT_SERVER_URL).rstrip("/"),
            username=str(data.get("username", "") or ""),
            password=str(data.get("password", "") or ""),
            e2ee_password=str(data.get("e2ee_password", "") or ""),
            onebot_ws_url=onebot_ws_url,
            onebot_access_token=access_token,
            onebot_self_id=0,
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
            inbound_worker_count=max(
                0,
                _coerce_int(
                    data.get("inbound_worker_count", DEFAULT_INBOUND_WORKER_COUNT),
                    DEFAULT_INBOUND_WORKER_COUNT,
                ),
            ),
            onebot_outgoing_queue_max_entries=max(
                1,
                _coerce_int(
                    data.get(
                        "onebot_outgoing_queue_max_entries",
                        DEFAULT_ONEBOT_OUTGOING_QUEUE_MAX_ENTRIES,
                    ),
                    DEFAULT_ONEBOT_OUTGOING_QUEUE_MAX_ENTRIES,
                ),
            ),
            identity_cache_max_entries=max(
                128,
                _coerce_int(
                    data.get(
                        "identity_cache_max_entries",
                        DEFAULT_IDENTITY_CACHE_MAX_ENTRIES,
                    ),
                    DEFAULT_IDENTITY_CACHE_MAX_ENTRIES,
                ),
            ),
            media_cache_max_bytes=max(
                0,
                _coerce_int(
                    data.get("media_cache_max_bytes", DEFAULT_MEDIA_CACHE_MAX_BYTES),
                    DEFAULT_MEDIA_CACHE_MAX_BYTES,
                ),
            ),
            media_cache_max_age_hours=max(
                0.0,
                _coerce_float(
                    data.get(
                        "media_cache_max_age_hours",
                        DEFAULT_MEDIA_CACHE_MAX_AGE_HOURS,
                    ),
                    DEFAULT_MEDIA_CACHE_MAX_AGE_HOURS,
                ),
            ),
            skip_own_messages=skip_own_messages,
            debug=debug,
            bot_id=str(data.get("id") or data.get("bot_id") or "").strip(),
            display_name=str(data.get("name") or data.get("display_name") or "").strip(),
            transport_type=transport_type,
            transport_label=transport_label,
            transport_settings=transport_settings,
            transport_validation_error=transport_error,
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
            "reconnect_delay": self.reconnect_delay,
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "enable_subchannel_session_isolation": self.enable_subchannel_session_isolation,
            "remote_media_max_size": self.remote_media_max_size,
            "room_info_cache_ttl_seconds": self.room_info_cache_ttl_seconds,
            "perf_trace_enabled": self.perf_trace_enabled,
            "inbound_worker_count": self.inbound_worker_count,
            "onebot_outgoing_queue_max_entries": self.onebot_outgoing_queue_max_entries,
            "identity_cache_max_entries": self.identity_cache_max_entries,
            "media_cache_max_bytes": self.media_cache_max_bytes,
            "media_cache_max_age_hours": self.media_cache_max_age_hours,
            "skip_own_messages": self.skip_own_messages,
            "debug": self.debug,
            "id": self.bot_id,
            "name": self.display_name,
            "type": self.transport_type,
            "onebot_transport": {
                "type": self.transport_type,
                "settings": dict(self.transport_settings),
            },
        }

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.transport_validation_error:
            errors.append(self.transport_validation_error)
        if self.enabled:
            if not self.server_url.startswith(("http://", "https://")):
                errors.append("server_url 必须以 http:// 或 https:// 开头")
            if (
                self.transport_type == "websocket-client"
                and not self.onebot_ws_url.startswith(("ws://", "wss://"))
            ):
                errors.append("onebot_ws_url 必须以 ws:// 或 wss:// 开头")
            if not self.username:
                errors.append("enabled=true 时 username 不能为空")
            if not self.password:
                errors.append("enabled=true 时 password 不能为空")
        if self.reconnect_delay < 0:
            errors.append("reconnect_delay 不能小于 0")
        if self.max_reconnect_attempts < 0:
            errors.append("max_reconnect_attempts 不能小于 0")
        if self.remote_media_max_size < 0:
            errors.append("remote_media_max_size 不能小于 0")
        if self.room_info_cache_ttl_seconds < 0:
            errors.append("room_info_cache_ttl_seconds 不能小于 0")
        return errors

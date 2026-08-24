from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable

from .codec import OneBotMessageCodec


ActionHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class OneBotTransport(ABC):
    def __init__(self, config: Any, action_handler: ActionHandler):
        self.config = config
        self.action_handler = action_handler
        settings = dict(getattr(config, "transport_settings", {}) or {})
        self.settings = settings
        self.codec = OneBotMessageCodec(
            str(settings.get("message_post_format") or "array")
        )
        self._running = False
        self._last_error = ""

    @property
    @abstractmethod
    def connected(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def emit_event(self, payload: dict[str, Any]) -> None:
        raise NotImplementedError

    @abstractmethod
    def build_diagnostic_snapshot(self) -> dict[str, Any]:
        raise NotImplementedError

    def _base_diagnostics(self, *, status: str, endpoint: str) -> dict[str, Any]:
        return {
            "onebot_transport_type": str(
                getattr(self.config, "transport_type", "websocket-client")
            ),
            "onebot_transport_label": str(
                getattr(self.config, "transport_label", "Websocket客户端")
            ),
            "onebot_transport_status": status,
            "onebot_transport_endpoint": endpoint,
            "onebot_connected": self.connected,
            "onebot_last_error": self._last_error,
        }


def lifecycle_event(self_id: int) -> dict[str, Any]:
    import time

    return {
        "time": int(time.time()),
        "self_id": int(self_id or 0),
        "post_type": "meta_event",
        "meta_event_type": "lifecycle",
        "sub_type": "connect",
    }


def heartbeat_event(self_id: int, interval_ms: int) -> dict[str, Any]:
    import time

    return {
        "time": int(time.time()),
        "self_id": int(self_id or 0),
        "post_type": "meta_event",
        "meta_event_type": "heartbeat",
        "status": {"online": True, "good": True},
        "interval": max(0, int(interval_ms or 0)),
    }

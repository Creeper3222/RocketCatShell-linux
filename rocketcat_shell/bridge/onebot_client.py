from __future__ import annotations

from typing import Any

from .transports.websocket_client import WebsocketClientTransport


_ONEBOT_UPSTREAM_RETRY_DELAY_SECONDS = 5.0


class OneBotReverseWsClient(WebsocketClientTransport):
    """Compatibility facade for the v0.2.2 reverse WebSocket client."""

    def __init__(self, config: Any, action_handler: Any):
        if not getattr(config, "transport_settings", None):
            config.transport_type = "websocket-client"
            config.transport_label = "Websocket客户端"
            config.transport_settings = {
                "url": str(getattr(config, "onebot_ws_url", "") or ""),
                "message_post_format": "array",
                "report_self_message": not bool(
                    getattr(config, "skip_own_messages", True)
                ),
                "reconnect_interval_ms": 5000,
                "access_token": str(
                    getattr(config, "onebot_access_token", "") or ""
                ),
                "debug": bool(getattr(config, "debug", False)),
                "heartbeat_interval_ms": 30000,
            }
        super().__init__(config, action_handler)


__all__ = ["OneBotReverseWsClient", "_ONEBOT_UPSTREAM_RETRY_DELAY_SECONDS"]

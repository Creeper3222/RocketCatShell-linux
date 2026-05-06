from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from rocketcat_shell.bridge.config import BridgeConfig
    from rocketcat_shell.bridge.id_map import DurableIdMap
    from rocketcat_shell.bridge.rocketchat_client import RocketChatClient
    from rocketcat_shell.bridge.storage import ContextRoomStore, MessageStore, PrivateRoomStore
    from rocketcat_shell.bridge.translator_inbound import InboundTranslator
    from rocketcat_shell.bridge.translator_outbound import OutboundMessageTranslator


@dataclass(slots=True)
class PluginContext:
    plugin_id: str
    plugin_dir: Path
    data_dir: Path
    config_path: Path
    metadata: dict[str, Any]


@dataclass(slots=True)
class PluginExecutionContext:
    instance_name: str
    bridge_config: BridgeConfig
    rocketchat: RocketChatClient
    id_map: DurableIdMap
    messages: MessageStore
    private_rooms: PrivateRoomStore
    context_rooms: ContextRoomStore
    inbound: InboundTranslator
    outbound: OutboundMessageTranslator

    async def resolve_message_source_id(self, message_id: int | str | None) -> str | None:
        if message_id is None:
            return None
        entry = await self.messages.get_by_surrogate(message_id)
        if isinstance(entry, dict) and entry.get("source_id"):
            return str(entry["source_id"])
        resolved = await self.id_map.get_source("message", message_id)
        if resolved:
            return str(resolved)
        return None

    async def resolve_user_source_id(self, user_id: int | str | None) -> str | None:
        if user_id is None:
            return None
        if str(user_id) == str(self.bridge_config.onebot_self_id):
            return self.rocketchat.user_id
        return await self.id_map.get_source("user", user_id)

    def ok(self, data: Any = None) -> dict[str, Any]:
        return {"status": "ok", "retcode": 0, "data": data, "wording": ""}

    def failed(self, wording: str, retcode: int = 1400) -> dict[str, Any]:
        return {"status": "failed", "retcode": retcode, "data": None, "wording": wording}


class RocketCatPlugin:
    def __init__(self, context: PluginContext, config: dict[str, Any]):
        self.context = context
        self.config = dict(config)

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    async def on_load(self, runtime: PluginExecutionContext) -> None:
        return None

    async def on_unload(self, runtime: PluginExecutionContext) -> None:
        return None

    async def handle_onebot_action(
        self,
        action: str,
        params: dict[str, Any],
        runtime: PluginExecutionContext,
    ) -> dict[str, Any] | None:
        return None
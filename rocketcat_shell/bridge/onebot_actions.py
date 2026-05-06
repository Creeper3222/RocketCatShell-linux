from __future__ import annotations

from typing import Any, Awaitable, Callable

from .config import BridgeConfig
from .id_map import DurableIdMap
from .rocketchat_client import RocketChatClient
from .storage import ContextRoomStore, MessageStore, PrivateRoomStore
from .translator_inbound import InboundTranslator
from .translator_outbound import OutboundMessageTranslator


def _ok(data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "retcode": 0, "data": data, "wording": ""}


def _failed(wording: str, retcode: int = 1400) -> dict[str, Any]:
    return {"status": "failed", "retcode": retcode, "data": None, "wording": wording}


PluginActionDispatcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any] | None]]


class OneBotActionHandler:
    def __init__(
        self,
        config: BridgeConfig,
        rocketchat: RocketChatClient,
        id_map: DurableIdMap,
        messages: MessageStore,
        private_rooms: PrivateRoomStore,
        context_rooms: ContextRoomStore,
        inbound: InboundTranslator,
        outbound: OutboundMessageTranslator,
        plugin_action_dispatcher: PluginActionDispatcher | None = None,
    ):
        self._config = config
        self._rocketchat = rocketchat
        self._id_map = id_map
        self._messages = messages
        self._private_rooms = private_rooms
        self._context_rooms = context_rooms
        self._inbound = inbound
        self._outbound = outbound
        self._plugin_action_dispatcher = plugin_action_dispatcher

    async def handle(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if action == "send_group_msg":
                return await self._handle_send_group_msg(params)
            if action == "send_private_msg":
                return await self._handle_send_private_msg(params)
            if action == "send_msg":
                return await self._handle_send_msg(params)
            if action == "get_msg":
                return await self._handle_get_msg(params)
            if action == "get_group_info":
                return await self._handle_get_group_info(params)
            if action == "get_group_member_info":
                return await self._handle_get_group_member_info(params)
            if action == "get_group_member_list":
                return await self._handle_get_group_member_list(params)
            if action == "get_stranger_info":
                return await self._handle_get_stranger_info(params)
            plugin_result = await self._dispatch_plugin_action(action, params)
            if plugin_result is not None:
                return plugin_result
            if action == "set_msg_emoji_like":
                return _failed("当前没有启用可处理 set_msg_emoji_like 的 RocketCat 插件", retcode=1404)
            if action == "get_login_info":
                return _ok({"user_id": self._config.onebot_self_id, "nickname": self._rocketchat.bot_username or self._config.username})
            if action in {"send_group_forward_msg", "send_private_forward_msg"}:
                return _failed("v1 暂不支持合并转发消息")
            return _failed(f"未实现的 OneBot 动作: {action}", retcode=1404)
        except Exception as exc:
            return _failed(str(exc), retcode=1500)

    async def _dispatch_plugin_action(
        self,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self._plugin_action_dispatcher is None:
            return None
        return await self._plugin_action_dispatcher(action, params)

    async def _handle_send_group_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        outbound = await self._outbound.translate(
            params.get("message"),
            group_id=params.get("group_id"),
        )
        return await self._send_outbound(outbound)

    async def _handle_send_private_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        outbound = await self._outbound.translate(
            params.get("message"),
            user_id=params.get("user_id"),
        )
        return await self._send_outbound(outbound)

    async def _handle_send_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        message_type = params.get("message_type")
        if message_type == "group" or params.get("group_id") is not None:
            return await self._handle_send_group_msg(params)
        return await self._handle_send_private_msg(params)

    async def _send_outbound(self, outbound: dict[str, Any]) -> dict[str, Any]:
        segments = outbound.get("segments") or []
        reply_source_id = outbound.get("reply_source_id")
        room_id = str(outbound["room_id"])
        if not segments and not reply_source_id:
            raise ValueError("当前消息为空，无法发送")

        raw_messages = await self._rocketchat.send_message_segments(
            room_id,
            segments,
            reply_source_id=reply_source_id,
            mention_usernames=outbound.get("mention_usernames") or [],
            reply_mention_username=outbound.get("reply_mention_username") or None,
        )
        if not raw_messages:
            raise RuntimeError("Rocket.Chat 未返回已发送消息")

        last_message_id: int | None = None
        for raw_message in raw_messages:
            event = await self._inbound.translate(raw_message)
            if event is not None:
                last_message_id = int(event["message_id"])
                continue
            source_id = str(raw_message.get("_id") or "")
            if source_id:
                mapping = await self._id_map.get_or_create("message", source_id)
                last_message_id = mapping.surrogate_id
                continue

            echoed_raw_message = await self._rocketchat.await_sent_message_echo(room_id)
            if not echoed_raw_message:
                continue

            echoed_event = await self._inbound.translate(echoed_raw_message)
            if echoed_event is not None:
                last_message_id = int(echoed_event["message_id"])
                continue

            echoed_source_id = str(echoed_raw_message.get("_id") or "")
            if echoed_source_id:
                mapping = await self._id_map.get_or_create("message", echoed_source_id)
                last_message_id = mapping.surrogate_id

        if last_message_id is None:
            raise RuntimeError("未能为已发送消息建立映射")
        return _ok({"message_id": last_message_id})

    async def _handle_get_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        event = await self._inbound.hydrate(params.get("message_id"))
        if not event:
            return _failed(f"找不到消息: {params.get('message_id')}", retcode=1404)
        return _ok(event)

    async def _handle_get_group_info(self, params: dict[str, Any]) -> dict[str, Any]:
        group_id = params.get("group_id")
        room_source_id = await self._resolve_group_room_source(group_id)
        if not room_source_id:
            return _failed(f"未知 group_id: {group_id}", retcode=1404)
        room_info = await self._rocketchat.get_room_info(room_source_id)
        members = await self._rocketchat.get_room_members(room_source_id)
        return _ok(
            {
                "group_id": int(group_id),
                "group_name": room_info.get("fname") or room_info.get("name") or room_source_id,
                "member_count": len(members),
                "max_member_count": 0,
            }
        )

    async def _handle_get_group_member_info(self, params: dict[str, Any]) -> dict[str, Any]:
        group_id = params.get("group_id")
        user_id = params.get("user_id")
        room_source_id = await self._resolve_group_room_source(group_id)
        user_source_id = await self._resolve_user_source_id(user_id)
        if not room_source_id or not user_source_id:
            return _failed("未知 group_id 或 user_id", retcode=1404)
        member = await self._resolve_member(room_source_id, user_source_id, group_id=group_id)
        return _ok(member)

    async def _handle_get_group_member_list(self, params: dict[str, Any]) -> dict[str, Any]:
        group_id = params.get("group_id")
        room_source_id = await self._resolve_group_room_source(group_id)
        if not room_source_id:
            return _failed(f"未知 group_id: {group_id}", retcode=1404)
        members = await self._rocketchat.get_room_members(room_source_id)
        payload: list[dict[str, Any]] = []
        for member in members:
            member_id = member.get("_id")
            if not member_id:
                continue
            payload.append(
                await self._resolve_member(
                    room_source_id,
                    str(member_id),
                    group_id=group_id,
                    cached=member,
                )
            )
        return _ok(payload)

    async def _resolve_group_room_source(self, group_id: int | str | None) -> str | None:
        if group_id is None:
            return None
        room_source_id = await self._id_map.get_source("room", group_id)
        if room_source_id:
            return room_source_id
        context_entry = await self._context_rooms.get_by_context_surrogate(group_id)
        if context_entry and context_entry.get("room_source_id"):
            return str(context_entry["room_source_id"])
        return None

    async def _handle_get_stranger_info(self, params: dict[str, Any]) -> dict[str, Any]:
        user_id = params.get("user_id")
        user_source_id = await self._resolve_user_source_id(user_id)
        if not user_source_id:
            return _failed(f"未知 user_id: {user_id}", retcode=1404)
        user_info = await self._rocketchat.get_user_info(user_source_id)
        if str(user_id) == str(self._config.onebot_self_id):
            resolved_user_id = self._config.onebot_self_id
        else:
            resolved_user_id = (await self._id_map.get_or_create("user", user_source_id)).surrogate_id
        return _ok(
            {
                "user_id": resolved_user_id,
                "nickname": user_info.get("name") or user_info.get("username") or user_source_id,
                "remark": user_info.get("username") or "",
                "sex": "unknown",
                "age": 0,
            }
        )

    async def _resolve_user_source_id(self, user_id: int | str | None) -> str | None:
        if user_id is None:
            return None
        if str(user_id) == str(self._config.onebot_self_id):
            return self._rocketchat.user_id
        return await self._id_map.get_source("user", user_id)

    async def _resolve_message_source_id(self, message_id: int | str | None) -> str | None:
        if message_id is None:
            return None
        entry = await self._messages.get_by_surrogate(message_id)
        if isinstance(entry, dict) and entry.get("source_id"):
            return str(entry["source_id"])
        resolved = await self._id_map.get_source("message", message_id)
        if resolved:
            return str(resolved)
        return None

    async def _resolve_member(
        self,
        room_source_id: str,
        user_source_id: str,
        *,
        group_id: int | str | None = None,
        cached: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        user_info = cached or await self._rocketchat.get_user_info(user_source_id)
        mapping = await self._id_map.get_or_create("user", user_source_id)
        role = self._pick_member_role(user_info)
        reported_group_id = group_id
        if reported_group_id is None:
            reported_group_id = (await self._id_map.get_or_create("room", room_source_id)).surrogate_id
        return {
            "group_id": int(reported_group_id),
            "user_id": mapping.surrogate_id,
            "nickname": user_info.get("name") or user_info.get("username") or user_source_id,
            "card": user_info.get("name") or user_info.get("username") or user_source_id,
            "sex": "unknown",
            "age": 0,
            "area": "",
            "join_time": 0,
            "last_sent_time": 0,
            "level": "0",
            "role": role,
            "unfriendly": False,
            "title": "",
            "title_expire_time": 0,
            "card_changeable": False,
        }

    def _pick_member_role(self, user_info: dict[str, Any]) -> str:
        roles = user_info.get("roles")
        if isinstance(roles, list):
            if "owner" in roles:
                return "owner"
            if "admin" in roles:
                return "admin"
        return "member"
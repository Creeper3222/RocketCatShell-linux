from __future__ import annotations

import asyncio
import weakref
from collections import Counter
from typing import Any, Awaitable, Callable

from rocketcat_shell.logger import logger

from .config import BridgeConfig
from .forward_messages import (
    is_forward_candidate,
    parse_forward_nodes,
    validate_forward_segments,
)
from .id_map import DurableIdMap
from .rocketchat_client import RocketChatClient
from .storage import ContextRoomStore, MessageStore, PrivateRoomStore
from .translator_inbound import InboundTranslator
from .translator_outbound import OutboundMessageTranslator
from .transports.codec import OneBotMessageCodec


def _ok(data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "retcode": 0, "data": data, "wording": ""}


def _failed(wording: str, retcode: int = 1400) -> dict[str, Any]:
    return {"status": "failed", "retcode": retcode, "data": None, "wording": wording}


def _describe_mapping_candidate(raw_message: Any) -> str:
    if not isinstance(raw_message, dict):
        return repr(raw_message)

    return (
        f"_id={str(raw_message.get('_id') or '-')} "
        f"rid={str(raw_message.get('rid') or '-')} "
        f"tmid={str(raw_message.get('tmid') or '-')} "
        f"upload_file_id={str(raw_message.get('_upload_file_id') or '-')} "
        f"keys={','.join(sorted(str(key) for key in raw_message.keys())) or '-'}"
    )


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
        self._forward_progress: weakref.WeakKeyDictionary[
            asyncio.Task[Any], dict[str, Any]
        ] = weakref.WeakKeyDictionary()
        self._forward_timeout_progress: weakref.WeakKeyDictionary[
            asyncio.Task[Any], dict[str, Any]
        ] = weakref.WeakKeyDictionary()

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
            if action == "get_forward_msg":
                return _failed("当前版本暂不支持合并转发消息", retcode=1404)
            if action in {"send_group_forward_msg", "send_private_forward_msg", "send_forward_msg"}:
                return await self._handle_forward_action(action, params)
            if action == "get_stranger_info":
                return await self._handle_get_stranger_info(params)
            plugin_result = await self._dispatch_plugin_action(action, params)
            if plugin_result is not None:
                return plugin_result
            if action == "set_msg_emoji_like":
                return _failed("当前没有启用可处理 set_msg_emoji_like 的 RocketCat 插件", retcode=1404)
            if action == "get_login_info":
                return _ok({"user_id": self._config.onebot_self_id, "nickname": self._rocketchat.bot_username or self._config.username})
            return _failed(f"未实现的 OneBot 动作: {action}", retcode=1404)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            progress = self._forward_progress.get(task) if task is not None else None
            if task is not None and progress is not None:
                self._forward_timeout_progress[task] = dict(progress)
            raise
        except Exception as exc:
            return _failed(str(exc), retcode=1500)

    def take_timeout_response(
        self,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        del action, params
        task = asyncio.current_task()
        progress = self._forward_timeout_progress.pop(task, None) if task is not None else None
        if progress is None:
            return None
        if progress.get("mode") == "thread":
            header_state = "已创建" if progress.get("thread_header_source_id") else "未确认"
            return _failed(
                "合并转发线程发送超时（"
                f"阶段 {progress.get('phase', '未知')}，线程头{header_state}，"
                f"第 {progress.get('current_item', 1)} 条正文，"
                f"已发送 {progress.get('sent_count', 0)} 条）",
                retcode=1504,
            )
        return _failed(
            "合并转发发送超时（"
            f"第 {progress.get('current_item', 1)} 条消息，"
            f"已发送 {progress.get('sent_count', 0)} 条）",
            retcode=1504,
        )

    async def _dispatch_plugin_action(
        self,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self._plugin_action_dispatcher is None:
            return None
        return await self._plugin_action_dispatcher(action, params)

    async def _handle_send_group_msg(
        self,
        params: dict[str, Any],
        *,
        source_action: str = "send_group_msg",
    ) -> dict[str, Any]:
        if is_forward_candidate(params.get("message")):
            return await self._handle_forward_send(
                params,
                source_action=source_action,
                group_id=params.get("group_id"),
            )
        outbound = await self._outbound.translate(
            params.get("message"),
            group_id=params.get("group_id"),
        )
        return await self._send_outbound(outbound)

    async def _handle_send_private_msg(
        self,
        params: dict[str, Any],
        *,
        source_action: str = "send_private_msg",
    ) -> dict[str, Any]:
        if is_forward_candidate(params.get("message")):
            return await self._handle_forward_send(
                params,
                source_action=source_action,
                user_id=params.get("user_id"),
            )
        outbound = await self._outbound.translate(
            params.get("message"),
            user_id=params.get("user_id"),
        )
        return await self._send_outbound(outbound)

    async def _handle_send_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        message_type = params.get("message_type")
        if message_type == "group" or params.get("group_id") is not None:
            return await self._handle_send_group_msg(params, source_action="send_msg")
        return await self._handle_send_private_msg(params, source_action="send_msg")

    async def _send_outbound(self, outbound: dict[str, Any]) -> dict[str, Any]:
        segments = outbound.get("segments") or []
        reply_source_id = outbound.get("reply_source_id")
        thread_source_id = str(outbound.get("thread_source_id") or "").strip() or None
        room_id = str(outbound["room_id"])
        if not segments and not reply_source_id:
            raise ValueError("当前消息为空，无法发送")

        raw_messages = await self._rocketchat.send_message_segments(
            room_id,
            segments,
            thread_source_id=thread_source_id,
            reply_source_id=reply_source_id,
            mention_usernames=outbound.get("mention_usernames") or [],
            reply_mention_username=outbound.get("reply_mention_username") or None,
        )
        if not raw_messages:
            raise RuntimeError("Rocket.Chat 未返回已发送消息")

        last_message_id: int | None = None
        for raw_message in raw_messages:
            mapped_message_id = await self._map_sent_message(
                room_id,
                raw_message,
                requested_thread_source_id=thread_source_id,
            )
            if mapped_message_id is not None:
                last_message_id = mapped_message_id
                continue
            logger.warning(
                "[RocketChatOneBotBridge] 已发送消息未直接返回可映射 source_id，且等待自回显超时: room_id=%s candidate=%s",
                room_id,
                _describe_mapping_candidate(raw_message),
            )

        if last_message_id is None:
            logger.error(
                "[RocketChatOneBotBridge] 未能为已发送消息建立映射: room_id=%s candidates=%s",
                room_id,
                " | ".join(_describe_mapping_candidate(raw_message) for raw_message in raw_messages) or "-",
            )
            raise RuntimeError("未能为已发送消息建立映射")
        return _ok({"message_id": last_message_id})

    async def _map_sent_message(
        self,
        room_id: str,
        raw_message: Any,
        *,
        requested_thread_source_id: str | None,
    ) -> int | None:
        if self._should_prefer_echo_for_thread(
            raw_message,
            requested_thread_source_id=requested_thread_source_id,
        ):
            echoed_raw_message = await self._rocketchat.await_sent_message_echo(room_id)
            if echoed_raw_message:
                raw_message = echoed_raw_message
            elif isinstance(raw_message, dict):
                raw_message = dict(raw_message)
                raw_message["tmid"] = requested_thread_source_id

        event = await self._inbound.translate(raw_message)
        if event is not None:
            return int(event["message_id"])
        source_id = str(raw_message.get("_id") or "") if isinstance(raw_message, dict) else ""
        if source_id:
            mapping = await self._id_map.get_or_create("message", source_id)
            return mapping.surrogate_id

        echoed_raw_message = await self._rocketchat.await_sent_message_echo(room_id)
        if not echoed_raw_message:
            return None
        echoed_event = await self._inbound.translate(echoed_raw_message)
        if echoed_event is not None:
            return int(echoed_event["message_id"])
        echoed_source_id = str(echoed_raw_message.get("_id") or "")
        if echoed_source_id:
            mapping = await self._id_map.get_or_create("message", echoed_source_id)
            return mapping.surrogate_id
        return None

    def _should_prefer_echo_for_thread(
        self,
        raw_message: Any,
        *,
        requested_thread_source_id: str | None,
    ) -> bool:
        if not requested_thread_source_id or not isinstance(raw_message, dict):
            return False
        if str(raw_message.get("tmid") or "").strip():
            return False
        return True

    async def _handle_forward_action(
        self,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if action == "send_group_forward_msg":
            return await self._handle_forward_send(
                params,
                source_action=action,
                group_id=params.get("group_id"),
            )
        if action == "send_private_forward_msg":
            return await self._handle_forward_send(
                params,
                source_action=action,
                user_id=params.get("user_id"),
            )
        if params.get("message_type") == "group" or params.get("group_id") is not None:
            return await self._handle_forward_send(
                params,
                source_action=action,
                group_id=params.get("group_id"),
            )
        return await self._handle_forward_send(
            params,
            source_action=action,
            user_id=params.get("user_id"),
        )

    async def _handle_forward_send(
        self,
        params: dict[str, Any],
        *,
        source_action: str,
        group_id: int | str | None = None,
        user_id: int | str | None = None,
    ) -> dict[str, Any]:
        target_type = "group" if group_id is not None else "private" if user_id is not None else "unknown"
        target_id = group_id if group_id is not None else user_id
        thread_mode = bool(getattr(self._config, "forward_messages_to_thread", False))
        bot_id = str(getattr(self._config, "bot_id", "") or "-")
        log_context = (
            f"action={source_action} bot_id={bot_id} "
            f"target={target_type}:{target_id if target_id is not None else '-'}"
        )
        payload_field = "messages" if params.get("messages") is not None else "message"
        task = asyncio.current_task()
        progress: dict[str, Any] = {
            "mode": "thread" if thread_mode else "sequential",
            "phase": "解析",
            "current_item": 1,
            "total_items": 0,
            "sent_count": 0,
            "last_message_id": None,
            "thread_header_source_id": None,
        }
        if task is not None:
            self._forward_progress[task] = progress

        logger.info(
            "[RocketCatShell][OneBot][Forward] 检测到 OneBot 合并转发消息块: %s mode=%s payload_field=%s",
            log_context,
            progress["mode"],
            payload_field,
        )

        try:
            payload = params.get("messages")
            if payload is None:
                payload = params.get("message")
            items = parse_forward_nodes(payload)
            top_level_nodes = len({item.node_index for item in items})
            reference_items = sum(1 for item in items if item.reference_id)
            progress["total_items"] = len(items)
            logger.info(
                "[RocketCatShell][OneBot][Forward] 合并转发解析完成: %s nodes=%d expanded_items=%d reference_items=%d",
                log_context,
                top_level_nodes,
                len(items),
                reference_items,
            )

            progress["phase"] = "发送前校验"
            destination_outbound = await self._outbound.translate(
                [],
                group_id=group_id,
                user_id=user_id,
            )
            destination = {
                "room_id": destination_outbound.get("room_id"),
                "thread_source_id": destination_outbound.get("thread_source_id"),
            }

            prepared: list[tuple[int, dict[str, Any], str]] = []
            segment_type_counts: Counter[str] = Counter()
            for item_position, item in enumerate(items, start=1):
                progress["current_item"] = item_position
                segments = item.segments
                if item.reference_id:
                    event = await self._inbound.hydrate(item.reference_id)
                    if not isinstance(event, dict):
                        raise ValueError(f"无法解析合并转发引用消息: {item.reference_id}")
                    segments = event.get("message")
                    if isinstance(segments, str):
                        segments = OneBotMessageCodec.cq_to_segments(segments)
                    segments = validate_forward_segments(segments, item.node_index)
                    logger.info(
                        "[RocketCatShell][OneBot][Forward] 引用节点解析完成: %s item=%d/%d node=%d reference_id=%s segment_types=%s",
                        log_context,
                        item_position,
                        len(items),
                        item.node_index,
                        item.reference_id,
                        ",".join(str(segment.get("type") or "text") for segment in segments),
                    )
                if segments is None:
                    raise ValueError(f"第 {item.node_index} 个 node 内容无效")
                segment_type_counts.update(
                    str(segment.get("type") or "text") for segment in segments
                )

                outbound = await self._outbound.translate(
                    segments,
                    group_id=group_id,
                    user_id=user_id,
                    fixed_destination=destination,
                    require_reply_reference=True,
                )
                if not outbound.get("segments") and not outbound.get("reply_source_id"):
                    raise ValueError(f"合并转发第 {item_position} 条消息没有可发送内容")
                item_segment_types = ",".join(
                    str(segment.get("type") or "text") for segment in segments
                ) or "-"
                prepared.append((item.node_index, outbound, item_segment_types))

            if not prepared:
                raise ValueError("合并转发消息列表不能为空")

            room_id = str(destination["room_id"])
            thread_source_id = (
                str(destination.get("thread_source_id") or "").strip() or None
            )
            progress["phase"] = "逐条发送"
            segment_summary = ",".join(
                f"{segment_type}:{count}"
                for segment_type, count in sorted(segment_type_counts.items())
            ) or "-"
            logger.info(
                "[RocketCatShell][OneBot][Forward] 合并转发预检通过，开始发送: %s mode=%s room_id=%s thread_context=%s nodes=%d expanded_items=%d segment_types=%s",
                log_context,
                progress["mode"],
                room_id,
                thread_source_id or "none",
                top_level_nodes,
                len(prepared),
                segment_summary,
            )
            if thread_mode:
                return await self._send_forward_in_thread(
                    prepared=prepared,
                    room_id=room_id,
                    top_level_nodes=top_level_nodes,
                    original_thread_source_id=thread_source_id,
                    log_context=log_context,
                    progress=progress,
                )
            for item_position, (node_index, outbound, item_segment_types) in enumerate(prepared, start=1):
                progress["current_item"] = item_position
                item_sent_count = 0
                logger.info(
                    "[RocketCatShell][OneBot][Forward] 开始发送合并转发节点: %s item=%d/%d node=%d segment_types=%s",
                    log_context,
                    item_position,
                    len(prepared),
                    node_index,
                    item_segment_types,
                )

                async def map_progress(raw_message: dict[str, Any]) -> None:
                    nonlocal item_sent_count
                    progress["sent_count"] += 1
                    mapped_id = await self._map_sent_message(
                        room_id,
                        raw_message,
                        requested_thread_source_id=thread_source_id,
                    )
                    if mapped_id is None:
                        logger.error(
                            "[RocketCatShell][OneBot][Forward] 消息已发送但无法建立 OneBot 映射: %s item=%d/%d node=%d delivered_count=%d",
                            log_context,
                            item_position,
                            len(prepared),
                            node_index,
                            progress["sent_count"],
                        )
                        raise RuntimeError("无法为已发送消息建立 OneBot message_id")
                    progress["last_message_id"] = mapped_id
                    item_sent_count += 1
                    logger.info(
                        "[RocketCatShell][OneBot][Forward] 合并转发消息已发送: %s item=%d/%d node=%d item_message=%d batch_sent=%d message_id=%d",
                        log_context,
                        item_position,
                        len(prepared),
                        node_index,
                        item_sent_count,
                        progress["sent_count"],
                        mapped_id,
                    )

                try:
                    await self._rocketchat.send_message_segments(
                        room_id,
                        outbound.get("segments") or [],
                        thread_source_id=thread_source_id,
                        reply_source_id=outbound.get("reply_source_id"),
                        mention_usernames=outbound.get("mention_usernames") or [],
                        reply_mention_username=outbound.get("reply_mention_username") or None,
                        strict_delivery=True,
                        on_message=map_progress,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "[RocketCatShell][OneBot][Forward] 合并转发发送失败并已停止后续节点: %s item=%d/%d node=%d batch_sent=%d error_type=%s error=%s",
                        log_context,
                        item_position,
                        len(prepared),
                        node_index,
                        progress["sent_count"],
                        type(exc).__name__,
                        str(exc)[:300],
                    )
                    return _failed(
                        "合并转发发送失败（"
                        f"第 {item_position} 条消息，来源 node {node_index}，"
                        f"已发送 {progress['sent_count']} 条）: {exc}",
                        retcode=1500,
                    )
                logger.info(
                    "[RocketCatShell][OneBot][Forward] 合并转发节点发送完成: %s item=%d/%d node=%d item_sent=%d batch_sent=%d",
                    log_context,
                    item_position,
                    len(prepared),
                    node_index,
                    item_sent_count,
                    progress["sent_count"],
                )

            last_message_id = progress.get("last_message_id")
            if last_message_id is None:
                logger.error(
                    "[RocketCatShell][OneBot][Forward] 合并转发未产生可映射消息: %s items=%d",
                    log_context,
                    len(prepared),
                )
                return _failed("合并转发没有产生可映射的已发送消息", retcode=1500)
            logger.info(
                "[RocketCatShell][OneBot][Forward] 合并转发发送完成: %s items=%d batch_sent=%d last_message_id=%d",
                log_context,
                len(prepared),
                progress["sent_count"],
                int(last_message_id),
            )
            return _ok({"message_id": int(last_message_id)})
        except asyncio.CancelledError:
            logger.warning(
                "[RocketCatShell][OneBot][Forward] 合并转发执行被取消或超时: %s phase=%s item=%s/%s batch_sent=%s",
                log_context,
                progress.get("phase", "解析"),
                progress.get("current_item", 1),
                progress.get("total_items", 0),
                progress.get("sent_count", 0),
            )
            if task is not None:
                self._forward_timeout_progress[task] = dict(progress)
            raise
        except (TypeError, ValueError) as exc:
            logger.warning(
                "[RocketCatShell][OneBot][Forward] 合并转发解析或预检失败: %s phase=%s item=%s/%s batch_sent=%s error_type=%s error=%s",
                log_context,
                progress.get("phase", "解析"),
                progress.get("current_item", 1),
                progress.get("total_items", 0),
                progress.get("sent_count", 0),
                type(exc).__name__,
                str(exc)[:300],
            )
            return _failed(str(exc), retcode=1400)
        except Exception:
            logger.exception(
                "[RocketCatShell][OneBot][Forward] 合并转发处理发生未预期错误: %s phase=%s item=%s/%s batch_sent=%s",
                log_context,
                progress.get("phase", "解析"),
                progress.get("current_item", 1),
                progress.get("total_items", 0),
                progress.get("sent_count", 0),
            )
            raise
        finally:
            if task is not None:
                self._forward_progress.pop(task, None)

    async def _send_forward_in_thread(
        self,
        *,
        prepared: list[tuple[int, dict[str, Any], str]],
        room_id: str,
        top_level_nodes: int,
        original_thread_source_id: str | None,
        log_context: str,
        progress: dict[str, Any],
    ) -> dict[str, Any]:
        progress["phase"] = "确认 Rocket.Chat 线程功能"
        try:
            threads_enabled = await self._rocketchat.are_threads_enabled()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[RocketCatShell][OneBot][Forward] 无法确认 Rocket.Chat 线程功能: %s error_type=%s error=%s",
                log_context,
                type(exc).__name__,
                str(exc)[:300],
            )
            return _failed("无法确认 Rocket.Chat 线程功能是否开启，未发送合并转发", retcode=1500)
        if threads_enabled is not True:
            logger.warning(
                "[RocketCatShell][OneBot][Forward] Rocket.Chat 线程功能未开启或状态无效: %s enabled=%s",
                log_context,
                threads_enabled,
            )
            return _failed("Rocket.Chat 线程功能未开启或无法确认，未发送合并转发", retcode=1500)

        title = f"合并转发消息(查看{top_level_nodes}条转发消息)"
        progress["phase"] = "发送线程头"
        logger.info(
            "[RocketCatShell][OneBot][Forward] 开始发送合并转发线程头: %s room_id=%s original_thread_context=%s nodes=%d expanded_items=%d",
            log_context,
            room_id,
            original_thread_source_id or "none",
            top_level_nodes,
            len(prepared),
        )
        try:
            header_message = await self._rocketchat.send_text(
                room_id,
                title,
                thread_mode=True,
            )
            if not isinstance(header_message, dict):
                raise RuntimeError("Rocket.Chat 未返回线程头消息")
            thread_source_id = str(header_message.get("_id") or "").strip()
            if not thread_source_id:
                raise RuntimeError("Rocket.Chat 线程头响应缺少真实消息 ID")
            progress["thread_header_source_id"] = thread_source_id
            header_message_id = await self._map_sent_message(
                room_id,
                header_message,
                requested_thread_source_id=None,
            )
            if header_message_id is None:
                raise RuntimeError("无法为 Rocket.Chat 线程头建立 OneBot 消息映射")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "[RocketCatShell][OneBot][Forward] 合并转发线程头发送或映射失败: %s error_type=%s error=%s",
                log_context,
                type(exc).__name__,
                str(exc)[:300],
            )
            header_state = (
                "线程头已创建，但消息映射失败"
                if progress.get("thread_header_source_id")
                else "线程头状态未确认"
            )
            return _failed(
                f"合并转发线程头阶段失败（{header_state}，正文未发送）: {exc}",
                retcode=1500,
            )

        progress["thread_header_source_id"] = thread_source_id
        progress["thread_header_message_id"] = header_message_id
        logger.info(
            "[RocketCatShell][OneBot][Forward] 合并转发线程头已发送: %s room_id=%s thread_header_id=%s message_id=%d title_nodes=%d",
            log_context,
            room_id,
            thread_source_id,
            header_message_id,
            top_level_nodes,
        )

        for item_position, (node_index, outbound, item_segment_types) in enumerate(prepared, start=1):
            progress["phase"] = "发送线程正文"
            progress["current_item"] = item_position
            item_sent_count = 0
            logger.info(
                "[RocketCatShell][OneBot][Forward] 开始发送合并转发线程正文: %s item=%d/%d node=%d thread_header_id=%s segment_types=%s",
                log_context,
                item_position,
                len(prepared),
                node_index,
                thread_source_id,
                item_segment_types,
            )

            async def map_progress(raw_message: dict[str, Any]) -> None:
                nonlocal item_sent_count
                progress["sent_count"] += 1
                mapped_id = await self._map_sent_message(
                    room_id,
                    raw_message,
                    requested_thread_source_id=thread_source_id,
                )
                if mapped_id is None:
                    logger.error(
                        "[RocketCatShell][OneBot][Forward] 线程正文已发送但无法建立 OneBot 映射: %s item=%d/%d node=%d thread_header_id=%s delivered_count=%d",
                        log_context,
                        item_position,
                        len(prepared),
                        node_index,
                        thread_source_id,
                        progress["sent_count"],
                    )
                    raise RuntimeError("无法为已发送线程正文建立 OneBot message_id")
                progress["last_message_id"] = mapped_id
                item_sent_count += 1
                logger.info(
                    "[RocketCatShell][OneBot][Forward] 合并转发线程正文已发送: %s item=%d/%d node=%d item_message=%d body_sent=%d thread_header_id=%s message_id=%d",
                    log_context,
                    item_position,
                    len(prepared),
                    node_index,
                    item_sent_count,
                    progress["sent_count"],
                    thread_source_id,
                    mapped_id,
                )

            try:
                await self._rocketchat.send_message_segments(
                    room_id,
                    outbound.get("segments") or [],
                    thread_source_id=thread_source_id,
                    reply_source_id=outbound.get("reply_source_id"),
                    mention_usernames=outbound.get("mention_usernames") or [],
                    reply_mention_username=outbound.get("reply_mention_username") or None,
                    strict_delivery=True,
                    thread_mode=True,
                    on_message=map_progress,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "[RocketCatShell][OneBot][Forward] 合并转发线程正文发送失败并已停止后续正文: %s item=%d/%d node=%d thread_header_id=%s body_sent=%d error_type=%s error=%s",
                    log_context,
                    item_position,
                    len(prepared),
                    node_index,
                    thread_source_id,
                    progress["sent_count"],
                    type(exc).__name__,
                    str(exc)[:300],
                )
                return _failed(
                    "合并转发线程正文发送失败（"
                    f"第 {item_position} 条消息，来源 node {node_index}，"
                    f"线程头已创建，已发送 {progress['sent_count']} 条正文）: {exc}",
                    retcode=1500,
                )
            logger.info(
                "[RocketCatShell][OneBot][Forward] 合并转发线程正文节点发送完成: %s item=%d/%d node=%d item_sent=%d body_sent=%d thread_header_id=%s",
                log_context,
                item_position,
                len(prepared),
                node_index,
                item_sent_count,
                progress["sent_count"],
                thread_source_id,
            )

        last_message_id = progress.get("last_message_id")
        if last_message_id is None:
            logger.error(
                "[RocketCatShell][OneBot][Forward] 线程头已发送但未产生可映射正文: %s thread_header_id=%s expanded_items=%d",
                log_context,
                thread_source_id,
                len(prepared),
            )
            return _failed("合并转发线程头已发送，但没有产生可映射的正文消息", retcode=1500)
        logger.info(
            "[RocketCatShell][OneBot][Forward] 合并转发线程发送完成: %s room_id=%s original_thread_context=%s nodes=%d expanded_items=%d body_sent=%d thread_header_id=%s header_message_id=%d last_message_id=%d",
            log_context,
            room_id,
            original_thread_source_id or "none",
            top_level_nodes,
            len(prepared),
            progress["sent_count"],
            thread_source_id,
            header_message_id,
            int(last_message_id),
        )
        return _ok({"message_id": int(last_message_id)})

    async def _handle_get_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        message_id = params.get("message_id")
        if message_id is None:
            message_id = params.get("id")
        event = await self._inbound.hydrate(message_id)
        if not event:
            return _failed(f"找不到消息: {message_id}", retcode=1404)
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
        mappings_by_user: dict[str, Any] = {}
        ensure_users = getattr(self._id_map, "ensure_users", None)
        if callable(ensure_users):
            mappings_by_user = await ensure_users(
                [
                    {
                        "user_id": str(member.get("_id") or ""),
                        "username": str(member.get("username") or ""),
                        "nickname": str(
                            member.get("name")
                            or member.get("nickname")
                            or ""
                        ),
                        "is_bot": str(member.get("_id") or "")
                        == str(self._rocketchat.user_id or ""),
                    }
                    for member in members
                    if member.get("_id")
                ]
            )
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
                    mapping=mappings_by_user.get(str(member_id)),
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
            resolved_user_id = (
                await self._ensure_user_mapping(user_source_id, user_info)
            ).surrogate_id
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
        mapping: Any = None,
    ) -> dict[str, Any]:
        user_info = cached or await self._rocketchat.get_user_info(user_source_id)
        if mapping is None:
            mapping = await self._ensure_user_mapping(user_source_id, user_info)
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

    async def _ensure_user_mapping(
        self,
        user_source_id: str,
        user_info: dict[str, Any] | None = None,
    ):
        profile = user_info or {}
        ensure_user = getattr(self._id_map, "ensure_user", None)
        if callable(ensure_user):
            return await ensure_user(
                user_source_id,
                username=str(profile.get("username") or ""),
                nickname=str(profile.get("name") or profile.get("nickname") or ""),
                is_bot=str(user_source_id) == str(self._rocketchat.user_id or ""),
            )
        return await self._id_map.get_or_create("user", user_source_id)

    def _pick_member_role(self, user_info: dict[str, Any]) -> str:
        roles = user_info.get("roles")
        if isinstance(roles, list):
            if "owner" in roles:
                return "owner"
            if "admin" in roles:
                return "admin"
        return "member"

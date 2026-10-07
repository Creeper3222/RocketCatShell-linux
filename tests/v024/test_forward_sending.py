from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web

from rocketcat_shell.bridge.config import BridgeConfig
from rocketcat_shell.bridge.forward_messages import is_forward_candidate, parse_forward_nodes
from rocketcat_shell.bridge.hot_storage import build_runtime_hot_stores
from rocketcat_shell.bridge.onebot_actions import OneBotActionHandler
from rocketcat_shell.bridge.rocketchat_client import RocketChatClient
from rocketcat_shell.bridge.translator_outbound import OutboundMessageTranslator
from rocketcat_shell.bridge.transports import create_transport
from rocketcat_shell.bridge.transports.action_dispatcher import OneBotActionDispatcher
from rocketcat_shell.bridge.transports.codec import OneBotMessageCodec
from rocketcat_shell.bridge.translator_inbound import InboundTranslator
from rocketcat_shell.layout import ProjectLayout
from rocketcat_shell.models import BotRecord, ShellSettings
from rocketcat_shell.registry import BotRegistry
from rocketcat_shell.shell.manager import ShellManager
from rocketcat_shell.update_manifest import build_manifest


def _node(content=None, *, reference_id=None, user_id="remote-user", nickname="Remote"):
    data = {"user_id": user_id, "nickname": nickname}
    if reference_id is not None:
        data["id"] = reference_id
    else:
        data["content"] = content
    return {"type": "node", "data": data}


class _FakeInbound:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.next_id = 100

    async def translate(self, raw_message):
        self.next_id += 1
        source_id = str(raw_message.get("_id") or self.next_id)
        event = {
            "message_id": self.next_id,
            "message": [{"type": "text", "data": {"text": raw_message.get("msg", "")}}],
        }
        self.events[str(event["message_id"])] = event
        self.events[source_id] = event
        return event

    async def hydrate(self, message_id):
        return self.events.get(str(message_id))


class _FakeOutbound:
    def __init__(self):
        self.reply_sources = {"888": "source-reply-888"}
        self.translations: list[dict] = []

    async def translate(
        self,
        message,
        *,
        group_id=None,
        user_id=None,
        fixed_destination=None,
        require_reply_reference=False,
    ):
        if fixed_destination is None:
            if group_id is not None:
                room_id, thread_source_id = f"room-{group_id}", "parent-thread"
            elif user_id is not None:
                room_id, thread_source_id = f"direct-{user_id}", None
            else:
                raise ValueError("缺少 group_id 或 user_id")
        else:
            room_id = str(fixed_destination.get("room_id") or "")
            thread_source_id = fixed_destination.get("thread_source_id")

        segments = list(message) if isinstance(message, list) else []
        reply_source_id = None
        output_segments = []
        for segment in segments:
            if segment.get("type") == "reply" and reply_source_id is None:
                reply_source_id = self.reply_sources.get(str(segment.get("data", {}).get("id")))
                if require_reply_reference and not reply_source_id:
                    raise ValueError(f"无法解析引用消息: {segment.get('data', {}).get('id')}")
            else:
                output_segments.append(segment)
        translated = {
            "room_id": room_id,
            "thread_source_id": thread_source_id,
            "segments": output_segments,
            "reply_source_id": reply_source_id,
            "mention_usernames": [],
            "reply_mention_username": "",
        }
        self.translations.append(
            {
                "message": segments,
                "group_id": group_id,
                "user_id": user_id,
                "fixed_destination": fixed_destination,
                "translated": translated,
            }
        )
        return translated


class _FakeRocketChat:
    bot_username = "rocketbot"
    config = SimpleNamespace(username="rocketbot")

    def __init__(self):
        self.attempts: list[str] = []
        self.calls: list[dict] = []
        self.header_calls: list[dict] = []
        self.fail_text: str | None = None
        self.fail_header = False
        self.wait_text: str | None = None
        self.waiting = asyncio.Event()
        self.release_wait = asyncio.Event()
        self.next_id = 0
        self.threads_enabled: bool | None = True
        self.thread_check_error = False

    async def are_threads_enabled(self):
        if self.thread_check_error:
            raise RuntimeError("mock settings endpoint unavailable")
        return self.threads_enabled

    async def send_text(self, room_id, text, **kwargs):
        self.header_calls.append({"room_id": room_id, "text": text, **kwargs})
        if self.fail_header:
            raise RuntimeError("mock thread header failed")
        self.next_id += 1
        raw = {
            "_id": f"sent-{self.next_id}",
            "rid": room_id,
            "msg": text,
            "tmid": kwargs.get("tmid"),
        }
        self.header_calls[-1]["source_id"] = raw["_id"]
        return raw

    async def send_message_segments(self, room_id, segments, **kwargs):
        text = "".join(
            str(segment.get("data", {}).get("text") or "")
            for segment in segments
            if segment.get("type") == "text"
        )
        self.attempts.append(text)
        self.calls.append({"room_id": room_id, "segments": segments, **kwargs})
        if text == self.fail_text:
            raise RuntimeError("mock send failed")
        if text == self.wait_text:
            self.waiting.set()
            await self.release_wait.wait()
        self.next_id += 1
        raw = {
            "_id": f"sent-{self.next_id}",
            "rid": room_id,
            "msg": text,
            "tmid": kwargs.get("thread_source_id"),
        }
        callback = kwargs.get("on_message")
        if callback:
            await callback(raw)
        return [raw]

    async def await_sent_message_echo(self, room_id):
        del room_id
        return None


class ForwardActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.inbound = _FakeInbound()
        self.outbound = _FakeOutbound()
        self.rocketchat = _FakeRocketChat()
        self.handler = OneBotActionHandler(
            config=SimpleNamespace(onebot_self_id=42, forward_messages_to_thread=False),
            rocketchat=self.rocketchat,
            id_map=SimpleNamespace(),
            messages=SimpleNamespace(),
            private_rooms=SimpleNamespace(),
            context_rooms=SimpleNamespace(),
            inbound=self.inbound,
            outbound=self.outbound,
        )

    async def test_supported_action_names_aliases_and_plain_node_send_entry(self):
        cases = [
            ("send_group_forward_msg", {"group_id": 7, "messages": [_node("group")]}, "room-7"),
            ("send_private_forward_msg", {"user_id": 9, "message": [_node("private")]}, "direct-9"),
            ("send_forward_msg", {"message_type": "group", "group_id": 8, "messages": [_node("generic")]}, "room-8"),
            ("send_forward_msg", {"user_id": 12, "messages": [_node("generic-private")]}, "direct-12"),
            ("send_group_msg", {"group_id": 10, "message": [_node("ordinary-group")]}, "room-10"),
            ("send_msg", {"message_type": "private", "user_id": 11, "message": [_node("ordinary-private")]}, "direct-11"),
        ]
        for action, params, expected_room in cases:
            with self.subTest(action=action):
                result = await self.handler.handle(action, params)
                self.assertEqual("ok", result["status"])
                self.assertEqual(expected_room, self.rocketchat.calls[-1]["room_id"])
                self.assertTrue(result["data"]["message_id"] > 0)

        plugin_dispatcher = AsyncMock(return_value={"status": "ok", "data": "plugin"})
        self.handler._plugin_action_dispatcher = plugin_dispatcher
        result = await self.handler.handle(
            "send_group_forward_msg",
            {"group_id": 13, "messages": [_node("core action")]},
        )
        self.assertEqual("ok", result["status"])
        unsupported = await self.handler.handle("get_forward_msg", {"message_id": 1})
        self.assertEqual("failed", unsupported["status"])
        self.assertEqual(1404, unsupported["retcode"])
        plugin_dispatcher.assert_not_awaited()

    async def test_nested_nodes_cq_escaping_order_and_metadata_are_preserved(self):
        payload = {
            "messages": [
                _node("first &amp; &#91;literal&#93; [CQ:at,qq=all]", nickname="Not a prefix"),
                _node(
                    {"messages": [_node("nested-one"), _node("nested-two")]},
                    nickname="Also ignored",
                ),
            ]
        }
        self.assertTrue(is_forward_candidate(payload))
        plan = parse_forward_nodes(payload)
        self.assertEqual(3, len(plan))

        result = await self.handler.handle(
            "send_group_forward_msg",
            {"group_id": 7, "messages": payload},
        )
        self.assertEqual("ok", result["status"])
        self.assertEqual(
            ["first & [literal] ", "nested-one", "nested-two"],
            self.rocketchat.attempts,
        )
        first_types = [segment["type"] for segment in self.rocketchat.calls[0]["segments"]]
        self.assertEqual(["text", "at"], first_types)
        self.assertNotIn("Not a prefix", json.dumps(self.rocketchat.calls[0]["segments"]))
        self.assertTrue(all(call["thread_source_id"] == "parent-thread" for call in self.rocketchat.calls))

    async def test_thread_mode_uses_outer_node_count_maps_header_and_routes_all_bodies(self):
        self.handler._config.forward_messages_to_thread = True
        payload = {
            "messages": [
                _node({"messages": [_node("nested one"), _node("nested two")]}),
                _node("outer two"),
            ]
        }

        with self.assertLogs("rocketcat", level="INFO") as captured:
            result = await self.handler.handle(
                "send_group_forward_msg",
                {"group_id": 7, "messages": payload},
            )

        self.assertEqual("ok", result["status"])
        self.assertEqual(
            "合并转发消息(查看2条转发消息)",
            self.rocketchat.header_calls[0]["text"],
        )
        self.assertEqual("room-7", self.rocketchat.header_calls[0]["room_id"])
        self.assertTrue(self.rocketchat.header_calls[0]["thread_mode"])
        self.assertIsNone(self.rocketchat.header_calls[0].get("tmid"))
        self.assertEqual(["nested one", "nested two", "outer two"], self.rocketchat.attempts)
        self.assertTrue(all(call["thread_source_id"] == "sent-1" for call in self.rocketchat.calls))
        self.assertTrue(all(call["thread_mode"] for call in self.rocketchat.calls))
        self.assertEqual(104, result["data"]["message_id"])
        log_text = "\n".join(captured.output)
        for expected in (
            "mode=thread",
            "room_id=room-7",
            "original_thread_context=parent-thread",
            "nodes=2",
            "expanded_items=3",
            "thread_header_id=sent-1",
            "body_sent=3",
        ):
            self.assertIn(expected, log_text)
        for private_body in ("nested one", "nested two", "outer two"):
            self.assertNotIn(private_body, log_text)

        header_id = self.inbound.events["sent-1"]["message_id"]
        header_event = await self.handler.handle("get_msg", {"message_id": header_id})
        self.assertEqual("合并转发消息(查看2条转发消息)", header_event["data"]["message"][0]["data"]["text"])
        body_event = await self.handler.handle("get_msg", {"message_id": result["data"]["message_id"]})
        self.assertEqual("outer two", body_event["data"]["message"][0]["data"]["text"])

    async def test_thread_mode_fails_closed_before_header_and_stops_on_header_failure(self):
        self.handler._config.forward_messages_to_thread = True
        for threads_enabled in (False, None):
            with self.subTest(threads_enabled=threads_enabled):
                self.rocketchat.threads_enabled = threads_enabled
                result = await self.handler.handle(
                    "send_group_forward_msg",
                    {"group_id": 7, "messages": [_node("not sent")]},
                )
                self.assertEqual("failed", result["status"])
                self.assertEqual([], self.rocketchat.header_calls)
                self.assertEqual([], self.rocketchat.attempts)

        self.rocketchat.threads_enabled = True
        self.rocketchat.thread_check_error = True
        check_failed = await self.handler.handle(
            "send_group_forward_msg",
            {"group_id": 7, "messages": [_node("not sent")]},
        )
        self.assertEqual("failed", check_failed["status"])
        self.assertEqual([], self.rocketchat.header_calls)
        self.assertEqual([], self.rocketchat.attempts)
        self.rocketchat.thread_check_error = False

        self.rocketchat.fail_header = True
        result = await self.handler.handle(
            "send_group_forward_msg",
            {"group_id": 7, "messages": [_node("not sent")]},
        )
        self.assertEqual("failed", result["status"])
        self.assertIn("线程头状态未确认", result["wording"])
        self.assertEqual([], self.rocketchat.attempts)

    async def test_thread_partial_failure_keeps_header_and_prior_body_mapping(self):
        self.handler._config.forward_messages_to_thread = True
        self.rocketchat.fail_text = "fail"
        result = await self.handler.handle(
            "send_group_forward_msg",
            {"group_id": 7, "messages": [_node("sent"), _node("fail"), _node("never")]},
        )

        self.assertEqual("failed", result["status"])
        self.assertIn("第 2 条消息", result["wording"])
        self.assertIn("已发送 1 条正文", result["wording"])
        self.assertEqual(["sent", "fail"], self.rocketchat.attempts)
        self.assertEqual("sent-1", self.rocketchat.calls[0]["thread_source_id"])
        header_id = self.inbound.events["sent-1"]["message_id"]
        body_id = self.inbound.events["sent-2"]["message_id"]
        self.assertIn("合并转发消息(查看3条转发消息)", str((await self.handler.handle("get_msg", {"message_id": header_id}))["data"]))
        self.assertEqual("sent", (await self.handler.handle("get_msg", {"message_id": body_id}))["data"]["message"][0]["data"]["text"])

    async def test_thread_setting_does_not_change_normal_messages(self):
        self.handler._config.forward_messages_to_thread = True
        result = await self.handler.handle(
            "send_group_msg",
            {"group_id": 7, "message": [{"type": "text", "data": {"text": "ordinary"}}]},
        )
        self.assertEqual("ok", result["status"])
        self.assertEqual([], self.rocketchat.header_calls)
        self.assertEqual(["ordinary"], self.rocketchat.attempts)
        self.assertFalse(self.rocketchat.calls[0].get("thread_mode", False))

    async def test_thread_mode_covers_private_generic_and_plain_send_node_entries(self):
        self.handler._config.forward_messages_to_thread = True
        cases = [
            (
                "send_private_forward_msg",
                {"user_id": 9, "message": [_node("private forward")]},
                "direct-9",
            ),
            (
                "send_forward_msg",
                {"message_type": "group", "group_id": 8, "messages": [_node("generic group")]},
                "room-8",
            ),
            (
                "send_forward_msg",
                {"message_type": "private", "user_id": 12, "messages": [_node("generic private")]},
                "direct-12",
            ),
            (
                "send_group_msg",
                {"group_id": 10, "message": [_node("plain node list")]},
                "room-10",
            ),
        ]

        for action, params, expected_room in cases:
            with self.subTest(action=action):
                result = await self.handler.handle(action, params)
                header = self.rocketchat.header_calls[-1]
                body = self.rocketchat.calls[-1]
                self.assertEqual("ok", result["status"])
                self.assertEqual(expected_room, header["room_id"])
                self.assertEqual("合并转发消息(查看1条转发消息)", header["text"])
                self.assertTrue(header["thread_mode"])
                self.assertEqual(header["source_id"], body["thread_source_id"])
                self.assertTrue(body["thread_mode"])

    async def test_forward_logs_detection_parse_send_progress_without_message_body(self):
        with self.assertLogs("rocketcat", level="INFO") as captured:
            result = await self.handler.handle(
                "send_group_forward_msg",
                {"group_id": 7, "messages": [_node("log-secret-text")]},
            )

        self.assertEqual("ok", result["status"])
        log_text = "\n".join(captured.output)
        for expected in (
            "检测到 OneBot 合并转发消息块",
            "合并转发解析完成",
            "预检通过，开始发送",
            "开始发送合并转发节点",
            "合并转发消息已发送",
            "合并转发发送完成",
        ):
            self.assertIn(expected, log_text)
        self.assertNotIn("log-secret-text", log_text)

    async def test_existing_message_and_reply_references_are_resolved_before_sending(self):
        self.inbound.events["777"] = {
            "message_id": 777,
            "message": [{"type": "text", "data": {"text": "hydrated source"}}],
        }
        result = await self.handler.handle(
            "send_group_forward_msg",
            {
                "group_id": 5,
                "messages": [
                    _node(reference_id=777),
                    _node([{"type": "reply", "data": {"id": "888"}}, {"type": "text", "data": {"text": "reply body"}}]),
                ],
            },
        )
        self.assertEqual("ok", result["status"])
        self.assertEqual(["hydrated source", "reply body"], self.rocketchat.attempts)
        self.assertEqual("source-reply-888", self.rocketchat.calls[1]["reply_source_id"])
        final_message = await self.handler.handle("get_msg", {"message_id": result["data"]["message_id"]})
        self.assertEqual("reply body", final_message["data"]["message"][0]["data"]["text"])

    async def test_invalid_empty_mixed_and_unknown_reference_payloads_send_nothing(self):
        invalid_payloads = [
            [],
            [{"type": "text", "data": {"text": "mixed"}}, _node("node")],
            [_node(None)],
            [_node(reference_id="unknown")],
            [_node([{"type": "reply", "data": {"id": "not-mapped"}}])],
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                before = len(self.rocketchat.calls)
                result = await self.handler.handle(
                    "send_group_forward_msg",
                    {"group_id": 7, "messages": payload},
                )
                self.assertEqual("failed", result["status"])
                self.assertEqual(before, len(self.rocketchat.calls))

    async def test_partial_failure_keeps_prior_mapping_and_stops_later_nodes(self):
        self.rocketchat.fail_text = "fail"
        result = await self.handler.handle(
            "send_group_forward_msg",
            {"group_id": 3, "messages": [_node("sent"), _node("fail"), _node("never")]},
        )
        self.assertEqual("failed", result["status"])
        self.assertIn("第 2 条消息", result["wording"])
        self.assertIn("已发送 1 条", result["wording"])
        self.assertEqual(["sent", "fail"], self.rocketchat.attempts)
        first_id = self.inbound.events["sent-1"]["message_id"]
        self.assertEqual("sent", (await self.handler.handle("get_msg", {"message_id": first_id}))[
            "data"]["message"][0]["data"]["text"]
        )

    async def test_timeout_stops_batch_reports_progress_and_releases_target_lock(self):
        import rocketcat_shell.bridge.transports.action_dispatcher as dispatcher_module

        self.rocketchat.wait_text = "waiting"
        dispatcher = OneBotActionDispatcher(
            self.handler.handle,
            codec=OneBotMessageCodec("array"),
            owner="forward-timeout-test",
        )
        dispatcher.start()
        try:
            with unittest.mock.patch.object(dispatcher_module, "ACTION_TIMEOUT_SECONDS", 0.08):
                forward_task = asyncio.create_task(
                    dispatcher.execute(
                        "send_group_forward_msg",
                        {"group_id": 19, "messages": [_node("sent"), _node("waiting"), _node("never")]},
                        "forward-echo",
                    )
                )
                await asyncio.wait_for(self.rocketchat.waiting.wait(), timeout=1)
                normal_task = asyncio.create_task(
                    dispatcher.execute(
                        "send_group_msg",
                        {"group_id": 19, "message": [{"type": "text", "data": {"text": "ordinary"}}]},
                        "normal-echo",
                    )
                )
                forward_result, normal_result = await asyncio.gather(forward_task, normal_task)
            self.assertEqual("forward-echo", forward_result["echo"])
            self.assertEqual("failed", forward_result["status"])
            self.assertIn("第 2 条消息", forward_result["wording"])
            self.assertIn("已发送 1 条", forward_result["wording"])
            self.assertEqual("normal-echo", normal_result["echo"])
            self.assertEqual("ok", normal_result["status"])
            self.assertEqual(["sent", "waiting", "ordinary"], self.rocketchat.attempts)
        finally:
            await dispatcher.stop(drain_timeout=0.2)

    async def test_thread_timeout_preserves_header_reports_body_progress_and_releases_lock(self):
        import rocketcat_shell.bridge.transports.action_dispatcher as dispatcher_module

        self.handler._config.forward_messages_to_thread = True
        self.rocketchat.wait_text = "waiting"
        dispatcher = OneBotActionDispatcher(
            self.handler.handle,
            codec=OneBotMessageCodec("array"),
            owner="thread-forward-timeout-test",
        )
        dispatcher.start()
        try:
            with unittest.mock.patch.object(dispatcher_module, "ACTION_TIMEOUT_SECONDS", 0.08):
                forward_task = asyncio.create_task(
                    dispatcher.execute(
                        "send_group_forward_msg",
                        {"group_id": 19, "messages": [_node("sent"), _node("waiting"), _node("never")]},
                        "thread-forward-echo",
                    )
                )
                await asyncio.wait_for(self.rocketchat.waiting.wait(), timeout=1)
                normal_task = asyncio.create_task(
                    dispatcher.execute(
                        "send_group_msg",
                        {"group_id": 19, "message": [{"type": "text", "data": {"text": "ordinary"}}]},
                        "normal-echo",
                    )
                )
                forward_result, normal_result = await asyncio.gather(forward_task, normal_task)

            self.assertEqual("failed", forward_result["status"])
            self.assertIn("线程头已创建", forward_result["wording"])
            self.assertIn("发送线程正文", forward_result["wording"])
            self.assertIn("第 2 条正文", forward_result["wording"])
            self.assertIn("已发送 1 条", forward_result["wording"])
            self.assertEqual("ok", normal_result["status"])
            self.assertEqual(["sent", "waiting", "ordinary"], self.rocketchat.attempts)
            self.assertEqual("sent-1", self.rocketchat.calls[0]["thread_source_id"])
            header_id = self.inbound.events["sent-1"]["message_id"]
            body_id = self.inbound.events["sent-2"]["message_id"]
            self.assertEqual(
                "合并转发消息(查看3条转发消息)",
                (await self.handler.handle("get_msg", {"message_id": header_id}))["data"]["message"][0]["data"]["text"],
            )
            self.assertEqual(
                "sent",
                (await self.handler.handle("get_msg", {"message_id": body_id}))["data"]["message"][0]["data"]["text"],
            )
            self.assertFalse(dispatcher._target_locks)
        finally:
            self.rocketchat.release_wait.set()
            await dispatcher.stop(drain_timeout=0.2)

    async def test_cancelled_batch_stops_and_retains_already_mapped_messages(self):
        self.rocketchat.wait_text = "waiting"
        task = asyncio.create_task(
            self.handler.handle(
                "send_group_forward_msg",
                {"group_id": 20, "messages": [_node("sent"), _node("waiting"), _node("never")]},
            )
        )
        await asyncio.wait_for(self.rocketchat.waiting.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(["sent", "waiting"], self.rocketchat.attempts)
        first_id = self.inbound.events["sent-1"]["message_id"]
        self.assertEqual("sent", (await self.handler.handle("get_msg", {"message_id": first_id}))[
            "data"]["message"][0]["data"]["text"]
        )

    async def test_cancelled_thread_batch_keeps_header_and_body_mappings(self):
        self.handler._config.forward_messages_to_thread = True
        self.rocketchat.wait_text = "waiting"
        task = asyncio.create_task(
            self.handler.handle(
                "send_group_forward_msg",
                {"group_id": 20, "messages": [_node("sent"), _node("waiting"), _node("never")]},
            )
        )
        await asyncio.wait_for(self.rocketchat.waiting.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(["sent", "waiting"], self.rocketchat.attempts)
        self.assertEqual("sent-1", self.rocketchat.calls[0]["thread_source_id"])
        for source_id, expected in (
            ("sent-1", "合并转发消息(查看3条转发消息)"),
            ("sent-2", "sent"),
        ):
            mapped_id = self.inbound.events[source_id]["message_id"]
            mapped = await self.handler.handle("get_msg", {"message_id": mapped_id})
            self.assertEqual(expected, mapped["data"]["message"][0]["data"]["text"])

    async def test_target_lock_keeps_normal_message_after_entire_forward_batch(self):
        self.rocketchat.wait_text = "pause"
        dispatcher = OneBotActionDispatcher(
            self.handler.handle,
            codec=OneBotMessageCodec("array"),
            owner="forward-order-test",
        )
        dispatcher.start()
        try:
            forward_task = asyncio.create_task(
                dispatcher.execute(
                    "send_group_forward_msg",
                    {"group_id": 21, "messages": [_node("first"), _node("pause"), _node("last")]},
                    "forward",
                )
            )
            await asyncio.wait_for(self.rocketchat.waiting.wait(), timeout=1)
            normal_task = asyncio.create_task(
                dispatcher.execute(
                    "send_group_msg",
                    {"group_id": 21, "message": [{"type": "text", "data": {"text": "ordinary"}}]},
                    "normal",
                )
            )
            await asyncio.sleep(0.02)
            self.assertEqual(["first", "pause"], self.rocketchat.attempts)
            self.rocketchat.release_wait.set()
            forward_result, normal_result = await asyncio.gather(forward_task, normal_task)
            self.assertEqual("ok", forward_result["status"])
            self.assertEqual("ok", normal_result["status"])
            self.assertEqual(["first", "pause", "last", "ordinary"], self.rocketchat.attempts)
            self.assertFalse(dispatcher._target_locks)
        finally:
            self.rocketchat.release_wait.set()
            await dispatcher.stop(drain_timeout=0.2)

    async def test_strict_media_failure_stops_after_previous_delivery(self):
        client = object.__new__(RocketChatClient)
        client.send_text = AsyncMock(return_value={"_id": "sent-text"})
        client._send_media_segment = AsyncMock(return_value=None)
        delivered: list[str] = []

        async def on_message(raw_message):
            delivered.append(raw_message["_id"])

        with self.assertRaisesRegex(RuntimeError, "image 媒体发送"):
            await client.send_message_segments(
                "room",
                [
                    {"type": "text", "data": {"text": "before"}},
                    {"type": "image", "data": {"file": "https://media.example/image.png"}},
                    {"type": "text", "data": {"text": "after"}},
                ],
                strict_delivery=True,
                thread_mode=True,
                thread_source_id="thread-header",
                on_message=on_message,
            )
        self.assertEqual(["sent-text"], delivered)
        self.assertEqual(1, client.send_text.await_count)
        self.assertTrue(client._send_media_segment.await_args.kwargs["thread_mode"])
        self.assertEqual("thread-header", client._send_media_segment.await_args.kwargs["tmid"])

    async def test_mixed_media_and_text_deliver_in_segment_order(self):
        client = object.__new__(RocketChatClient)
        media_types = ["image", "file", "record", "video"]
        client.send_text = AsyncMock(
            side_effect=[{"_id": f"text-{index}"} for index in range(1, 6)]
        )
        client._send_media_segment = AsyncMock(
            side_effect=[{"_id": f"media-{index}"} for index in range(1, 5)]
        )
        delivered: list[str] = []

        async def on_message(raw_message):
            delivered.append(raw_message["_id"])

        segments = []
        for index, media_type in enumerate(media_types, start=1):
            segments.append({"type": "text", "data": {"text": f"text-{index}"}})
            segments.append({"type": media_type, "data": {"file": f"base64://{media_type}"}})
        segments.append({"type": "text", "data": {"text": "text-5"}})

        sent = await client.send_message_segments(
            "room",
            segments,
            strict_delivery=True,
            on_message=on_message,
        )
        self.assertEqual(
            ["text-1", "media-1", "text-2", "media-2", "text-3", "media-3", "text-4", "media-4", "text-5"],
            delivered,
        )
        self.assertEqual(delivered, [message["_id"] for message in sent])
        self.assertEqual(media_types, [call.args[1] for call in client._send_media_segment.await_args_list])


class ForwardConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_thread_switch_ui_is_enabled_only_for_websocket_client_and_logs_share_level_colors(self):
        root = Path(__file__).resolve().parents[2]
        html = (root / "rocketcat_shell" / "shell" / "static" / "index.html").read_text(encoding="utf-8")
        js = (root / "rocketcat_shell" / "shell" / "static" / "app.js").read_text(encoding="utf-8")
        css = (root / "rocketcat_shell" / "shell" / "static" / "styles.css").read_text(encoding="utf-8")

        switch = html.split('id="forwardMessagesToThreadSetting"', 1)[1].split("</div>", 2)[0]
        self.assertIn('name="forward_messages_to_thread" type="checkbox"', switch)
        self.assertNotIn("disabled", switch)
        self.assertNotIn("尚未实现", switch)
        self.assertIn("最外层节点统计", switch)
        self.assertIn("forward_messages_to_thread: false", js)
        self.assertIn("spec.type !== 'websocket-client'", js)

        for level in ("debug", "info", "warn", "error"):
            selector = f".log-{level} .log-entry-level,\n.log-{level} .log-entry-line"
            self.assertIn(selector, css)

    async def test_release_manifest_omits_generated_bytecode(self):
        root = Path(__file__).resolve().parents[2]
        manifest = build_manifest(root, version="v0.2.4", tag_name="v0.2.4")
        paths = {entry["path"] for entry in manifest["files"]}
        self.assertFalse(any("__pycache__" in path or path.endswith(".pyc") for path in paths))

    @staticmethod
    def _manager(root: Path) -> ShellManager:
        config_dir = root / "config"
        data_dir = root / "data"
        layout = ProjectLayout(
            project_root=root,
            package_root=Path(__file__).resolve().parents[2] / "rocketcat_shell",
            config_dir=config_dir,
            plugins_config_dir=config_dir / "plugins_config",
            data_dir=data_dir,
            temp_dir=data_dir / "temp",
            bots_dir=data_dir / "bots",
            plugins_dir=data_dir / "plugins",
            plugin_data_dir=data_dir / "plugin_data",
            logs_dir=root / "logs",
            shell_settings_path=config_dir / "shell.json",
            bot_registry_path=config_dir / "bots.json",
            log_file_path=root / "logs" / "rocketcat.log",
            onebot_transports_path=config_dir / "onebot_transports.json",
        )
        layout.ensure_directories()
        manager = ShellManager(layout)
        manager.settings = ShellSettings()
        return manager

    async def test_old_default_registry_import_export_and_edit_preserve_boolean(self):
        defaults = ShellSettings()
        old = BotRecord.from_mapping({"id": "old", "name": "Old"}, defaults=defaults)
        self.assertFalse(old.forward_messages_to_thread)
        old_runtime = BridgeConfig.from_mapping({"enabled": False})
        self.assertFalse(old_runtime.forward_messages_to_thread)
        self.assertFalse(old_runtime.to_mapping()["forward_messages_to_thread"])

        runtime = BridgeConfig.from_mapping({"forward_messages_to_thread": True})
        self.assertTrue(runtime.forward_messages_to_thread)
        self.assertTrue(
            BridgeConfig.from_mapping(runtime.to_mapping()).forward_messages_to_thread
        )

        for configured_value in (False, True):
            with self.subTest(value=configured_value), tempfile.TemporaryDirectory() as temp:
                bot_path = Path(temp) / "bots.json"
                registry = BotRegistry(bot_path, Path(temp) / "transports.json")
                bot = BotRecord.from_mapping(
                    {
                        "id": "bot-a",
                        "name": "A",
                        "forward_messages_to_thread": configured_value,
                    },
                    defaults=defaults,
                )
                registry.save([bot])
                restored = registry.load(defaults=defaults)[0]
                self.assertIs(configured_value, restored.forward_messages_to_thread)
                self.assertIs(configured_value, restored.to_mapping()["forward_messages_to_thread"])

                manager = self._manager(Path(temp) / "manager")
                manager.bots = [restored]
                exported = await manager.export_configuration()
                self.assertIs(configured_value, exported["bots"][0]["forward_messages_to_thread"])
                await manager.import_configuration(exported)
                self.assertIs(configured_value, manager.bots[0].forward_messages_to_thread)
                compact = (await manager.list_bots(compact=True))[0]
                self.assertIs(configured_value, compact["forward_messages_to_thread"])
                edited = await manager.update_bot("bot-a", {"name": "Edited"})
                self.assertEqual("Edited", edited["name"])
                self.assertIs(configured_value, edited["forward_messages_to_thread"])

    async def test_invalid_import_does_not_replace_saved_forward_setting(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = self._manager(Path(temp))
            bot = BotRecord.from_mapping(
                {
                    "id": "bot-a",
                    "name": "A",
                    "forward_messages_to_thread": False,
                },
                defaults=manager.settings,
            )
            manager.bots = [bot]
            manager._persist_after_bot_change_locked()
            paths = [
                manager.layout.shell_settings_path,
                manager.layout.bot_registry_path,
                manager.layout.onebot_transports_path,
            ]
            before = {path: path.read_bytes() for path in paths}
            invalid = await manager.export_configuration()
            invalid["bots"][0]["forward_messages_to_thread"] = True
            invalid["bots"][0]["onebot_transport"] = {
                "type": "http-server",
                "settings": {"host": "127.0.0.1", "port": 70000},
            }
            with self.assertRaisesRegex(ValueError, "不能大于 65535"):
                await manager.import_configuration(invalid)
            self.assertFalse(manager.bots[0].forward_messages_to_thread)
            self.assertEqual(before, {path: path.read_bytes() for path in paths})


class WebsocketForwardIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_astrbot_shape_reaches_mock_rocketchat_and_echoes_last_message(self):
        with tempfile.TemporaryDirectory() as temp:
            bundle = build_runtime_hot_stores(Path(temp) / "state")
            room = await bundle.id_map.get_or_create("room", "room-live")
            sent_payloads: list[dict] = []
            next_message_id = 0
            connected_ws: list[web.WebSocketResponse] = []
            close_upstream_ws = asyncio.Event()

            async def rest_api_info(_request):
                return web.json_response({"version": "8.5.0"})

            async def rest_login(_request):
                return web.json_response(
                    {
                        "status": "success",
                        "data": {"authToken": "test-token", "userId": "bot-user"},
                    }
                )

            async def rest_room_info(_request):
                return web.json_response(
                    {"success": True, "room": {"_id": "room-live", "t": "c", "name": "room", "fname": "Room"}}
                )

            async def rest_public_settings(_request):
                return web.json_response(
                    {"success": True, "settings": [{"_id": "Threads_enabled", "value": True}]}
                )

            async def rest_post_message(request):
                nonlocal next_message_id
                payload = await request.json()
                sent_payloads.append(
                    {
                        "route": "chat.postMessage",
                        "text": payload.get("text", ""),
                        "tmid": payload.get("tmid"),
                    }
                )
                next_message_id += 1
                return web.json_response(
                    {
                        "success": True,
                        "message": {
                            "_id": f"live-{next_message_id}",
                            "rid": payload["roomId"],
                            "msg": payload.get("text", ""),
                            "tmid": payload.get("tmid"),
                            "ts": "2026-10-06T10:00:00.000Z",
                            "u": {"_id": "bot-user", "username": "bot", "name": "Bot"},
                        },
                    }
                )

            async def rest_send_message(request):
                nonlocal next_message_id
                payload = await request.json()
                message = payload.get("message", {})
                sent_payloads.append(
                    {
                        "route": "chat.sendMessage",
                        "text": message.get("msg", ""),
                        "tmid": message.get("tmid"),
                    }
                )
                next_message_id += 1
                return web.json_response(
                    {
                        "success": True,
                        "message": {
                            "_id": f"live-{next_message_id}",
                            "rid": message["rid"],
                            "msg": message.get("msg", ""),
                            "tmid": message.get("tmid"),
                            "ts": "2026-10-06T10:00:00.000Z",
                            "u": {"_id": "bot-user", "username": "bot", "name": "Bot"},
                        },
                    }
                )

            async def rest_rooms_media(request):
                form = await request.post()
                uploaded = form.get("file")
                sent_payloads.append(
                    {
                        "route": "rooms.media",
                        "text": "",
                        "tmid": None,
                        "filename": uploaded.filename if uploaded else "",
                    }
                )
                return web.json_response(
                    {"success": True, "file": {"_id": "uploaded-image", "name": uploaded.filename if uploaded else "image.png", "url": "/file/uploaded-image"}}
                )

            async def rest_media_confirm(request):
                nonlocal next_message_id
                payload = await request.json()
                sent_payloads.append(
                    {
                        "route": "rooms.mediaConfirm",
                        "text": payload.get("msg", ""),
                        "tmid": payload.get("tmid"),
                    }
                )
                next_message_id += 1
                return web.json_response(
                    {
                        "success": True,
                        "message": {
                            "_id": f"live-{next_message_id}",
                            "rid": request.match_info["room_id"],
                            "msg": payload.get("msg", ""),
                            "tmid": payload.get("tmid"),
                            "ts": "2026-10-06T10:00:00.000Z",
                            "u": {"_id": "bot-user", "username": "bot", "name": "Bot"},
                            "file": {"_id": request.match_info["file_id"], "name": payload.get("fileName", "image.png")},
                        },
                    }
                )

            async def onebot_websocket(request):
                websocket = web.WebSocketResponse(autoping=True)
                await websocket.prepare(request)
                connected_ws.append(websocket)
                await close_upstream_ws.wait()
                await websocket.close()
                return websocket

            app = web.Application()
            app.router.add_get("/api/info", rest_api_info)
            app.router.add_post("/api/v1/login", rest_login)
            app.router.add_get("/api/v1/rooms.info", rest_room_info)
            app.router.add_get("/api/v1/settings.public", rest_public_settings)
            app.router.add_post("/api/v1/chat.postMessage", rest_post_message)
            app.router.add_post("/api/v1/chat.sendMessage", rest_send_message)
            app.router.add_post("/api/v1/rooms.media/{room_id}", rest_rooms_media)
            app.router.add_post("/api/v1/rooms.mediaConfirm/{room_id}/{file_id}", rest_media_confirm)
            app.router.add_get("/onebot", onebot_websocket)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]

            config = BridgeConfig.from_mapping(
                {
                    "enabled": True,
                    "id": "forward-e2e",
                    "name": "Forward E2E",
                    "server_url": f"http://127.0.0.1:{port}",
                    "username": "bot",
                    "password": "password",
                    "onebot_transport": {
                        "type": "websocket-client",
                        "settings": {
                            "url": f"ws://127.0.0.1:{port}/onebot",
                            "message_post_format": "array",
                            "report_self_message": False,
                            "reconnect_interval_ms": 100,
                            "heartbeat_interval_ms": 0,
                            "access_token": "",
                            "debug": False,
                        },
                    },
                }
            )
            config.onebot_self_id = 42001
            rocketchat = RocketChatClient(config, media_temp_dir=Path(temp) / "media")
            inbound = InboundTranslator(
                rocketchat,
                bundle.id_map,
                bundle.message_store,
                bundle.private_room_store,
                bundle.context_room_store,
                config.onebot_self_id,
            )
            outbound = OutboundMessageTranslator(
                rocketchat,
                bundle.id_map,
                bundle.message_store,
                bundle.private_room_store,
                bundle.context_room_store,
            )
            handler = OneBotActionHandler(
                config,
                rocketchat,
                bundle.id_map,
                bundle.message_store,
                bundle.private_room_store,
                bundle.context_room_store,
                inbound,
                outbound,
            )
            transport = create_transport(config, handler.handle)
            try:
                await rocketchat.start(start_realtime=False)
                await transport.start()
                async with asyncio.timeout(3):
                    while not connected_ws or not transport.connected:
                        await asyncio.sleep(0.01)

                upstream = connected_ws[0]
                await upstream.send_json(
                    {
                        "action": "send_group_forward_msg",
                        "params": {
                            "group_id": room.surrogate_id,
                            "messages": {
                                "messages": [
                                    _node([{"type": "text", "data": {"text": "first from AstrBot"}}]),
                                    _node([{"type": "text", "data": {"text": "second from AstrBot"}}]),
                                ]
                            },
                        },
                        "echo": "astrbot-forward-echo",
                    }
                )
                async with asyncio.timeout(3):
                    while True:
                        frame = await upstream.receive_json()
                        if frame.get("echo") == "astrbot-forward-echo":
                            break
                self.assertEqual(["first from AstrBot", "second from AstrBot"], [item["text"] for item in sent_payloads])
                self.assertTrue(all(item["route"] == "chat.postMessage" for item in sent_payloads))
                self.assertEqual("ok", frame["status"])
                self.assertEqual("astrbot-forward-echo", frame["echo"])
                self.assertEqual("live-2", await bundle.id_map.get_source("message", frame["data"]["message_id"]))

                await upstream.send_json(
                    {
                        "action": "get_msg",
                        "params": {"message_id": frame["data"]["message_id"]},
                        "echo": "get-last-forward-message",
                    }
                )
                async with asyncio.timeout(3):
                    while True:
                        get_frame = await upstream.receive_json()
                        if get_frame.get("echo") == "get-last-forward-message":
                            break
                self.assertEqual("second from AstrBot", get_frame["data"]["message"][0]["data"]["text"])

                config.forward_messages_to_thread = True
                await upstream.send_json(
                    {
                        "action": "send_group_msg",
                        "params": {
                            "group_id": room.surrogate_id,
                            "message": [{"type": "text", "data": {"text": "ordinary while thread mode enabled"}}],
                        },
                        "echo": "ordinary-with-thread-setting",
                    }
                )
                async with asyncio.timeout(3):
                    while True:
                        ordinary_frame = await upstream.receive_json()
                        if ordinary_frame.get("echo") == "ordinary-with-thread-setting":
                            break
                self.assertEqual("ok", ordinary_frame["status"])
                self.assertEqual("chat.postMessage", sent_payloads[-1]["route"])
                self.assertIsNone(sent_payloads[-1]["tmid"])

                await upstream.send_json(
                    {
                        "action": "send_group_forward_msg",
                        "params": {
                            "group_id": room.surrogate_id,
                            "messages": {
                                "messages": [
                                    _node(
                                        {
                                            "messages": [
                                                _node([{"type": "text", "data": {"text": "nested thread one"}}]),
                                                _node([{"type": "text", "data": {"text": "nested thread two"}}]),
                                            ]
                                        }
                                    ),
                                    _node([{"type": "text", "data": {"text": "outer thread two"}}]),
                                ]
                            },
                        },
                        "echo": "astrbot-thread-forward-echo",
                    }
                )
                async with asyncio.timeout(3):
                    while True:
                        thread_frame = await upstream.receive_json()
                        if thread_frame.get("echo") == "astrbot-thread-forward-echo":
                            break

                thread_payloads = sent_payloads[-4:]
                self.assertEqual(
                    [
                        "合并转发消息(查看2条转发消息)",
                        "nested thread one",
                        "nested thread two",
                        "outer thread two",
                    ],
                    [item["text"] for item in thread_payloads],
                )
                self.assertTrue(all(item["route"] == "chat.sendMessage" for item in thread_payloads))
                header_surrogate = await bundle.id_map.get_surrogate("message", "live-4")
                self.assertIsNotNone(header_surrogate)
                self.assertIsNone(thread_payloads[0]["tmid"])
                self.assertTrue(all(item["tmid"] == "live-4" for item in thread_payloads[1:]))
                self.assertEqual("ok", thread_frame["status"])
                self.assertEqual("astrbot-thread-forward-echo", thread_frame["echo"])
                self.assertEqual("live-7", await bundle.id_map.get_source("message", thread_frame["data"]["message_id"]))
                await upstream.send_json(
                    {
                        "action": "get_msg",
                        "params": {"message_id": header_surrogate},
                        "echo": "get-thread-forward-header",
                    }
                )
                async with asyncio.timeout(3):
                    while True:
                        header_get_frame = await upstream.receive_json()
                        if header_get_frame.get("echo") == "get-thread-forward-header":
                            break
                self.assertEqual(
                    "合并转发消息(查看2条转发消息)",
                    header_get_frame["data"]["message"][0]["data"]["text"],
                )
                await upstream.send_json(
                    {
                        "action": "get_msg",
                        "params": {"message_id": thread_frame["data"]["message_id"]},
                        "echo": "get-thread-forward-last-body",
                    }
                )
                async with asyncio.timeout(3):
                    while True:
                        thread_get_frame = await upstream.receive_json()
                        if thread_get_frame.get("echo") == "get-thread-forward-last-body":
                            break
                self.assertEqual("outer thread two", thread_get_frame["data"]["message"][0]["data"]["text"])

                await upstream.send_json(
                    {
                        "action": "send_group_forward_msg",
                        "params": {
                            "group_id": room.surrogate_id,
                            "messages": [
                                _node(
                                    [
                                        {"type": "text", "data": {"text": "before thread media"}},
                                        {"type": "image", "data": {"file": "base64://aGVsbG8="}},
                                    ]
                                )
                            ],
                        },
                        "echo": "astrbot-thread-media-echo",
                    }
                )
                async with asyncio.timeout(3):
                    while True:
                        media_frame = await upstream.receive_json()
                        if media_frame.get("echo") == "astrbot-thread-media-echo":
                            break
                media_payloads = sent_payloads[-4:]
                self.assertEqual(
                    ["合并转发消息(查看1条转发消息)", "before thread media", "", ""],
                    [item["text"] for item in media_payloads],
                )
                self.assertEqual(
                    ["chat.sendMessage", "chat.sendMessage", "rooms.media", "rooms.mediaConfirm"],
                    [item["route"] for item in media_payloads],
                )
                self.assertTrue(media_payloads[2]["filename"].endswith(".png"))
                self.assertIsNone(media_payloads[0]["tmid"])
                media_thread_header_id = "live-8"
                self.assertEqual(media_thread_header_id, media_payloads[1]["tmid"])
                self.assertEqual(media_thread_header_id, media_payloads[3]["tmid"])
                self.assertEqual("ok", media_frame["status"])
                self.assertEqual("live-10", await bundle.id_map.get_source("message", media_frame["data"]["message_id"]))
            finally:
                await transport.stop()
                await rocketchat.stop()
                close_upstream_ws.set()
                await runner.cleanup()
                bundle.close()


class RocketChatThreadProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_thread_message_uses_send_message_and_preserves_tmid(self):
        client = object.__new__(RocketChatClient)
        client.config = SimpleNamespace(server_url="http://rocket.invalid")
        client.get_room_info = AsyncMock(return_value={"_id": "room", "t": "c"})
        client._post_json_message = AsyncMock(
            return_value={"success": True, "message": {"_id": "msg-1", "rid": "room", "msg": "body", "tmid": "head"}}
        )

        await client._send_structured_message("room", "body", tmid="head", thread_mode=True)

        client._post_json_message.assert_awaited_once_with(
            "http://rocket.invalid/api/v1/chat.sendMessage",
            {"message": {"rid": "room", "msg": "body", "tmid": "head"}},
        )

    async def test_regular_message_path_stays_on_post_message_and_keeps_existing_thread(self):
        client = object.__new__(RocketChatClient)
        client.config = SimpleNamespace(server_url="http://rocket.invalid")
        client.get_room_info = AsyncMock(return_value={"_id": "room", "t": "c"})
        client._post_json_message = AsyncMock(
            return_value={"success": True, "message": {"_id": "msg-2", "rid": "room", "msg": "body", "tmid": "parent"}}
        )

        await client._send_structured_message("room", "body", tmid="parent")

        client._post_json_message.assert_awaited_once_with(
            "http://rocket.invalid/api/v1/chat.postMessage",
            {"roomId": "room", "text": "body", "tmid": "parent"},
        )

    async def test_e2ee_thread_message_keeps_tmid_on_encrypted_send_path(self):
        client = object.__new__(RocketChatClient)
        client.config = SimpleNamespace(server_url="http://rocket.invalid")
        client.get_room_info = AsyncMock(return_value={"_id": "room", "t": "p", "encrypted": True})
        client.e2ee = SimpleNamespace(
            should_encrypt_room=AsyncMock(return_value=True),
            build_send_message=AsyncMock(
                return_value={"message": {"rid": "room", "t": "e2e", "e2e": "pending", "content": {"ciphertext": "..."}, "tmid": "head"}}
            ),
        )
        client._post_json_message = AsyncMock(
            return_value={"success": True, "message": {"_id": "msg-e2ee", "rid": "room", "tmid": "head"}}
        )

        await client._send_structured_message("room", "plaintext", tmid="head", thread_mode=True)

        client.e2ee.build_send_message.assert_awaited_once_with(
            "room",
            text="plaintext",
            attachments=None,
            tmid="head",
            e2e_mentions=None,
        )
        client._post_json_message.assert_awaited_once_with(
            "http://rocket.invalid/api/v1/chat.sendMessage",
            {"message": {"rid": "room", "t": "e2e", "e2e": "pending", "content": {"ciphertext": "..."}, "tmid": "head"}},
        )

    async def test_public_threads_setting_requires_explicit_boolean(self):
        client = object.__new__(RocketChatClient)
        client.config = SimpleNamespace(server_url="http://rocket.invalid")
        client._auth_headers = lambda: {"X-Auth-Token": "token"}
        client._request_json = AsyncMock(
            return_value={"success": True, "settings": [{"_id": "Threads_enabled", "value": True}]}
        )

        self.assertIs(True, await client.are_threads_enabled())
        client._request_json.assert_awaited_once_with(
            "GET",
            "http://rocket.invalid/api/v1/settings.public",
            headers={"X-Auth-Token": "token"},
            params={"_id": "Threads_enabled", "count": 1},
        )

        for response in (
            {"success": False, "settings": [{"_id": "Threads_enabled", "value": True}]},
            {"success": True, "settings": [{"_id": "Threads_enabled", "value": "true"}]},
            {"success": True, "settings": []},
        ):
            with self.subTest(response=response):
                client._request_json.return_value = response
                self.assertIsNone(await client.are_threads_enabled())


if __name__ == "__main__":
    unittest.main()

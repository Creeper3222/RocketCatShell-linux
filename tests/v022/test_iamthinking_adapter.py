from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_PATH = ROOT / "data/plugins/rocketcat_plugin_adapt_iamthinking/main.py"


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("test_iamthinking_v020", PLUGIN_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRocketChat:
    def __init__(self) -> None:
        self.reactions: list[tuple[str, str, bool]] = []
        self.typing: list[tuple[str, bool]] = []

    async def set_message_reaction(
        self,
        message_id: str,
        reaction: str,
        *,
        should_react: bool,
    ) -> bool:
        self.reactions.append((message_id, reaction, should_react))
        return True

    async def set_room_typing(self, room_id: str, *, is_typing: bool) -> bool:
        self.typing.append((room_id, is_typing))
        return True


class FakeMessages:
    def __init__(self, entries: dict[int, dict[str, str]]) -> None:
        self.entries = entries

    async def get_by_surrogate(self, message_id: Any) -> dict[str, str] | None:
        try:
            return self.entries.get(int(message_id))
        except (TypeError, ValueError):
            return None


class FakeRuntime:
    def __init__(
        self,
        *,
        runtime_key: str = "bot-a",
        entries: dict[int, dict[str, str]] | None = None,
    ) -> None:
        self.runtime_key = runtime_key
        self.instance_name = runtime_key
        self.bridge_config = SimpleNamespace(bot_id=runtime_key)
        self.rocketchat = FakeRocketChat()
        self.messages = FakeMessages(
            entries
            or {1: {"source_id": "message-1", "room_source_id": "room-1"}}
        )

    async def resolve_message_source_id(self, message_id: Any) -> str | None:
        entry = await self.messages.get_by_surrogate(message_id)
        return str(entry["source_id"]) if entry else None

    def ok(self, data: Any = None) -> dict[str, Any]:
        return {"status": "ok", "retcode": 0, "data": data, "wording": ""}

    def failed(self, wording: str, retcode: int = 1400) -> dict[str, Any]:
        return {"status": "failed", "retcode": retcode, "data": None, "wording": wording}


class IAmThinkingAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.module = load_plugin_module()
        self.module._TYPING_STOP_GRACE_SECONDS = 0.01
        self.plugins: list[Any] = []

    async def asyncTearDown(self) -> None:
        for plugin in self.plugins:
            await plugin.on_terminate()

    def plugin(self, config: dict[str, Any] | None = None):
        instance = self.module.Plugin(
            SimpleNamespace(plugin_id="rocketcat_plugin_adapt_iamthinking"),
            config or {},
        )
        self.plugins.append(instance)
        return instance

    async def action(
        self,
        plugin,
        runtime: FakeRuntime,
        emoji_id: Any,
        *,
        set_: bool = True,
        message_id: int = 1,
    ) -> dict[str, Any]:
        result = await plugin.handle_onebot_action(
            "set_msg_emoji_like",
            {"message_id": message_id, "emoji_id": emoji_id, "set": set_},
            runtime,
        )
        self.assertIsInstance(result, dict)
        return result

    async def test_default_tool_cycle_keeps_typing_until_done(self):
        plugin = self.plugin()
        runtime = FakeRuntime()

        await self.action(plugin, runtime, 66)
        await self.action(plugin, runtime, 66, set_=False)
        await self.action(plugin, runtime, 270)
        await asyncio.sleep(0.05)
        self.assertEqual([("room-1", True)], runtime.rocketchat.typing)

        await self.action(plugin, runtime, 270, set_=False)
        await self.action(plugin, runtime, 66)
        await self.action(plugin, runtime, 66, set_=False)
        await self.action(plugin, runtime, 74)

        self.assertEqual(("room-1", False), runtime.rocketchat.typing[-1])
        self.assertIn(("message-1", ":tools:", True), runtime.rocketchat.reactions)
        self.assertIn(("message-1", ":sunny:", True), runtime.rocketchat.reactions)

    async def test_error_stops_typing_and_late_done_replaces_error(self):
        plugin = self.plugin()
        runtime = FakeRuntime()

        await self.action(plugin, runtime, 66)
        await self.action(plugin, runtime, 66, set_=False)
        await self.action(plugin, runtime, 264)
        self.assertEqual(("room-1", False), runtime.rocketchat.typing[-1])

        await self.action(plugin, runtime, 264, set_=False)
        await self.action(plugin, runtime, 74)
        self.assertEqual(
            [
                ("message-1", ":octagonal_sign:", True),
                ("message-1", ":octagonal_sign:", False),
                ("message-1", ":sunny:", True),
            ],
            [item for item in runtime.rocketchat.reactions if item[1] != ":heart:"],
        )

    async def test_terminal_state_removal_still_stops_recovered_typing(self):
        plugin = self.plugin()
        runtime = FakeRuntime()

        await self.action(plugin, runtime, 66)
        await self.action(plugin, runtime, 264)
        await self.action(plugin, runtime, 66)
        self.assertEqual(("room-1", True), runtime.rocketchat.typing[-1])
        await self.action(plugin, runtime, 264, set_=False)
        self.assertEqual(("room-1", False), runtime.rocketchat.typing[-1])

    async def test_custom_ids_drive_fixed_rocket_chat_shortcodes(self):
        plugin = self.plugin(
            {
                "thinking_emoji_ids": [101],
                "using_tool_emoji_ids": [202],
                "error_emoji_ids": [303],
                "done_emoji_ids": [404],
            }
        )
        runtime = FakeRuntime()

        for emoji_id, shortcode in (
            (101, ":heart:"),
            (202, ":tools:"),
            (303, ":octagonal_sign:"),
            (404, ":sunny:"),
        ):
            result = await self.action(plugin, runtime, emoji_id)
            self.assertEqual(shortcode, result["data"]["reaction"])

        unknown = await self.action(plugin, runtime, 66)
        self.assertEqual(1404, unknown["retcode"])

    async def test_multiple_ids_are_reference_counted_per_state(self):
        plugin = self.plugin({"thinking_emoji_ids": [66, 67]})
        runtime = FakeRuntime()

        await self.action(plugin, runtime, 66)
        await self.action(plugin, runtime, 67)
        await self.action(plugin, runtime, 66, set_=False)
        self.assertEqual(
            [("message-1", ":heart:", True)],
            runtime.rocketchat.reactions,
        )
        await self.action(plugin, runtime, 67, set_=False)
        await asyncio.sleep(0.05)
        self.assertEqual(
            [
                ("message-1", ":heart:", True),
                ("message-1", ":heart:", False),
            ],
            runtime.rocketchat.reactions,
        )
        self.assertEqual(("room-1", False), runtime.rocketchat.typing[-1])

    async def test_concurrent_messages_share_room_typing_membership(self):
        plugin = self.plugin()
        runtime = FakeRuntime(
            entries={
                1: {"source_id": "message-1", "room_source_id": "room-1"},
                2: {"source_id": "message-2", "room_source_id": "room-1"},
            }
        )

        await self.action(plugin, runtime, 66, message_id=1)
        await self.action(plugin, runtime, 66, message_id=2)
        self.assertEqual([("room-1", True)], runtime.rocketchat.typing)

        await self.action(plugin, runtime, 74, message_id=1)
        self.assertEqual([("room-1", True)], runtime.rocketchat.typing)
        await self.action(plugin, runtime, 74, message_id=2)
        self.assertEqual(("room-1", False), runtime.rocketchat.typing[-1])

    async def test_shortcode_passthrough_does_not_change_typing(self):
        plugin = self.plugin()
        runtime = FakeRuntime()

        result = await self.action(plugin, runtime, ":wave:")
        self.assertEqual("ok", result["status"])
        self.assertEqual([("message-1", ":wave:", True)], runtime.rocketchat.reactions)
        self.assertEqual([], runtime.rocketchat.typing)

        invalid = await self.action(plugin, runtime, ":not valid:")
        self.assertEqual(1404, invalid["retcode"])
        self.assertEqual([("message-1", ":wave:", True)], runtime.rocketchat.reactions)

    def test_conflicting_state_ids_reject_plugin_load(self):
        with self.assertRaisesRegex(ValueError, "不能交叉重复"):
            self.module.Plugin(
                SimpleNamespace(plugin_id="rocketcat_plugin_adapt_iamthinking"),
                {"thinking_emoji_ids": [66], "done_emoji_ids": [66]},
            )
        with self.assertRaisesRegex(ValueError, "只能包含整数"):
            self.module.Plugin(
                SimpleNamespace(plugin_id="rocketcat_plugin_adapt_iamthinking"),
                {"thinking_emoji_ids": [66.5]},
            )

    async def test_unknown_id_is_rejected_and_warning_is_deduplicated(self):
        plugin = self.plugin()
        runtime = FakeRuntime()

        first = await self.action(plugin, runtime, 999)
        second = await self.action(plugin, runtime, 999)
        self.assertEqual(1404, first["retcode"])
        self.assertEqual(1404, second["retcode"])
        self.assertEqual({999}, plugin._unknown_emoji_warnings)

        guessed = await self.action(plugin, runtime, 66.5)
        self.assertEqual(1404, guessed["retcode"])
        self.assertEqual([], runtime.rocketchat.reactions)


if __name__ == "__main__":
    unittest.main()

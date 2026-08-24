from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import aiohttp
from aiohttp import web

from rocketcat_shell.bridge.config import BridgeConfig
from rocketcat_shell.bridge.transports import (
    TransportValidationError,
    create_transport,
    normalize_transport,
    transport_catalog,
)
from rocketcat_shell.bridge.transports.codec import OneBotMessageCodec
from rocketcat_shell.bridge.transports.action_dispatcher import (
    ACTION_QUEUE_CAPACITY,
    OneBotActionDispatcher,
)
from rocketcat_shell.bridge.transports.server_runner import AiohttpServerRunner
from rocketcat_shell.bridge.transports.websocket_peer import (
    WebsocketPeer,
    WebsocketPeerHub,
)
from rocketcat_shell.models import BotRecord, ShellSettings
from rocketcat_shell.registry import BotRegistry
from rocketcat_shell.layout import ProjectLayout
from rocketcat_shell.shell.manager import (
    BotTransportTypeConflictError,
    ShellManager,
)


EXPECTED_TYPES = [
    ("http-server", "HTTP服务器"),
    ("http-client", "HTTP客户端"),
    ("http-sse-server", "HTTP SSE服务器"),
    ("websocket-server", "Websocket服务器"),
    ("websocket-client", "Websocket客户端"),
]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def build_config(type_id: str, settings: dict) -> BridgeConfig:
    config = BridgeConfig.from_mapping(
        {
            "enabled": True,
            "id": f"test-{type_id}",
            "name": type_id,
            "server_url": "http://127.0.0.1:3000",
            "username": "bot",
            "password": "password",
            "onebot_transport": {"type": type_id, "settings": settings},
        }
    )
    config.onebot_self_id = 10001
    return config


async def wait_until(predicate, *, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class TransportCatalogAndMigrationTests(unittest.TestCase):
    def test_catalog_order_labels_and_schema_are_exact(self) -> None:
        catalog = transport_catalog(ShellSettings())
        self.assertEqual(EXPECTED_TYPES, [(item["type"], item["label"]) for item in catalog])
        self.assertTrue(all(item["fields"] for item in catalog))
        websocket_server = next(item for item in catalog if item["type"] == "websocket-server")
        self.assertIn(
            "enableForcePushEvent",
            {field["key"] for field in websocket_server["fields"]},
        )
        for item in catalog[:-1]:
            if any(field["key"] == "access_token" for field in item["fields"]):
                self.assertRegex(item["defaults"]["access_token"], r"^[0-9a-f]{16}$")

    def test_legacy_websocket_fields_migrate_without_loss(self) -> None:
        defaults = ShellSettings()
        normalized = normalize_transport(
            None,
            shell_defaults=defaults,
            legacy_payload={
                "onebot_ws_url": "wss://onebot.example/ws",
                "onebot_access_token": "secret",
                "skip_own_messages": False,
                "debug": True,
            },
        )
        self.assertEqual("websocket-client", normalized["type"])
        self.assertEqual("wss://onebot.example/ws", normalized["settings"]["url"])
        self.assertEqual("secret", normalized["settings"]["access_token"])
        self.assertTrue(normalized["settings"]["report_self_message"])
        self.assertTrue(normalized["settings"]["debug"])
        self.assertEqual(5000, normalized["settings"]["reconnect_interval_ms"])
        self.assertEqual(30000, normalized["settings"]["heartbeat_interval_ms"])

    def test_legacy_explicit_empty_token_does_not_inherit_shell_default(self) -> None:
        defaults = ShellSettings(default_onebot_access_token="shell-default-token")
        normalized = normalize_transport(
            None,
            shell_defaults=defaults,
            legacy_payload={
                "onebot_ws_url": "ws://127.0.0.1:6199/ws/",
                "onebot_access_token": "",
                "skip_own_messages": True,
                "debug": False,
            },
        )
        self.assertEqual("", normalized["settings"]["access_token"])

    def test_v022_edits_override_shared_fields_and_preserve_v023_settings(self) -> None:
        defaults = ShellSettings()
        stored = {
            "type": "websocket-client",
            "settings": {
                "url": "wss://stored.example/ws",
                "message_post_format": "string",
                "report_self_message": False,
                "reconnect_interval_ms": 1700,
                "access_token": "stored-token",
                "debug": False,
                "heartbeat_interval_ms": 9000,
            },
        }
        normalized = normalize_transport(
            stored,
            shell_defaults=defaults,
            legacy_payload={
                "onebot_ws_url": "wss://edited-in-v022.example/ws",
                "onebot_access_token": "edited-token",
                "skip_own_messages": False,
                "debug": True,
            },
        )
        self.assertEqual("wss://edited-in-v022.example/ws", normalized["settings"]["url"])
        self.assertEqual("edited-token", normalized["settings"]["access_token"])
        self.assertTrue(normalized["settings"]["report_self_message"])
        self.assertTrue(normalized["settings"]["debug"])
        self.assertEqual("string", normalized["settings"]["message_post_format"])
        self.assertEqual(1700, normalized["settings"]["reconnect_interval_ms"])
        self.assertEqual(9000, normalized["settings"]["heartbeat_interval_ms"])

        unchanged = normalize_transport(
            stored,
            shell_defaults=defaults,
            legacy_payload={"name": "edited-only"},
        )
        self.assertEqual(stored, unchanged)

    def test_registry_round_trip_absorbs_v022_edits_without_losing_new_fields(self) -> None:
        defaults = ShellSettings()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bots_path = root / "bots.json"
            transports_path = root / "onebot_transports.json"
            bots_path.write_text(
                json.dumps(
                    {
                        "bots": [
                            {
                                "id": "bot-a",
                                "name": "A",
                                "enabled": False,
                                "onebot_ws_url": "wss://original.example/ws",
                                "onebot_access_token": "original-token",
                                "skip_own_messages": True,
                                "debug": False,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            registry = BotRegistry(bots_path, transports_path)
            migrated = registry.load(defaults=defaults)[0]
            self.assertTrue(transports_path.is_file())
            migrated.onebot_transport_settings.update(
                message_post_format="string",
                reconnect_interval_ms=1700,
                heartbeat_interval_ms=9000,
            )
            registry.save([migrated])

            legacy_payload = json.loads(bots_path.read_text(encoding="utf-8"))
            legacy_bot = legacy_payload["bots"][0]
            legacy_bot.update(
                onebot_ws_url="wss://edited.example/ws",
                onebot_access_token="edited-token",
                skip_own_messages=False,
                debug=True,
            )
            bots_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

            restored = registry.load(defaults=defaults)[0]
            self.assertEqual("wss://edited.example/ws", restored.onebot_transport_settings["url"])
            self.assertEqual("edited-token", restored.onebot_transport_settings["access_token"])
            self.assertTrue(restored.onebot_transport_settings["report_self_message"])
            self.assertTrue(restored.onebot_transport_settings["debug"])
            self.assertEqual("string", restored.onebot_transport_settings["message_post_format"])
            self.assertEqual(1700, restored.onebot_transport_settings["reconnect_interval_ms"])
            self.assertEqual(9000, restored.onebot_transport_settings["heartbeat_interval_ms"])

            registry.save([restored])
            saved_legacy = json.loads(bots_path.read_text(encoding="utf-8"))["bots"][0]
            self.assertEqual("wss://edited.example/ws", saved_legacy["onebot_ws_url"])
            self.assertEqual("edited-token", saved_legacy["onebot_access_token"])
            self.assertFalse(saved_legacy["skip_own_messages"])
            self.assertTrue(saved_legacy["debug"])

    def test_validation_rejects_unknown_and_invalid_settings(self) -> None:
        defaults = ShellSettings()
        with self.assertRaisesRegex(TransportValidationError, "未知 OneBot 网络类型"):
            normalize_transport(
                {"type": "future-transport", "settings": {}},
                shell_defaults=defaults,
            )
        with self.assertRaisesRegex(TransportValidationError, "不能大于 65535"):
            normalize_transport(
                {
                    "type": "http-server",
                    "settings": {"host": "127.0.0.1", "port": 70000},
                },
                shell_defaults=defaults,
            )
        with self.assertRaisesRegex(TransportValidationError, "未知设置项"):
            normalize_transport(
                {
                    "type": "http-client",
                    "settings": {"url": "http://localhost:8080", "surprise": True},
                },
                shell_defaults=defaults,
            )
        with self.assertRaisesRegex(TransportValidationError, "Host格式无效"):
            normalize_transport(
                {
                    "type": "http-server",
                    "settings": {"host": "127.0.0.1:3000", "port": 3000},
                },
                shell_defaults=defaults,
            )
        wildcard = normalize_transport(
            {
                "type": "websocket-server",
                "settings": {"host": "*", "port": 3001},
            },
            shell_defaults=defaults,
        )
        self.assertEqual("0.0.0.0", wildcard["settings"]["host"])

    def test_independent_transport_file_survives_legacy_projection(self) -> None:
        defaults = ShellSettings()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bots_path = root / "bots.json"
            transports_path = root / "onebot_transports.json"
            bot = BotRecord.from_mapping(
                {"id": "bot-a", "name": "A", "enabled": False}, defaults=defaults
            )
            bot.apply_onebot_transport(
                normalize_transport(
                    {
                        "type": "http-server",
                        "settings": {"host": "127.0.0.1", "port": 3100},
                    },
                    shell_defaults=defaults,
                )
            )
            registry = BotRegistry(bots_path, transports_path)
            registry.save([bot])

            legacy_projection = json.loads(bots_path.read_text(encoding="utf-8"))["bots"][0]
            self.assertEqual(
                "ws://127.0.0.1:1/__rocketcat_disabled_transport__",
                legacy_projection["onebot_ws_url"],
            )
            legacy_projection["name"] = "edited-by-v022"
            legacy_projection["onebot_ws_url"] = "ws://must-not-override.example/ws"
            legacy_projection["onebot_access_token"] = "must-not-override"
            bots_path.write_text(
                json.dumps({"bots": [legacy_projection]}, ensure_ascii=False),
                encoding="utf-8",
            )
            restored = registry.load(defaults=defaults)[0]
            self.assertEqual("edited-by-v022", restored.name)
            self.assertEqual("http-server", restored.onebot_transport_type)
            self.assertEqual(3100, restored.onebot_transport_settings["port"])

    def test_all_five_catalog_defaults_round_trip_through_tagged_union(self) -> None:
        defaults = ShellSettings()
        for item in transport_catalog(defaults):
            normalized = normalize_transport(
                {"type": item["type"], "settings": item["defaults"]},
                shell_defaults=defaults,
            )
            self.assertEqual(item["type"], normalized["type"])
            self.assertEqual(item["defaults"], normalized["settings"])

    def test_registry_restores_both_files_when_second_write_fails(self) -> None:
        defaults = ShellSettings()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            registry = BotRegistry(root / "bots.json", root / "onebot_transports.json")
            original = BotRecord.from_mapping(
                {"id": "bot-a", "name": "before", "enabled": False},
                defaults=defaults,
            )
            registry.save([original])
            before = {
                registry.path: registry.path.read_bytes(),
                registry.transport_path: registry.transport_path.read_bytes(),
            }
            changed = BotRecord.from_mapping(
                {"id": "bot-a", "name": "after", "enabled": False},
                defaults=defaults,
            )
            from rocketcat_shell import registry as registry_module

            real_write_json = registry_module.write_json
            calls = 0

            def fail_second_write(path, payload):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated transport write failure")
                return real_write_json(path, payload)

            with mock.patch.object(registry_module, "write_json", side_effect=fail_second_write):
                with self.assertRaisesRegex(OSError, "simulated"):
                    registry.save([changed])
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_unknown_transport_file_version_is_preserved_and_ignored(self) -> None:
        defaults = ShellSettings()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bots_path = root / "bots.json"
            transports_path = root / "onebot_transports.json"
            bots_path.write_text(
                json.dumps(
                    {
                        "bots": [
                            {
                                "id": "bot-a",
                                "name": "A",
                                "enabled": False,
                                "onebot_ws_url": "ws://legacy.example/ws",
                                "onebot_access_token": "legacy-token",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            transports_path.write_text(
                json.dumps(
                    {
                        "format_version": 99,
                        "transports": {
                            "bot-a": {
                                "type": "http-server",
                                "settings": {"host": "127.0.0.1", "port": 3100},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            before = transports_path.read_bytes()

            registry = BotRegistry(bots_path, transports_path)
            restored = registry.load(defaults=defaults)[0]

            self.assertFalse(registry.loaded_transport_format_supported)
            self.assertFalse(registry.loaded_transport_state_valid)
            self.assertEqual("websocket-client", restored.onebot_transport_type)
            self.assertEqual(
                "ws://legacy.example/ws",
                restored.onebot_transport_settings["url"],
            )
            self.assertEqual(before, transports_path.read_bytes())

    def test_invalid_transport_settings_are_not_marked_safe_for_startup_rewrite(self) -> None:
        defaults = ShellSettings()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bots_path = root / "bots.json"
            transports_path = root / "onebot_transports.json"
            bots_path.write_text(
                json.dumps({"bots": [{"id": "bot-a", "name": "A"}]}),
                encoding="utf-8",
            )
            transports_path.write_text(
                json.dumps(
                    {
                        "format_version": 1,
                        "transports": {
                            "bot-a": {
                                "type": "http-server",
                                "settings": {"host": "127.0.0.1", "port": 70000},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            before = transports_path.read_bytes()

            registry = BotRegistry(bots_path, transports_path)
            restored = registry.load(defaults=defaults)[0]

            self.assertFalse(registry.loaded_transport_state_valid)
            self.assertEqual("http-server", restored.onebot_transport_type)
            self.assertEqual(70000, restored.onebot_transport_settings["port"])
            self.assertEqual(before, transports_path.read_bytes())

    def test_invalid_legacy_websocket_is_preserved_for_webui_repair(self) -> None:
        defaults = ShellSettings()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            bots_path = root / "bots.json"
            transports_path = root / "onebot_transports.json"
            bots_path.write_text(
                json.dumps(
                    {
                        "bots": [
                            {
                                "id": "bot-a",
                                "name": "A",
                                "enabled": False,
                                "onebot_ws_url": "not-a-websocket-url",
                                "onebot_access_token": "legacy-token",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            registry = BotRegistry(bots_path, transports_path)
            restored = registry.load(defaults=defaults)[0]

            self.assertFalse(registry.loaded_transport_state_valid)
            self.assertEqual("websocket-client", restored.onebot_transport_type)
            self.assertEqual(
                "not-a-websocket-url",
                restored.onebot_transport_settings["url"],
            )
            self.assertFalse(transports_path.exists())


class CodecTests(unittest.TestCase):
    def test_array_and_cq_string_round_trip(self) -> None:
        segments = [
            {"type": "text", "data": {"text": "a[b]&c"}},
            {"type": "image", "data": {"file": "a,b.png"}},
        ]
        encoded = OneBotMessageCodec("string").encode_event({"message": segments})
        self.assertEqual("a&#91;b&#93;&amp;c[CQ:image,file=a&#44;b.png]", encoded["message"])
        decoded = OneBotMessageCodec("array").normalize_action_params(
            {"message": encoded["message"]}
        )
        self.assertEqual(segments, decoded["message"])
        self.assertEqual(segments, OneBotMessageCodec("array").encode_event({"message": segments})["message"])

    def test_string_format_converts_nested_action_response_messages(self) -> None:
        segments = [{"type": "text", "data": {"text": "hello"}}]
        dispatcher = OneBotActionDispatcher(
            mock.AsyncMock(),
            codec=OneBotMessageCodec("string"),
            owner="string-response",
        )
        payload = dispatcher.response_payload(
            {
                "status": "ok",
                "retcode": 0,
                "data": {"message": segments, "nested": [{"message": segments}]},
            },
            "echo",
        )
        self.assertEqual("hello", payload["data"]["message"])
        self.assertEqual("hello", payload["data"]["nested"][0]["message"])


class ActionDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_full_returns_explicit_busy_response(self) -> None:
        async def handler(_action: str, _params: dict) -> dict:
            return {"status": "ok", "retcode": 0, "data": {}}

        dispatcher = OneBotActionDispatcher(
            handler,
            codec=OneBotMessageCodec("array"),
            owner="busy-test",
        )
        responses: list[dict] = []

        async def respond(payload: dict) -> None:
            responses.append(payload)

        for index in range(ACTION_QUEUE_CAPACITY):
            self.assertTrue(await dispatcher.submit("get_status", {}, index, respond))
        self.assertFalse(await dispatcher.submit("get_status", {}, "busy", respond))
        self.assertEqual(1503, responses[-1]["retcode"])
        self.assertEqual("busy", responses[-1]["echo"])
        self.assertEqual(ACTION_QUEUE_CAPACITY, dispatcher.discard_pending())

    async def test_action_timeout_returns_explicit_failure_and_releases_worker(self) -> None:
        async def handler(_action: str, _params: dict) -> dict:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        dispatcher = OneBotActionDispatcher(
            handler,
            codec=OneBotMessageCodec("array"),
            owner="timeout-test",
        )
        dispatcher.start()
        try:
            with mock.patch(
                "rocketcat_shell.bridge.transports.action_dispatcher.ACTION_TIMEOUT_SECONDS",
                0.01,
            ):
                response = await asyncio.wait_for(
                    dispatcher.execute("get_status", {}, "timeout"),
                    timeout=1,
                )
            self.assertEqual(1504, response["retcode"])
            self.assertEqual("timeout", response["echo"])
            self.assertEqual(1, dispatcher.diagnostic_snapshot()["timed_out"])
            self.assertEqual(0, dispatcher.active_count)
        finally:
            await dispatcher.stop()

    async def test_full_peer_queue_closes_slow_client_instead_of_losing_response(self) -> None:
        async def handler(_action: str, _params: dict) -> dict:
            return {"status": "ok", "retcode": 0, "data": {}}

        class FakeWebsocket:
            def __init__(self) -> None:
                self.closed = False
                self.close_code = None

            async def close(self, *, code=1000, message=b"") -> None:
                self.closed = True
                self.close_code = code

        dispatcher = OneBotActionDispatcher(
            handler,
            codec=OneBotMessageCodec("array"),
            owner="slow-peer",
        )
        dispatcher.start()
        websocket = FakeWebsocket()
        peer = WebsocketPeer(
            websocket=websocket,
            event_enabled=True,
            queue=asyncio.Queue(maxsize=1),
        )
        peer.queue.put_nowait({"already": "full"})
        hub = WebsocketPeerHub(
            owner="slow-peer",
            token="",
            self_id=1,
            heartbeat_interval_ms=0,
            queue_capacity=1,
            codec=OneBotMessageCodec("array"),
            dispatcher=dispatcher,
            max_msg_size=1024,
        )
        try:
            await hub._handle_message(
                peer,
                json.dumps({"action": "get_status", "params": {}, "echo": "x"}),
            )
            await wait_until(lambda: websocket.closed)
            self.assertEqual(1013, websocket.close_code)
            self.assertEqual("action response queue full", hub.last_error)
        finally:
            peer.queue.get_nowait()
            peer.queue.task_done()
            await dispatcher.stop()


class TransportManagerTests(unittest.IsolatedAsyncioTestCase):
    def build_manager(self, root: Path) -> ShellManager:
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

    async def test_compact_bot_list_excludes_secrets_and_detail_remains_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = self.build_manager(Path(temporary_directory))
            bot = BotRecord.from_mapping(
                {
                    "id": "bot-a",
                    "name": "A",
                    "enabled": False,
                    "password": "rocket-secret",
                    "e2ee_password": "e2ee-secret",
                },
                defaults=manager.settings,
            )
            bot.apply_onebot_transport(
                normalize_transport(
                    {
                        "type": "http-client",
                        "settings": {
                            "url": "http://localhost:8080",
                            "access_token": "onebot-secret",
                        },
                    },
                    shell_defaults=manager.settings,
                )
            )
            manager.bots = [bot]

            compact = (await manager.list_bots(compact=True))[0]
            compact_text = json.dumps(compact, ensure_ascii=False)
            self.assertNotIn("rocket-secret", compact_text)
            self.assertNotIn("e2ee-secret", compact_text)
            self.assertNotIn("onebot-secret", compact_text)
            self.assertEqual("HTTP客户端", compact["onebot_transport_label"])
            self.assertEqual("http://localhost:8080", compact["onebot_transport"]["settings"]["url"])

            detail = await manager.get_bot("bot-a")
            self.assertEqual("rocket-secret", detail["password"])
            self.assertEqual(
                "onebot-secret",
                detail["onebot_transport"]["settings"]["access_token"],
            )

    async def test_transport_type_is_immutable_after_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = self.build_manager(Path(temporary_directory))
            bot = BotRecord.from_mapping(
                {"id": "bot-a", "name": "A", "enabled": False},
                defaults=manager.settings,
            )
            manager.bots = [bot]
            with self.assertRaisesRegex(BotTransportTypeConflictError, "不可修改"):
                await manager.update_bot(
                    "bot-a",
                    {
                        "onebot_transport": {
                            "type": "http-client",
                            "settings": {"url": "http://localhost:8080"},
                        }
                    },
                )

    async def test_legacy_api_edit_preserves_v023_only_websocket_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = self.build_manager(Path(temporary_directory))
            existing = BotRecord.from_mapping(
                {
                    "id": "bot-a",
                    "name": "A",
                    "enabled": False,
                    "onebot_ws_url": "wss://before.example/ws",
                    "onebot_access_token": "before-token",
                },
                defaults=manager.settings,
            )
            existing.onebot_transport_settings.update(
                message_post_format="string",
                reconnect_interval_ms=1700,
                heartbeat_interval_ms=9000,
            )
            updated = manager._normalize_bot_payload(
                {
                    "onebot_ws_url": "wss://after.example/ws",
                    "onebot_access_token": "after-token",
                    "skip_own_messages": False,
                    "debug": True,
                },
                forced_id="bot-a",
                existing=existing,
            )
            self.assertEqual(
                "wss://after.example/ws",
                updated.onebot_transport_settings["url"],
            )
            self.assertEqual(
                "after-token",
                updated.onebot_transport_settings["access_token"],
            )
            self.assertTrue(updated.onebot_transport_settings["report_self_message"])
            self.assertTrue(updated.onebot_transport_settings["debug"])
            self.assertEqual(
                "string",
                updated.onebot_transport_settings["message_post_format"],
            )
            self.assertEqual(
                1700,
                updated.onebot_transport_settings["reconnect_interval_ms"],
            )
            self.assertEqual(
                9000,
                updated.onebot_transport_settings["heartbeat_interval_ms"],
            )

    async def test_enabled_listener_conflicts_with_bot_and_webui(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = self.build_manager(Path(temporary_directory))
            existing = BotRecord.from_mapping(
                {
                    "id": "bot-a",
                    "name": "A",
                    "enabled": True,
                    "server_url": "http://localhost:3000",
                    "username": "a",
                    "password": "secret",
                },
                defaults=manager.settings,
            )
            existing.apply_onebot_transport(
                normalize_transport(
                    {
                        "type": "http-server",
                        "settings": {"host": "0.0.0.0", "port": 3100},
                    },
                    shell_defaults=manager.settings,
                )
            )
            manager.bots = [existing]
            candidate = BotRecord.from_mapping(
                {
                    "id": "bot-b",
                    "name": "B",
                    "enabled": True,
                    "server_url": "http://localhost:3000",
                    "username": "b",
                    "password": "secret",
                },
                defaults=manager.settings,
            )
            candidate.apply_onebot_transport(
                normalize_transport(
                    {
                        "type": "websocket-server",
                        "settings": {"host": "127.0.0.1", "port": 3100},
                    },
                    shell_defaults=manager.settings,
                )
            )
            self.assertRegex(
                "；".join(manager._validate_bot(candidate, exclude_bot_id=None)),
                "与 Bot A 冲突",
            )
            candidate.onebot_transport_settings["port"] = manager.settings.webui_port
            self.assertRegex(
                "；".join(manager._validate_bot(candidate, exclude_bot_id=None)),
                "与 WebUI 冲突",
            )

    async def test_import_accepts_all_five_tagged_unions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = self.build_manager(Path(temporary_directory))
            catalog = transport_catalog(manager.settings)
            raw_bots = [
                {
                    "id": f"bot-{index}",
                    "name": item["label"],
                    "enabled": False,
                    "onebot_transport": {
                        "type": item["type"],
                        "settings": item["defaults"],
                    },
                }
                for index, item in enumerate(catalog, start=1)
            ]

            restored = manager._build_import_bots(
                raw_bots,
                defaults=manager.settings,
                webui_port=manager.settings.webui_port,
            )

            self.assertEqual(
                [item[0] for item in EXPECTED_TYPES],
                [bot.onebot_transport_type for bot in restored],
            )

    async def test_tagged_websocket_import_wins_over_legacy_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = self.build_manager(Path(temporary_directory))
            restored = manager._build_import_bots(
                [
                    {
                        "id": "bot-a",
                        "name": "A",
                        "enabled": False,
                        "onebot_ws_url": "wss://legacy.example/ws",
                        "onebot_access_token": "legacy-token",
                        "skip_own_messages": True,
                        "debug": False,
                        "onebot_transport": {
                            "type": "websocket-client",
                            "settings": {
                                "url": "wss://tagged.example/ws",
                                "message_post_format": "string",
                                "report_self_message": True,
                                "reconnect_interval_ms": 1700,
                                "access_token": "tagged-token",
                                "debug": True,
                                "heartbeat_interval_ms": 9000,
                            },
                        },
                    }
                ],
                defaults=manager.settings,
                webui_port=manager.settings.webui_port,
            )[0]
            self.assertEqual("wss://tagged.example/ws", restored.onebot_transport_settings["url"])
            self.assertEqual("tagged-token", restored.onebot_transport_settings["access_token"])
            self.assertEqual("string", restored.onebot_transport_settings["message_post_format"])
            self.assertTrue(restored.onebot_transport_settings["report_self_message"])
            self.assertTrue(restored.onebot_transport_settings["debug"])

    async def test_invalid_transport_import_is_rejected_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = self.build_manager(Path(temporary_directory))
            existing = BotRecord.from_mapping(
                {"id": "bot-a", "name": "A", "enabled": False},
                defaults=manager.settings,
            )
            manager.bots = [existing]
            manager._persist_after_bot_change_locked()
            tracked_paths = [
                manager.layout.shell_settings_path,
                manager.layout.bot_registry_path,
                manager.layout.onebot_transports_path,
            ]
            before = {path: path.read_bytes() for path in tracked_paths}
            payload = {
                "Is rocketcat config": True,
                "shell_settings": manager.settings.to_mapping(),
                "bots": [
                    {
                        **existing.to_mapping(),
                        "onebot_transport": {
                            "type": "http-server",
                            "settings": {"host": "127.0.0.1", "port": 70000},
                        },
                    }
                ],
                "plugin_configs": {},
            }

            with self.assertRaisesRegex(ValueError, "不能大于 65535"):
                await manager.import_configuration(payload)

            self.assertEqual(
                before,
                {path: path.read_bytes() for path in tracked_paths},
            )


class TransportIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.actions: list[tuple[str, dict]] = []

    async def action_handler(self, action: str, params: dict) -> dict:
        self.actions.append((action, params))
        return {"status": "ok", "retcode": 0, "data": {"action": action}}

    async def test_http_server_auth_cors_get_and_post_actions(self) -> None:
        port = free_port()
        config = build_config(
            "http-server",
            {
                "host": "127.0.0.1",
                "port": port,
                "enable_cors": True,
                "enable_websocket": False,
                "message_post_format": "array",
                "access_token": "secret",
                "debug": False,
            },
        )
        transport = create_transport(config, self.action_handler)
        await transport.start()
        try:
            self.assertTrue(transport.connected)
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{port}/get_status") as response:
                    self.assertEqual(403, response.status)
                async with session.get(
                    f"http://127.0.0.1:{port}/get_status?access_token=secret&echo=e1"
                ) as response:
                    payload = await response.json()
                    self.assertEqual(200, response.status)
                    self.assertEqual("e1", payload["echo"])
                    self.assertEqual("*", response.headers["Access-Control-Allow-Origin"])
                async with session.post(
                    f"http://127.0.0.1:{port}/send_msg",
                    headers={"Authorization": "Bearer secret"},
                    json={"message": "hello", "user_id": 1, "echo": "e2"},
                ) as response:
                    payload = await response.json()
                    self.assertEqual("e2", payload["echo"])
                async with session.post(
                    f"http://127.0.0.1:{port}/get_version_info",
                    headers={
                        "Authorization": "Bearer secret",
                        "Content-Type": "application/octet-stream",
                    },
                    data=json.dumps({"echo": "e3"}).encode(),
                ) as response:
                    payload = await response.json()
                    self.assertEqual(200, response.status)
                    self.assertEqual("e3", payload["echo"])
            self.assertEqual(
                ["get_status", "send_msg", "get_version_info"],
                [item[0] for item in self.actions],
            )
            self.assertNotIn("access_token", self.actions[0][1])
        finally:
            await transport.stop()

    async def test_server_bind_failure_is_reported_without_stopping_transport_owner(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = int(occupied.getsockname()[1])
            config = build_config(
                "websocket-server",
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "message_post_format": "array",
                    "report_self_message": False,
                    "enableForcePushEvent": True,
                    "heartbeat_interval_ms": 0,
                    "access_token": "secret",
                    "debug": False,
                },
            )
            transport = create_transport(config, self.action_handler)
            await transport.start()
            try:
                snapshot = transport.build_diagnostic_snapshot()
                self.assertEqual("端口错误", snapshot["onebot_transport_status"])
                self.assertFalse(snapshot["onebot_listener_active"])
                self.assertTrue(transport._running)
            finally:
                await transport.stop()

    async def test_http_sse_server_streams_ordered_events_and_cleans_up(self) -> None:
        port = free_port()
        config = build_config(
            "http-sse-server",
            {
                "host": "127.0.0.1",
                "port": port,
                "enable_cors": True,
                "enable_websocket": False,
                "message_post_format": "array",
                "report_self_message": False,
                "access_token": "secret",
                "debug": False,
            },
        )
        transport = create_transport(config, self.action_handler)
        await transport.start()
        try:
            async with aiohttp.ClientSession() as session:
                response = await session.get(
                    f"http://127.0.0.1:{port}/_events?access_token=secret"
                )
                try:
                    self.assertEqual(200, response.status)
                    self.assertTrue((await response.content.readline()).startswith(b":"))
                    await response.content.readline()
                    await transport.emit_event({"post_type": "message", "message_id": 1})
                    await transport.emit_event({"post_type": "message", "message_id": 2})
                    first = json.loads((await response.content.readline()).decode().removeprefix("data: "))
                    await response.content.readline()
                    second = json.loads((await response.content.readline()).decode().removeprefix("data: "))
                    self.assertEqual([1, 2], [first["message_id"], second["message_id"]])
                finally:
                    response.close()
            await wait_until(
                lambda: transport.build_diagnostic_snapshot()["onebot_client_count"] == 0,
                timeout=16.0,
            )
        finally:
            await transport.stop()

    async def test_http_server_variants_support_optional_same_port_websocket(self) -> None:
        for type_id in ("http-server", "http-sse-server"):
            with self.subTest(type_id=type_id):
                port = free_port()
                settings = {
                    "host": "127.0.0.1",
                    "port": port,
                    "enable_cors": True,
                    "enable_websocket": True,
                    "message_post_format": "array",
                    "access_token": "secret",
                    "debug": False,
                }
                if type_id == "http-sse-server":
                    settings["report_self_message"] = False
                transport = create_transport(
                    build_config(type_id, settings),
                    self.action_handler,
                )
                await transport.start()
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.ws_connect(
                            f"http://127.0.0.1:{port}/event?access_token=secret"
                        ) as websocket:
                            lifecycle = await websocket.receive_json(timeout=2)
                            self.assertEqual(
                                "lifecycle",
                                lifecycle["meta_event_type"],
                            )
                            await transport.emit_event(
                                {"post_type": "notice", "notice_type": type_id}
                            )
                            event = await websocket.receive_json(timeout=2)
                            self.assertEqual(type_id, event["notice_type"])
                finally:
                    await transport.stop()

    async def test_http_client_signs_events_and_executes_supported_quick_reply(self) -> None:
        port = free_port()
        received: list[tuple[bytes, dict[str, str]]] = []

        async def receiver(request: web.Request) -> web.Response:
            body = await request.read()
            received.append((body, dict(request.headers)))
            return web.json_response({"reply": "quick reply", "delete": True})

        app = web.Application()
        app.router.add_post("/events", receiver)
        server = AiohttpServerRunner(app, host="127.0.0.1", port=port)
        await server.start()
        config = build_config(
            "http-client",
            {
                "url": f"http://127.0.0.1:{port}/events",
                "message_post_format": "array",
                "report_self_message": False,
                "access_token": "secret",
                "debug": False,
            },
        )
        transport = create_transport(config, self.action_handler)
        await transport.start()
        try:
            await transport.emit_event(
                {"post_type": "message", "message_type": "private", "user_id": 42, "message": []}
            )
            await asyncio.wait_for(transport._outgoing.join(), timeout=2)
            self.assertEqual(1, len(received))
            body, headers = received[0]
            expected = hmac.new(b"secret", body, hashlib.sha1).hexdigest()
            self.assertEqual(f"sha1={expected}", headers["X-Signature"])
            self.assertEqual("10001", headers["X-Self-ID"])
            self.assertIn(("send_msg", {"message": "quick reply", "message_type": "private", "user_id": 42}), self.actions)
            self.assertEqual(1, transport.build_diagnostic_snapshot()["onebot_unsupported_quick_action_count"])
        finally:
            await transport.stop()
            await server.stop()

    async def test_http_client_failure_is_diagnostic_only_and_keeps_running(self) -> None:
        port = free_port()

        async def receiver(_request: web.Request) -> web.Response:
            return web.Response(status=500, text="simulated failure")

        app = web.Application()
        app.router.add_post("/events", receiver)
        server = AiohttpServerRunner(app, host="127.0.0.1", port=port)
        await server.start()
        transport = create_transport(
            build_config(
                "http-client",
                {
                    "url": f"http://127.0.0.1:{port}/events",
                    "message_post_format": "array",
                    "report_self_message": False,
                    "access_token": "secret",
                    "debug": False,
                },
            ),
            self.action_handler,
        )
        await transport.start()
        try:
            await transport.emit_event({"post_type": "notice"})
            await asyncio.wait_for(transport._outgoing.join(), timeout=2)
            snapshot = transport.build_diagnostic_snapshot()
            self.assertEqual(1, snapshot["onebot_delivery_failure_count"])
            self.assertIn("投递失败", snapshot["onebot_last_delivery_status"])
            self.assertTrue(transport._running)
            self.assertTrue(transport.connected)
        finally:
            await transport.stop()
            await server.stop()

    async def test_websocket_server_path_roles_lifecycle_and_action_echo(self) -> None:
        port = free_port()
        config = build_config(
            "websocket-server",
            {
                "host": "127.0.0.1",
                "port": port,
                "message_post_format": "array",
                "report_self_message": False,
                "enableForcePushEvent": True,
                "heartbeat_interval_ms": 100,
                "access_token": "secret",
                "debug": False,
            },
        )
        transport = create_transport(config, self.action_handler)
        await transport.start()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    f"http://127.0.0.1:{port}/event"
                ) as denied_socket:
                    denied = await denied_socket.receive_json(timeout=2)
                    self.assertEqual(1403, denied["retcode"])
                async with session.ws_connect(
                    f"http://127.0.0.1:{port}/api",
                    headers={"Authorization": "Bearer secret"},
                ) as header_socket:
                    await header_socket.send_json(
                        {"action": "get_status", "params": {}, "echo": "header"}
                    )
                    self.assertEqual(
                        "header",
                        (await header_socket.receive_json(timeout=2))["echo"],
                    )
                async with session.ws_connect(
                    f"http://127.0.0.1:{port}/api?access_token=secret"
                ) as api_socket:
                    await api_socket.send_json({"action": "get_status", "params": {}, "echo": "api"})
                    api_response = await api_socket.receive_json(timeout=2)
                    self.assertEqual("api", api_response["echo"])
                async with session.ws_connect(
                    f"http://127.0.0.1:{port}/event?access_token=secret"
                ) as first_event_socket, session.ws_connect(
                    f"http://127.0.0.1:{port}/event?access_token=secret"
                ) as second_event_socket:
                    first_lifecycle = await first_event_socket.receive_json(timeout=2)
                    second_lifecycle = await second_event_socket.receive_json(timeout=2)
                    self.assertEqual("lifecycle", first_lifecycle["meta_event_type"])
                    self.assertEqual("lifecycle", second_lifecycle["meta_event_type"])
                    first_heartbeat = await first_event_socket.receive_json(timeout=2)
                    second_heartbeat = await second_event_socket.receive_json(timeout=2)
                    self.assertEqual("heartbeat", first_heartbeat["meta_event_type"])
                    self.assertEqual("heartbeat", second_heartbeat["meta_event_type"])
                    await transport.emit_event(
                        {"post_type": "notice", "notice_type": "test"}
                    )
                    async def receive_notice(socket):
                        for _attempt in range(5):
                            payload = await socket.receive_json(timeout=2)
                            if payload.get("notice_type") == "test":
                                return payload
                        self.fail("notice event was not delivered before heartbeat limit")

                    first_event = await receive_notice(first_event_socket)
                    second_event = await receive_notice(second_event_socket)
                    self.assertEqual("test", first_event["notice_type"])
                    self.assertEqual("test", second_event["notice_type"])
        finally:
            await transport.stop()

    async def test_websocket_client_headers_actions_events_and_lifecycle(self) -> None:
        port = free_port()
        headers: dict[str, str] = {}
        messages: list[dict] = []
        connected = asyncio.Event()
        got_action_response = asyncio.Event()
        got_event = asyncio.Event()

        async def upstream(request: web.Request) -> web.StreamResponse:
            headers.update(dict(request.headers))
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            connected.set()
            await websocket.send_json({"action": "get_status", "params": {}, "echo": "upstream"})
            async for message in websocket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                messages.append(payload)
                if payload.get("echo") == "upstream":
                    got_action_response.set()
                if payload.get("notice_type") == "from-rocketchat":
                    got_event.set()
                if got_action_response.is_set() and got_event.is_set():
                    break
            await websocket.close()
            return websocket

        app = web.Application()
        app.router.add_get("/ws", upstream)
        server = AiohttpServerRunner(app, host="127.0.0.1", port=port)
        await server.start()
        config = build_config(
            "websocket-client",
            {
                "url": f"ws://127.0.0.1:{port}/ws",
                "message_post_format": "array",
                "report_self_message": False,
                "reconnect_interval_ms": 100,
                "heartbeat_interval_ms": 0,
                "access_token": "secret",
                "debug": False,
            },
        )
        transport = create_transport(config, self.action_handler)
        await transport.start()
        try:
            await asyncio.wait_for(connected.wait(), timeout=2)
            await wait_until(lambda: transport.connected)
            await transport.emit_event({"post_type": "notice", "notice_type": "from-rocketchat"})
            await asyncio.wait_for(got_action_response.wait(), timeout=2)
            await asyncio.wait_for(got_event.wait(), timeout=2)
            self.assertEqual("Bearer secret", headers["Authorization"])
            self.assertEqual("10001", headers["X-Self-ID"])
            self.assertEqual("Universal", headers["X-Client-Role"])
            self.assertEqual("OneBot/11", headers["User-Agent"])
            self.assertEqual("lifecycle", messages[0]["meta_event_type"])
        finally:
            await transport.stop()
            await server.stop()

    async def test_websocket_client_recovers_when_upstream_appears_later(self) -> None:
        port = free_port()
        connected = asyncio.Event()
        headers: dict[str, str] = {}

        async def upstream(request: web.Request) -> web.StreamResponse:
            headers.update(dict(request.headers))
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            connected.set()
            async for _message in websocket:
                pass
            return websocket

        config = build_config(
            "websocket-client",
            {
                "url": f"ws://127.0.0.1:{port}/ws",
                "message_post_format": "array",
                "report_self_message": False,
                "reconnect_interval_ms": 100,
                "heartbeat_interval_ms": 0,
                "access_token": "",
                "debug": False,
            },
        )
        transport = create_transport(config, self.action_handler)
        await transport.start()
        server = None
        try:
            await wait_until(
                lambda: transport.build_diagnostic_snapshot()[
                    "onebot_waiting_for_upstream"
                ],
                timeout=5,
            )
            app = web.Application()
            app.router.add_get("/ws", upstream)
            server = AiohttpServerRunner(app, host="127.0.0.1", port=port)
            await server.start()
            await asyncio.wait_for(connected.wait(), timeout=2)
            await wait_until(lambda: transport.connected)
            self.assertEqual(
                "已连接",
                transport.build_diagnostic_snapshot()["onebot_transport_status"],
            )
            self.assertIn("Authorization", headers)
            self.assertTrue(headers["Authorization"].startswith("Bearer"))
        finally:
            await transport.stop()
            if server is not None:
                await server.stop()

    async def test_websocket_client_does_not_send_old_action_response_after_reconnect(self) -> None:
        action_started = asyncio.Event()
        release_action = asyncio.Event()

        async def delayed_handler(_action: str, _params: dict) -> dict:
            action_started.set()
            await release_action.wait()
            return {"status": "ok", "retcode": 0, "data": {}}

        class FakeWebsocket:
            def __init__(self, incoming: list[dict] | None = None) -> None:
                self.closed = False
                self.sent: list[dict] = []
                self._incoming = list(incoming or [])

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._incoming:
                    raise StopAsyncIteration
                return SimpleNamespace(
                    type=aiohttp.WSMsgType.TEXT,
                    data=json.dumps(self._incoming.pop(0)),
                )

            async def send_str(self, payload: str) -> None:
                self.sent.append(json.loads(payload))

            async def close(self, *, code=1000) -> None:
                self.closed = True

        config = build_config(
            "websocket-client",
            {
                "url": "ws://127.0.0.1:1/ws",
                "message_post_format": "array",
                "report_self_message": False,
                "reconnect_interval_ms": 100,
                "heartbeat_interval_ms": 0,
                "access_token": "",
                "debug": False,
            },
        )
        transport = create_transport(config, delayed_handler)
        origin = FakeWebsocket(
            [{"action": "get_status", "params": {}, "echo": "old-session"}]
        )
        replacement = FakeWebsocket()
        transport._running = True
        transport._dispatcher.start()
        transport._ws = origin
        try:
            await transport._listen_loop(origin)
            await asyncio.wait_for(action_started.wait(), timeout=2)
            transport._ws = replacement
            release_action.set()
            await asyncio.wait_for(transport._dispatcher.queue.join(), timeout=2)
            self.assertEqual([], origin.sent)
            self.assertEqual([], replacement.sent)
        finally:
            transport._running = False
            await transport._dispatcher.stop()

    async def test_websocket_client_sender_failure_is_counted_and_closes_connection(self) -> None:
        class FailingWebsocket:
            def __init__(self) -> None:
                self.closed = False
                self.close_code = None

            async def send_str(self, _payload: str) -> None:
                raise ConnectionError("simulated send failure")

            async def close(self, *, code=1000) -> None:
                self.closed = True
                self.close_code = code

        config = build_config(
            "websocket-client",
            {
                "url": "ws://127.0.0.1:1/ws",
                "message_post_format": "array",
                "report_self_message": False,
                "reconnect_interval_ms": 100,
                "heartbeat_interval_ms": 0,
                "access_token": "",
                "debug": False,
            },
        )
        transport = create_transport(config, self.action_handler)
        websocket = FailingWebsocket()
        transport._running = True
        transport._ws = websocket
        transport._outgoing.put_nowait({"post_type": "notice"})
        try:
            with self.assertRaisesRegex(ConnectionError, "simulated send failure"):
                await transport._sender_loop()
            await asyncio.wait_for(transport._outgoing.join(), timeout=2)
            self.assertEqual(1, transport._dropped_event_count)
            self.assertEqual(1011, websocket.close_code)
        finally:
            transport._running = False


if __name__ == "__main__":
    unittest.main()

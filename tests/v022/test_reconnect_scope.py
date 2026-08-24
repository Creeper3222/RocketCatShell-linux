from __future__ import annotations

import asyncio
import inspect
import unittest
from unittest.mock import AsyncMock, patch

import rocketcat_shell.bridge.rocketchat_client as rocketchat_client_module
import rocketcat_shell.bridge.transports.websocket_client as websocket_client_module
from rocketcat_shell.bridge.config import BridgeConfig
from rocketcat_shell.bridge.onebot_client import (
    OneBotReverseWsClient,
    _ONEBOT_UPSTREAM_RETRY_DELAY_SECONDS,
)
from rocketcat_shell.bridge.rocketchat_client import RocketChatClient
from rocketcat_shell.bridge.runtime import BridgeRuntime
from rocketcat_shell.diagnostics import format_runtime_diagnostic_lines


async def empty_action_handler(_action: str, _params: dict) -> dict:
    return {"status": "ok", "retcode": 0, "data": None, "wording": ""}


def build_config(**overrides) -> BridgeConfig:
    payload = {
        "enabled": True,
        "server_url": "http://127.0.0.1:3000",
        "username": "rocketcat",
        "password": "secret",
        "onebot_ws_url": "ws://127.0.0.1:6199/ws/",
        "reconnect_delay": 0.01,
        "max_reconnect_attempts": 1,
        "onebot_outgoing_queue_max_entries": 1,
    }
    payload.update(overrides)
    return BridgeConfig.from_mapping(payload)


class FailingWebSocketContext:
    async def __aenter__(self):
        raise ConnectionRefusedError("OneBot upstream is offline")

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class FailingSession:
    def __init__(self) -> None:
        self.attempts = 0

    def ws_connect(self, *_args, **_kwargs):
        self.attempts += 1
        return FailingWebSocketContext()


class FakeWebSocket:
    def __init__(self, *, closed: bool = False) -> None:
        self.closed = closed


class ReconnectScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_onebot_waits_forever_with_its_own_quiet_retry_policy(self) -> None:
        config = build_config(reconnect_delay=0.01, max_reconnect_attempts=1)
        client = OneBotReverseWsClient(config, empty_action_handler)
        session = FailingSession()
        client._http_session = session
        client._running = True
        sleep_delays: list[float] = []

        async def controlled_sleep(delay: float) -> None:
            sleep_delays.append(delay)
            if len(sleep_delays) >= 3:
                client._running = False

        with (
            patch.object(websocket_client_module.asyncio, "sleep", side_effect=controlled_sleep),
            patch.object(websocket_client_module.logger, "info") as log_info,
            patch.object(websocket_client_module.logger, "debug") as log_debug,
            patch.object(websocket_client_module.logger, "warning") as log_warning,
        ):
            await client._run_forever()

        self.assertEqual(3, session.attempts)
        self.assertEqual(
            [_ONEBOT_UPSTREAM_RETRY_DELAY_SECONDS] * 3,
            sleep_delays,
        )
        self.assertGreater(session.attempts, config.max_reconnect_attempts)
        self.assertEqual(1, log_info.call_count)
        self.assertEqual(2, log_debug.call_count)
        log_warning.assert_not_called()
        self.assertTrue(client._waiting_for_upstream)

    async def test_onebot_drops_offline_and_overflow_events_without_replay(self) -> None:
        client = OneBotReverseWsClient(build_config(), empty_action_handler)
        client._running = True
        websocket = FakeWebSocket()
        client._ws = websocket

        await client.emit_event({"message_id": 1})
        await client.emit_event({"message_id": 2})
        websocket.closed = True
        await client.emit_event({"message_id": 3})

        self.assertEqual(1, client._outgoing.qsize())
        self.assertEqual(2, client._dropped_event_count)
        self.assertEqual(1, client._discard_queued_events())
        snapshot = client.build_diagnostic_snapshot()
        self.assertEqual(0, snapshot["outgoing_queue_depth"])
        self.assertEqual(3, snapshot["onebot_dropped_event_count"])
        self.assertFalse(snapshot["onebot_connected"])

    async def test_rocketchat_exhaustion_still_invokes_disable_callback(self) -> None:
        failures: list[tuple[str, int, str]] = []

        async def on_exhausted(client_name: str, attempts: int, reason: str) -> None:
            failures.append((client_name, attempts, reason))

        rocketchat = RocketChatClient(
            build_config(max_reconnect_attempts=2),
            on_reconnect_exhausted=on_exhausted,
        )
        rocketchat._running = True
        rocketchat._consecutive_reconnect_failures = 2
        with patch.object(rocketchat_client_module.logger, "error"):
            await rocketchat._handle_reconnect_exhausted(ConnectionError("offline"))

        self.assertFalse(rocketchat._running)
        self.assertEqual("Rocket.Chat", failures[0][0])
        self.assertEqual(2, failures[0][1])
        self.assertIn("offline", failures[0][2])

    async def test_websocket_reconnect_keeps_rest_session_for_accepted_messages(self) -> None:
        rocketchat = RocketChatClient(build_config(max_reconnect_attempts=3))
        rocketchat._running = True
        rocketchat.auth_token = "still-valid-token"
        rocketchat.user_id = "bot-user"

        async def stop_after_failure(_delay: float) -> None:
            rocketchat._running = False

        with (
            patch.object(rocketchat.e2ee, "initialize", new=AsyncMock()),
            patch.object(
                rocketchat,
                "_ws_connect_and_listen",
                new=AsyncMock(side_effect=ConnectionError("realtime disconnected")),
            ),
            patch.object(rocketchat_client_module.asyncio, "sleep", side_effect=stop_after_failure),
        ):
            await rocketchat._run_forever(initial_login_complete=True)

        self.assertEqual("still-valid-token", rocketchat.auth_token)
        self.assertEqual("bot-user", rocketchat.user_id)

    def test_only_rocketchat_owns_configured_retry_budget_and_disable_callback(self) -> None:
        config = build_config(max_reconnect_attempts=2)
        rocketchat = RocketChatClient(config)
        rocketchat._consecutive_reconnect_failures = 1
        self.assertFalse(rocketchat._should_stop_reconnect())
        rocketchat._consecutive_reconnect_failures = 2
        self.assertTrue(rocketchat._should_stop_reconnect())

        self.assertNotIn(
            "on_reconnect_exhausted",
            inspect.signature(OneBotReverseWsClient).parameters,
        )
        start_source = inspect.getsource(BridgeRuntime._start_clients)
        self.assertIn("self.onebot = create_transport(", start_source)
        onebot_constructor = start_source.split(
            "self.onebot = create_transport(", 1
        )[1].split("await self.rocketchat.start_realtime()", 1)[0]
        self.assertNotIn("on_reconnect_exhausted", onebot_constructor)
        self.assertIn(
            "on_reconnect_exhausted=self._handle_rocketchat_reconnect_exhausted",
            start_source,
        )

    def test_text_diagnostics_distinguish_both_connection_sides(self) -> None:
        lines = format_runtime_diagnostic_lines(
            {
                "reconnect_failures": 2,
                "onebot_connected": False,
                "onebot_waiting_for_upstream": True,
                "onebot_retry_delay_seconds": 5.0,
                "onebot_dropped_event_count": 7,
            }
        )
        self.assertIn("Rocket.Chat 重连失败次数：2", lines)
        self.assertIn("OneBot 上游：等待上游（每 5 秒重试）", lines)
        self.assertIn("OneBot 丢弃事件：7", lines)


if __name__ == "__main__":
    unittest.main()

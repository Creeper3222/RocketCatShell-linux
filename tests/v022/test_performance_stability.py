from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from rocketcat_shell.bridge.config import BridgeConfig
from rocketcat_shell.bridge.hot_storage import JournalPersistenceWorker, RuntimeStateEngine
from rocketcat_shell.bridge.media import MediaCacheCoordinator, RocketChatMediaBridge
from rocketcat_shell.bridge.onebot_client import OneBotReverseWsClient
from rocketcat_shell.bridge.rocketchat_client import RocketChatClient
from rocketcat_shell.bridge.runtime import BridgeRuntime
from rocketcat_shell.bridge.user_identity import UserIdentityRegistry
from rocketcat_shell.crypto_executor import CRYPTO_EXECUTOR
from rocketcat_shell.logger import _AsyncLogDispatcher
from rocketcat_shell.performance import EventLoopLagMonitor, RuntimeMetrics
from rocketcat_shell.plugin_system.manager import RuntimePluginBinding
from tools.stress_v022_full_stack import FullStackHarness, FakeOneBot, phase_adjusted_rss_slope


def build_config(**overrides):
    payload = {
        "enabled": True,
        "bot_id": "perf-bot",
        "display_name": "Perf Bot",
        "server_url": "http://127.0.0.1:3000",
        "username": "bot",
        "password": "password",
        "onebot_ws_url": "ws://127.0.0.1:6200/ws/",
        "onebot_self_id": 10001,
        "inbound_worker_count": 1,
        "max_reconnect_attempts": 3,
        "reconnect_delay": 0.01,
    }
    payload.update(overrides)
    return BridgeConfig.from_mapping(payload)


class _FakeWebSocket:
    def __init__(self):
        self.closed = False
        self.sent: list[str] = []

    async def send_str(self, payload: str) -> None:
        self.sent.append(payload)


class PerformanceMetricTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_loop_monitor_is_bounded(self) -> None:
        monitor = EventLoopLagMonitor(interval_seconds=0.02, capacity=32)
        await monitor.start()
        await asyncio.sleep(0.08)
        await monitor.stop()
        snapshot = monitor.snapshot()
        self.assertGreater(snapshot["count"], 0)
        self.assertLessEqual(snapshot["sample_count"], 32)
        self.assertAlmostEqual(snapshot["max"], monitor.current_max(), places=3)

    def test_runtime_metrics_never_store_payloads(self) -> None:
        metrics = RuntimeMetrics(sample_capacity=32)
        for index in range(100):
            metrics.observe("latency", index)
            metrics.increment("processed")
        snapshot = metrics.snapshot()
        self.assertEqual(100, snapshot["counters"]["processed"])
        timing = snapshot["timings"]["latency"]
        self.assertEqual(7, timing["sample_count"])
        self.assertEqual(16, timing["sample_every"])
        self.assertEqual(0.0625, timing["sample_rate"])
        self.assertNotIn("payload", repr(snapshot).lower())

    def test_rss_slope_uses_stable_low_water_windows(self) -> None:
        samples = []
        timestamp = 0.0
        base = 100 * 1024 * 1024
        for phase in ("steady", "mixed", "overload", "recovery"):
            for index in range(120):
                transient = 8 * 1024 * 1024 if index < 20 or index % 30 == 0 else 0
                samples.append((timestamp, base + transient, phase))
                timestamp += 1.0
            base += 4 * 1024 * 1024
        self.assertAlmostEqual(0.0, phase_adjusted_rss_slope(samples), delta=0.05)

    def test_rss_slope_detects_sustained_growth(self) -> None:
        samples = []
        timestamp = 0.0
        bytes_per_second = (2 * 1024 * 1024) / 600.0
        for phase in ("steady", "mixed", "overload", "recovery"):
            phase_start = timestamp
            phase_base = 100 * 1024 * 1024
            for _index in range(180):
                samples.append(
                    (timestamp, int(phase_base + (timestamp - phase_start) * bytes_per_second), phase)
                )
                timestamp += 1.0
        self.assertAlmostEqual(2.0, phase_adjusted_rss_slope(samples), delta=0.1)

    def test_stress_duplicate_tracker_uses_exact_bitset(self) -> None:
        observer = FakeOneBot()
        payload = {"raw_message": "seq=1000 room=3 load=stress"}
        observer._observe_message(10001, payload)
        observer._observe_message(10001, payload)
        self.assertEqual(1, observer.received)
        self.assertEqual(1, observer.duplicates)
        self.assertEqual(126, len(observer.seen[10001]))

    def test_gc_pause_snapshot_is_safe_during_callback_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            harness = FullStackHarness(seed=1, root=Path(temporary_directory))

            def record_pauses() -> None:
                for _index in range(500):
                    harness._observe_gc_pause("start", {"generation": 1})
                    harness._observe_gc_pause("stop", {"generation": 1})

            workers = [threading.Thread(target=record_pauses) for _index in range(4)]
            for worker in workers:
                worker.start()
            while any(worker.is_alive() for worker in workers):
                harness.gc_pause_snapshot()
            for worker in workers:
                worker.join()
            snapshot = harness.gc_pause_snapshot()
            self.assertLessEqual(len(snapshot), 2000)
            self.assertTrue(all(item[2] == 1 for item in snapshot))


class InboundQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_drops_newest_and_preserves_room_order(self) -> None:
        release = asyncio.Event()
        started = asyncio.Event()
        processed: list[str] = []

        async def on_message(payload):
            started.set()
            await release.wait()
            processed.append(payload["_id"])

        client = RocketChatClient(build_config(), on_message=on_message)
        client._INBOUND_QUEUE_MAX_SIZE = 2
        client._running = True
        client._start_inbound_workers()
        try:
            await client._enqueue_incoming_message({"_id": "m1", "rid": "room-a"})
            await started.wait()
            await client._enqueue_incoming_message({"_id": "m2", "rid": "room-a"})
            await client._enqueue_incoming_message({"_id": "m3", "rid": "room-a"})
            await client._enqueue_incoming_message({"_id": "m4", "rid": "room-a"})
            snapshot = client.build_diagnostic_snapshot()["performance"]["ingress"]
            self.assertEqual(1, snapshot["overload_dropped"])
            release.set()
            await client._drain_inbound_queue(timeout=1)
            self.assertEqual(["m1", "m2", "m3"], processed)
        finally:
            release.set()
            await client._stop_inbound_workers()

    async def test_control_frame_does_not_wait_for_ingress_capacity(self) -> None:
        client = RocketChatClient(build_config())
        websocket = _FakeWebSocket()
        started = time.perf_counter()
        await client._dispatch_ddp({"msg": "ping"}, websocket)
        self.assertLess(time.perf_counter() - started, 0.05)
        self.assertEqual(1, len(websocket.sent))
        snapshot = client.build_diagnostic_snapshot()
        self.assertEqual(1, snapshot["ddp_ping_count"])
        self.assertIsNotNone(snapshot["last_ddp_ping_at"])

    async def test_inbound_workers_retire_after_idle_window(self) -> None:
        client = RocketChatClient(build_config())
        client._running = True
        client._start_inbound_workers()
        with mock.patch(
            "rocketcat_shell.bridge.rocketchat_client.INBOUND_WORKER_IDLE_SECONDS",
            0.02,
        ):
            await client._enqueue_incoming_message({"_id": "m1", "rid": "room-a"})
            await client._drain_inbound_queue(timeout=1)
            self.assertEqual(1, len(client._inbound_workers))
            await asyncio.sleep(0.15)
            self.assertEqual(0, len(client._inbound_workers))
        await client._stop_inbound_workers()

    async def test_subscription_ready_diagnostics_require_every_requested_id(self) -> None:
        client = RocketChatClient(build_config())
        websocket = _FakeWebSocket()
        client.user_id = "user-1"
        await client._ddp_subscribe_rooms(websocket, [{"rid": "room-1"}])
        await client._ddp_subscribe_user_events(websocket)

        snapshot = client.build_diagnostic_snapshot()
        self.assertEqual(2, snapshot["subscription_requested_count"])
        self.assertEqual(0, snapshot["subscription_ready_count"])
        self.assertFalse(snapshot["subscriptions_ready"])

        await client._dispatch_ddp({"msg": "ready", "subs": ["room-room-1"]}, websocket)
        self.assertFalse(client.build_diagnostic_snapshot()["subscriptions_ready"])
        await client._dispatch_ddp({"msg": "ready", "subs": ["user-notif-user-1"]}, websocket)
        snapshot = client.build_diagnostic_snapshot()
        self.assertEqual(2, snapshot["subscription_ready_count"])
        self.assertTrue(snapshot["subscriptions_ready"])


class ActionWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_workers_start_lazily_and_report_diagnostics(self) -> None:
        async def handler(_action, _params):
            return {"status": "ok", "retcode": 0, "data": {"ok": True}}

        client = OneBotReverseWsClient(build_config(), handler)
        client._running = True
        client._start_action_workers()
        try:
            self.assertEqual(0, len(client._action_workers))
            response = await client._dispatcher.execute("get_status", {}, "probe")
            self.assertEqual("ok", response["status"])
            self.assertEqual(1, len(client._action_workers))
            snapshot = client.build_diagnostic_snapshot()["performance"]["onebot_actions"]
            self.assertEqual(256, snapshot["capacity"])
            self.assertEqual(1, snapshot["workers"])
            self.assertEqual(8, snapshot["worker_limit"])
        finally:
            await client._stop_action_workers()

    async def test_action_workers_retire_after_idle_window(self) -> None:
        async def handler(_action, _params):
            return {"status": "ok", "retcode": 0, "data": None}

        client = OneBotReverseWsClient(build_config(), handler)
        client._running = True
        client._start_action_workers()
        with mock.patch(
            "rocketcat_shell.bridge.transports.action_dispatcher.ACTION_WORKER_IDLE_SECONDS",
            0.02,
        ):
            await client._dispatcher.execute("get_status", {}, "probe")
            self.assertEqual(1, len(client._action_workers))
            await asyncio.sleep(0.15)
            self.assertEqual(0, len(client._action_workers))
        await client._stop_action_workers()


class PersistenceWorkerTests(unittest.IsolatedAsyncioTestCase):
    def test_runtime_state_diagnostic_counts_stay_within_message_window(self) -> None:
        engine = RuntimeStateEngine(message_window_size=128)
        for index in range(400):
            mapping, _snapshot = engine.allocate_mapping("message", f"message-{index}")
            engine.put_message(
                {
                    "source_id": f"message-{index}",
                    "surrogate_id": mapping.surrogate_id,
                    "context_source_id": "room-context",
                    "sender_source_id": f"user-{index % 20}",
                    "room_source_id": "room-1",
                    "timestamp": index,
                }
            )
        counts = engine.diagnostic_counts()
        self.assertEqual(128, counts["message_order"])
        self.assertEqual(128, counts["messages_by_source"])
        self.assertEqual(128, counts["context_sender_entries"])
        self.assertEqual(128, counts["forward_mappings"]["message"])

    async def test_async_submission_backpressures_and_flushes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            state = {"value": 1}
            worker = JournalPersistenceWorker(
                snapshot_path=root / "state.snapshot",
                journal_path=root / "state.journal",
                snapshot_provider=lambda: dict(state),
                flush_every_records=1,
                snapshot_every_records=2048,
            )
            try:
                await worker.enqueue_batch_async([{"op": "test"}])
                await asyncio.to_thread(worker.flush, timeout=2)
                snapshot = worker.diagnostic_snapshot()
                self.assertTrue(snapshot["writer_alive"])
                self.assertEqual(1024, snapshot["capacity"])
                self.assertEqual(1, snapshot["accepted_batches"])
            finally:
                await asyncio.to_thread(worker.close, timeout=2)
            self.assertTrue((root / "state.snapshot").exists())


class SharedResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_identity_registry_singleflight_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "identity.sqlite3"
            first = UserIdentityRegistry(database_path, scope_key="scope", bot_id="a")
            second = UserIdentityRegistry(database_path, scope_key="scope", bot_id="b")
            original = first.ensure_mapping_sync
            calls = 0

            def delayed(*args, **kwargs):
                nonlocal calls
                calls += 1
                time.sleep(0.03)
                return original(*args, **kwargs)

            with mock.patch.object(first, "ensure_mapping_sync", side_effect=delayed):
                left, right = await asyncio.gather(
                    first.ensure_mapping("shared-user"),
                    second.ensure_mapping("shared-user"),
                )
            self.assertEqual(left.onebot_id, right.onebot_id)
            self.assertEqual(1, calls)
            self.assertGreaterEqual(first.cache_summary()["singleflight_merges"], 1)
            state_key = first._state_key
            await first.release()
            self.assertIn(state_key, UserIdentityRegistry._shared_states)
            await second.release()
            self.assertNotIn(state_key, UserIdentityRegistry._shared_states)

    async def test_media_coordinator_reassigns_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = SimpleNamespace(
                bot_id="one",
                onebot_ws_url="ws://127.0.0.1:1/ws/",
                remote_media_max_size=1024,
            )
            first = RocketChatMediaBridge(SimpleNamespace(config=config), temp_dir=temporary_directory)
            second_config = SimpleNamespace(**vars(config))
            second_config.bot_id = "two"
            second = RocketChatMediaBridge(SimpleNamespace(config=second_config), temp_dir=temporary_directory)
            with mock.patch.object(first, "_cleanup_media_cache"), mock.patch.object(second, "_cleanup_media_cache"):
                await first.start()
                await second.start()
                coordinator = first._coordinator
                self.assertIs(coordinator, second._coordinator)
                self.assertEqual(2, coordinator.ref_count)
                await first.stop()
                self.assertIs(second, coordinator.owner)
                self.assertEqual(1, coordinator.ref_count)
                await second.stop()
            self.assertFalse(MediaCacheCoordinator._registry)

    async def test_crypto_pool_never_exceeds_two_workers(self) -> None:
        lock = threading.Lock()
        active = 0
        maximum = 0

        def work():
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.04)
            with lock:
                active -= 1

        loop = asyncio.get_running_loop()
        await asyncio.gather(*(loop.run_in_executor(CRYPTO_EXECUTOR, work) for _ in range(6)))
        self.assertLessEqual(maximum, 2)


class _TimeoutPlugin:
    enabled = True

    def wants_inbound_message(self, *_args):
        return True

    async def on_inbound_message(self, *_args):
        raise asyncio.TimeoutError


class PluginCircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_timeouts_open_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime = BridgeRuntime(
                plugin_root=Path(__file__).resolve().parents[2] / "rocketcat_shell",
                raw_config=build_config().to_mapping(),
                data_dir=Path(temporary_directory),
            )
            descriptor = SimpleNamespace(plugin_id="slow-plugin")
            runtime._runtime_plugins = [
                RuntimePluginBinding(
                    descriptor=descriptor,
                    instance=_TimeoutPlugin(),
                    handled_actions=frozenset(),
                    generation=1,
                )
            ]
            runtime._plugin_runtime_context = SimpleNamespace()
            for _ in range(3):
                await runtime._dispatch_inbound_message_plugins({}, {})
            breaker = runtime._plugin_breakers["slow-plugin"]
            self.assertGreater(breaker["open_until"], time.monotonic())
            await runtime._dispatch_inbound_message_plugins({}, {})
            metrics = runtime._plugin_metrics.snapshot()["counters"]
            self.assertEqual(3, metrics["timeouts"])
            self.assertEqual(1, metrics["circuit_skips"])
            if runtime._hot_store_bundle is not None:
                await asyncio.to_thread(runtime._hot_store_bundle.close)
                runtime._hot_store_bundle = None


class LogDispatcherTests(unittest.TestCase):
    def test_priority_queues_are_bounded(self) -> None:
        sink = logging.NullHandler()
        dispatcher = _AsyncLogDispatcher([sink], normal_capacity=2, critical_capacity=1)
        try:
            with dispatcher._condition:
                dispatcher._normal.extend(
                    [
                        logging.LogRecord("test", logging.DEBUG, __file__, 1, "d", (), None),
                        logging.LogRecord("test", logging.INFO, __file__, 1, "i", (), None),
                    ]
                )
                dispatcher._submit_normal(
                    logging.LogRecord("test", logging.INFO, __file__, 1, "new", (), None)
                )
                dispatcher._critical.append(
                    logging.LogRecord("test", logging.WARNING, __file__, 1, "w", (), None)
                )
                dispatcher._submit_critical(
                    logging.LogRecord("test", logging.ERROR, __file__, 1, "e", (), None)
                )
                snapshot = dispatcher.snapshot()
            self.assertLessEqual(snapshot["normal_depth"], 2)
            self.assertLessEqual(snapshot["critical_depth"], 1)
            self.assertEqual(1, snapshot["dropped"]["debug"])
            self.assertEqual(1, snapshot["dropped"]["warning"])
        finally:
            dispatcher.stop(timeout=1)


if __name__ == "__main__":
    unittest.main()

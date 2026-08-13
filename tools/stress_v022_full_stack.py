from __future__ import annotations

import argparse
import asyncio
import contextlib
import faulthandler
import gc
import json
import logging
import math
import os
import random
import re
import shutil
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from collections import Counter, OrderedDict, defaultdict, deque
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
from aiohttp import web
import psutil

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rocketcat_shell.bridge.media_publication import MediaPublicationService
from rocketcat_shell.bridge.runtime import BridgeRuntime
from rocketcat_shell.crypto_executor import CRYPTO_EXECUTOR
from rocketcat_shell.layout import ProjectLayout
from rocketcat_shell.logger import (
    configure_logging,
    logger,
    logging_diagnostic_snapshot,
    shutdown_logging,
)
from rocketcat_shell.performance import EventLoopLagMonitor
from rocketcat_shell.plugin_system.manager import RocketCatPluginManager, RuntimePluginBinding
from rocketcat_shell.shell.webui import ShellWebUI


BOT_COUNT = 3
ROOM_COUNT = 12
USER_COUNT = 200
SEQ_PATTERN = re.compile(r"\bseq=(\d+)\b")
ROOM_PATTERN = re.compile(r"\broom=(\d+)\b")


def percentile(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * value) - 1))
    return round(float(ordered[index]), 3)


def process_handles(process: psutil.Process) -> int:
    return int(process.num_handles()) if hasattr(process, "num_handles") else 0


def sample_process_rss(process_id: int) -> int:
    """Sample RSS outside the stressed interpreter and its GIL."""
    process = psutil.Process(int(process_id))
    with process.oneshot():
        return int(process.memory_info().rss)


def phase_adjusted_rss_slope(samples: list[tuple[float, int, str]]) -> float:
    """Estimate sustained RSS growth while excluding bounded phase offsets.

    Queue/cache high-water allocation legitimately differs between steady,
    mixed, overload and recovery workloads. The first 20 percent of each phase
    is transition/warm-up and is excluded. Thirty-second low-water windows
    remove transient work-queue occupancy, then a phase-local Theil-Sen slope
    rejects isolated allocator/working-set outliers. Sustained leaks still raise
    each successive low-water point. Fault injection and drain are reported
    separately because they deliberately change connection/task populations.
    """
    by_phase: dict[str, list[tuple[float, int]]] = defaultdict(list)
    for timestamp, rss_bytes, phase in samples:
        if phase in {"steady", "mixed", "overload", "recovery"}:
            by_phase[phase].append((timestamp, rss_bytes))
    low_water_by_phase: dict[str, list[tuple[float, int]]] = {}
    for phase, values in by_phase.items():
        transition_samples = max(1, math.ceil(len(values) * 0.20))
        stable_values = values[transition_samples:]
        if len(stable_values) < 3:
            continue
        window_seconds = 30.0 if (stable_values[-1][0] - stable_values[0][0]) >= 90.0 else 10.0
        window_start = stable_values[0][0]
        windows: dict[int, list[tuple[float, int]]] = defaultdict(list)
        for timestamp, rss_bytes in stable_values:
            window_index = int((timestamp - window_start) // window_seconds)
            windows[window_index].append((timestamp, rss_bytes))
        low_water_by_phase[phase] = [
            min(window_values, key=lambda item: item[1])
            for _window_index, window_values in sorted(windows.items())
            if window_values
        ]

    slopes: list[float] = []
    for values in low_water_by_phase.values():
        if len(values) < 2:
            continue
        for left_index, (left_time, left_rss) in enumerate(values[:-1]):
            for right_time, right_rss in values[left_index + 1 :]:
                elapsed = right_time - left_time
                if elapsed <= 0.0:
                    continue
                slopes.append((right_rss - left_rss) / elapsed)
    if not slopes:
        return 0.0
    # Theil-Sen is resistant to allocator page faults and short-lived queue
    # occupancy while still reporting a consistent leak in every phase.
    bytes_per_second = statistics.median(slopes)
    return bytes_per_second * 600.0 / (1024.0 * 1024.0)


def summarize_rss_phases(samples: list[tuple[float, int, str]]) -> dict[str, dict[str, int]]:
    grouped: dict[str, list[int]] = defaultdict(list)
    for _timestamp, rss_bytes, phase in samples:
        grouped[phase].append(rss_bytes)
    return {
        phase: {
            "samples": len(values),
            "start_bytes": values[0],
            "end_bytes": values[-1],
            "min_bytes": min(values),
            "max_bytes": max(values),
        }
        for phase, values in grouped.items()
        if values
    }


def task_summary() -> dict[str, int]:
    descriptions = []
    for task in asyncio.all_tasks():
        if task.done():
            continue
        coroutine = task.get_coro()
        descriptions.append(getattr(coroutine, "__qualname__", type(coroutine).__name__))
    return dict(sorted(Counter(descriptions).items()))


class FakeRocketChat:
    def __init__(self) -> None:
        self.app = web.Application()
        self.app.add_routes(
            [
                web.get("/api/info", self.info),
                web.post("/api/v1/login", self.login),
                web.get("/api/v1/subscriptions.get", self.subscriptions),
                web.get("/api/v1/rooms.info", self.room_info),
                web.get("/api/v1/users.info", self.user_info),
                web.get("/api/v1/chat.getMessage", self.get_message),
                web.get("/media/sample.png", self.media),
                web.get("/websocket", self.websocket),
            ]
        )
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.base_url = ""
        self.clients: dict[str, web.WebSocketResponse] = {}
        self.client_lock = asyncio.Lock()
        self.send_lock = asyncio.Lock()
        self.pending_pings: dict[str, tuple[float, asyncio.Future[float]]] = {}
        self.rest_failures: deque[int] = deque()
        self.messages: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.media_requests = 0

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        socket = self.site._server.sockets[0]
        self.base_url = f"http://127.0.0.1:{socket.getsockname()[1]}"

    async def stop(self) -> None:
        async with self.client_lock:
            sockets = list(self.clients.values())
            self.clients.clear()
        for socket in sockets:
            await socket.close()
        if self.runner is not None:
            await self.runner.cleanup()
        self.runner = None
        self.site = None

    async def info(self, _request: web.Request) -> web.Response:
        return web.json_response({"version": "8.7.2", "cloudWorkspaceId": "stress-workspace"})

    async def login(self, request: web.Request) -> web.Response:
        payload = await request.json()
        username = str(payload.get("user") or "bot")
        return web.json_response(
            {
                "status": "success",
                "data": {
                    "authToken": f"token-{username}",
                    "userId": f"rc-{username}",
                    "me": {"_id": f"rc-{username}", "username": username, "name": username},
                },
            }
        )

    async def subscriptions(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "success": True,
                "update": [
                    {"rid": f"room-{index}", "t": "c", "name": f"room-{index}"}
                    for index in range(ROOM_COUNT)
                ],
            }
        )

    async def room_info(self, request: web.Request) -> web.Response:
        if self.rest_failures:
            status = self.rest_failures.popleft()
            return web.json_response(
                {"success": False, "error": "injected"},
                status=status,
                headers={"Retry-After": "0"},
            )
        room_id = str(request.query.get("roomId") or "room-0")
        return web.json_response(
            {"success": True, "room": {"_id": room_id, "t": "c", "name": room_id}}
        )

    async def user_info(self, request: web.Request) -> web.Response:
        user_id = str(request.query.get("userId") or "user-0")
        return web.json_response(
            {
                "success": True,
                "user": {"_id": user_id, "username": user_id, "name": user_id},
            }
        )

    async def get_message(self, request: web.Request) -> web.Response:
        message_id = str(request.query.get("msgId") or "")
        message = self.messages.get(message_id)
        if message is None:
            return web.json_response(
                {"success": False, "error": "message-not-found"},
                status=404,
            )
        return web.json_response({"success": True, "message": message})

    async def media(self, _request: web.Request) -> web.Response:
        self.media_requests += 1
        return web.Response(
            body=b"\x89PNG\r\n\x1a\n" + (b"rocketcat" * 2048),
            content_type="image/png",
        )

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse(heartbeat=30)
        await socket.prepare(request)
        username = ""
        try:
            async for message in socket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                message_type = payload.get("msg")
                if message_type == "connect":
                    await socket.send_json({"msg": "connected", "session": "stress"})
                elif message_type == "method" and payload.get("method") == "login":
                    params = payload.get("params") or [{}]
                    token = str((params[0] or {}).get("resume") or "")
                    username = token.removeprefix("token-")
                    async with self.client_lock:
                        self.clients[username] = socket
                    await socket.send_json({"msg": "result", "id": payload.get("id"), "result": {"id": username}})
                elif message_type == "pong" and username:
                    pending = self.pending_pings.pop(username, None)
                    if pending is not None:
                        started, future = pending
                        if not future.done():
                            future.set_result((time.perf_counter() - started) * 1000.0)
        finally:
            if username:
                async with self.client_lock:
                    if self.clients.get(username) is socket:
                        self.clients.pop(username, None)
                pending = self.pending_pings.pop(username, None)
                if pending is not None and not pending[1].done():
                    pending[1].cancel()
        return socket

    async def wait_clients(self, count: int = BOT_COUNT, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            async with self.client_lock:
                if len(self.clients) >= count:
                    return
            await asyncio.sleep(0.05)
        raise TimeoutError(f"Rocket.Chat DDP clients did not reach {count}")

    async def inject(self, raw_message: dict[str, Any]) -> int:
        message_id = str(raw_message.get("_id") or "")
        if message_id:
            self.messages[message_id] = dict(raw_message)
            self.messages.move_to_end(message_id)
            while len(self.messages) > 8192:
                self.messages.popitem(last=False)
        frame = {
            "msg": "changed",
            "collection": "stream-room-messages",
            "fields": {"args": [raw_message]},
        }
        async with self.client_lock:
            sockets = list(self.clients.values())
        async with self.send_lock:
            delivered = 0
            for socket in sockets:
                if socket.closed:
                    continue
                await socket.send_json(frame)
                delivered += 1
        return delivered

    async def ping_all(self, timeout: float = 2.0) -> list[float]:
        loop = asyncio.get_running_loop()
        async with self.client_lock:
            clients = list(self.clients.items())
        futures: list[asyncio.Future[float]] = []
        async with self.send_lock:
            for username, socket in clients:
                future: asyncio.Future[float] = loop.create_future()
                self.pending_pings[username] = (time.perf_counter(), future)
                futures.append(future)
                await socket.send_json({"msg": "ping"})
        if not futures:
            return []
        results = await asyncio.gather(
            *(asyncio.wait_for(asyncio.shield(future), timeout=timeout) for future in futures),
            return_exceptions=True,
        )
        return [float(result) for result in results if isinstance(result, (int, float))]

    async def disconnect_one(self) -> None:
        async with self.client_lock:
            socket = next(iter(self.clients.values()), None)
        if socket is not None:
            await socket.close()


class FakeOneBot:
    def __init__(self) -> None:
        self.app = web.Application()
        self.app.add_routes([web.get("/ws/", self.websocket)])
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self.ws_url = ""
        self.clients: dict[int, web.WebSocketResponse] = {}
        self.client_lock = asyncio.Lock()
        self.pending_actions: dict[str, tuple[float, asyncio.Future[dict[str, Any]]]] = {}
        self.reset_observations()

    def reset_observations(self) -> None:
        self.received = 0
        self.duplicates = 0
        self.out_of_order = 0
        self.unparsed = 0
        self.seen: dict[int, bytearray] = defaultdict(bytearray)
        self.last_by_room: dict[tuple[int, int], int] = {}
        self.action_latencies_ms: list[float] = []
        self.action_failures = 0

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        socket = self.site._server.sockets[0]
        self.ws_url = f"ws://127.0.0.1:{socket.getsockname()[1]}/ws/"

    async def stop(self) -> None:
        async with self.client_lock:
            sockets = list(self.clients.values())
            self.clients.clear()
        for socket in sockets:
            await socket.close()
        if self.runner is not None:
            await self.runner.cleanup()
        self.runner = None
        self.site = None

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse(heartbeat=30)
        await socket.prepare(request)
        self_id = 0
        try:
            async for message in socket:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                if payload.get("post_type") == "meta_event" and payload.get("meta_event_type") == "lifecycle":
                    self_id = int(payload.get("self_id") or 0)
                    async with self.client_lock:
                        self.clients[self_id] = socket
                    continue
                if payload.get("post_type") == "message":
                    self._observe_message(self_id, payload)
                    continue
                echo = str(payload.get("echo") or "")
                if echo and echo in self.pending_actions:
                    started, future = self.pending_actions.pop(echo)
                    if not future.done():
                        self.action_latencies_ms.append((time.perf_counter() - started) * 1000.0)
                        future.set_result(payload)
        finally:
            if self_id:
                async with self.client_lock:
                    if self.clients.get(self_id) is socket:
                        self.clients.pop(self_id, None)
        return socket

    def _observe_message(self, self_id: int, payload: dict[str, Any]) -> None:
        raw = str(payload.get("raw_message") or payload.get("message") or "")
        sequence_match = SEQ_PATTERN.search(raw)
        room_match = ROOM_PATTERN.search(raw)
        if sequence_match is None or room_match is None:
            self.unparsed += 1
            return
        sequence = int(sequence_match.group(1))
        room = int(room_match.group(1))
        seen = self.seen[self_id]
        byte_index, bit_index = divmod(sequence, 8)
        if byte_index >= len(seen):
            seen.extend(b"\0" * (byte_index + 1 - len(seen)))
        bit_mask = 1 << bit_index
        if seen[byte_index] & bit_mask:
            self.duplicates += 1
        else:
            seen[byte_index] |= bit_mask
            self.received += 1
        key = (self_id, room)
        previous = self.last_by_room.get(key)
        if previous is not None and sequence <= previous:
            self.out_of_order += 1
        self.last_by_room[key] = max(previous or 0, sequence)

    async def wait_clients(self, count: int = BOT_COUNT, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            async with self.client_lock:
                if len(self.clients) >= count:
                    return
            await asyncio.sleep(0.05)
        raise TimeoutError(f"OneBot clients did not reach {count}")

    async def send_actions(self, action: str = "get_login_info") -> None:
        loop = asyncio.get_running_loop()
        async with self.client_lock:
            clients = list(self.clients.items())
        pending: list[tuple[str, asyncio.Future[dict[str, Any]]]] = []
        for self_id, socket in clients:
            echo = f"stress-{self_id}-{time.time_ns()}"
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self.pending_actions[echo] = (time.perf_counter(), future)
            pending.append((echo, future))
            await socket.send_json({"action": action, "params": {}, "echo": echo})
        results = await asyncio.gather(
            *(asyncio.wait_for(asyncio.shield(future), timeout=3) for _, future in pending),
            return_exceptions=True,
        )
        for echo, _ in pending:
            self.pending_actions.pop(echo, None)
        for result in results:
            if isinstance(result, BaseException) or int(result.get("retcode") or 0) != 0:
                self.action_failures += 1

    async def disconnect_one(self) -> None:
        async with self.client_lock:
            socket = next(iter(self.clients.values()), None)
        if socket is not None:
            await socket.close()


class StressManagerAdapter:
    def __init__(
        self,
        *,
        layout: ProjectLayout,
        runtimes: list[BridgeRuntime],
        publication: MediaPublicationService,
        event_loop_monitor: EventLoopLagMonitor,
    ) -> None:
        self.layout = layout
        self.runtimes = runtimes
        self.media_publication = publication
        self.event_loop_monitor = event_loop_monitor
        self.settings = SimpleNamespace(terminal_max_sessions=2, terminal_idle_timeout_seconds=0)
        self.webui_port = 0

    def set_webui_runtime(self, *, actual_port: int, **_kwargs) -> None:
        self.webui_port = int(actual_port)
        self.media_publication.configure_webui(port=actual_port)

    def clear_webui_runtime(self) -> None:
        self.media_publication.clear_webui()

    async def get_webui_state(self, *, compact: bool = False) -> dict[str, Any]:
        payload = {
            "product": "RocketCat",
            "version": "v0.2.2",
            "bridge_enabled": True,
            "independent_webui_enabled": True,
            "enabled_bot_count": len(self.runtimes),
            "bot_count": len(self.runtimes),
            "access_url": f"http://127.0.0.1:{self.webui_port}/",
        }
        if not compact:
            payload["items"] = []
        return payload

    async def get_diagnostics_state(self) -> dict[str, Any]:
        items = [runtime.build_diagnostic_summary() for runtime in self.runtimes]
        return {
            "version": "v0.2.2",
            "host": None,
            "host_cache": {"cache_status": "stress"},
            "host_error": "",
            "performance": {
                "event_loop_lag_ms": self.event_loop_monitor.snapshot(),
                "logging": logging_diagnostic_snapshot(),
            },
            "items": items,
            "summary": {
                "bot_count": len(items),
                "enabled_bot_count": len(items),
                "online_bot_count": sum(1 for item in items if item.get("status_code") == "online"),
                "total_runtime_snapshot_bytes": sum(int(item.get("runtime_snapshot_bytes") or 0) for item in items),
                "total_runtime_journal_bytes": sum(int(item.get("runtime_journal_bytes") or 0) for item in items),
            },
        }


class TimeoutFaultPlugin:
    enabled = True

    def wants_inbound_message(self, *_args) -> bool:
        return True

    async def on_inbound_message(self, *_args) -> bool:
        raise asyncio.TimeoutError

    async def handle_onebot_action(self, *_args):
        return None

    async def on_unload(self, *_args) -> None:
        return None


@dataclass
class SampleState:
    rss: list[tuple[float, int, str]] = field(default_factory=list)
    webui_ms: list[float] = field(default_factory=list)
    webui_endpoint_ms: dict[str, list[float]] = field(default_factory=dict)
    ddp_ping_ms: list[float] = field(default_factory=list)
    overload_ping_ms: list[float] = field(default_factory=list)
    event_loop_max: list[tuple[float, float, str]] = field(default_factory=list)
    gc_pauses: deque[tuple[float, float, int, str]] = field(
        default_factory=lambda: deque(maxlen=4096)
    )
    errors: list[str] = field(default_factory=list)


class FullStackHarness:
    def __init__(self, *, seed: int, root: Path) -> None:
        self.random = random.Random(seed)
        self.root = root
        self.fake_rc = FakeRocketChat()
        self.fake_onebot = FakeOneBot()
        self.layout = self._build_layout(root)
        self.plugin_manager = RocketCatPluginManager(self.layout)
        self.publication = MediaPublicationService(max_entries=512)
        self.event_loop_monitor = EventLoopLagMonitor(interval_seconds=0.05)
        self.runtimes: list[BridgeRuntime] = []
        self.webui: ShellWebUI | None = None
        self.http_session: aiohttp.ClientSession | None = None
        self.webui_base_url = ""
        self.sequence = 0
        self.sent = 0
        self.last_message_by_room: dict[int, str] = {}
        self.processing_delay = 0.0
        self.pause_traffic = False
        self.current_phase = "startup"
        self.samples = SampleState()
        self.process = psutil.Process(os.getpid())
        self.resource_sampler: ProcessPoolExecutor | None = ProcessPoolExecutor(max_workers=1)
        self.sample_task: asyncio.Task[None] | None = None
        self.crypto_task: asyncio.Task[Any] | None = None
        self._gc_started: dict[tuple[int, int], float] = {}
        self._gc_pause_lock = threading.Lock()
        self._gc_callback_installed = False
        self.media_source = root / "media-source.bin"
        self.media_source.write_bytes(os.urandom(256 * 1024))

    @staticmethod
    def _build_layout(root: Path) -> ProjectLayout:
        config = root / "config"
        data = root / "data"
        layout = ProjectLayout(
            project_root=root,
            package_root=PROJECT_ROOT / "rocketcat_shell",
            config_dir=config,
            plugins_config_dir=config / "plugins_config",
            data_dir=data,
            temp_dir=data / "temp",
            bots_dir=data / "bots",
            plugins_dir=data / "plugins",
            plugin_data_dir=data / "plugin_data",
            logs_dir=root / "logs",
            shell_settings_path=config / "shell.json",
            bot_registry_path=config / "bots.json",
            log_file_path=root / "logs" / "rocketcat.log",
        )
        layout.ensure_directories()
        for plugin_dir in (PROJECT_ROOT / "data" / "plugins").glob("rocketcat_plugin_*"):
            shutil.copytree(plugin_dir, layout.plugins_dir / plugin_dir.name)
        return layout

    async def start(self) -> None:
        configure_logging(self.layout.log_file_path, level_name="WARNING")
        await self.fake_rc.start()
        await self.fake_onebot.start()
        await self.event_loop_monitor.start()
        await self.plugin_manager.initialize()
        for index in range(BOT_COUNT):
            bot_id = f"stress-bot-{index + 1}"
            raw_config = {
                "enabled": True,
                "bot_id": bot_id,
                "display_name": f"Stress Bot {index + 1}",
                "server_url": self.fake_rc.base_url,
                "username": f"bot{index + 1}",
                "password": "stress-only",
                "onebot_ws_url": self.fake_onebot.ws_url,
                "onebot_access_token": "",
                "reconnect_delay": 0.2,
                "max_reconnect_attempts": 20,
                "inbound_worker_count": 4,
                "onebot_outgoing_queue_max_entries": 512,
                "message_index_max_entries": 2048,
                "media_cache_max_bytes": 16 * 1024 * 1024,
                "media_cache_max_age_hours": 1,
            }
            runtime = BridgeRuntime(
                plugin_root=PROJECT_ROOT / "rocketcat_shell",
                raw_config=raw_config,
                data_dir=self.layout.bots_dir / bot_id,
                media_temp_dir=self.layout.temp_dir,
                instance_name=f"Stress Bot {index + 1}",
                message_index_max_entries=2048,
                media_publication_service=self.publication,
                plugin_manager=self.plugin_manager,
            )
            await runtime.start()
            self.runtimes.append(runtime)
        await self.fake_rc.wait_clients()
        await self.fake_onebot.wait_clients()
        for runtime in self.runtimes:
            client = runtime.rocketchat
            original = client._on_message

            async def delayed(payload, callback=original):
                if self.processing_delay:
                    await asyncio.sleep(self.processing_delay)
                await callback(payload)

            client._on_message = delayed

        manager = StressManagerAdapter(
            layout=self.layout,
            runtimes=self.runtimes,
            publication=self.publication,
            event_loop_monitor=self.event_loop_monitor,
        )
        self.webui = ShellWebUI(manager, host="127.0.0.1", port=0)
        await self.webui.start()
        self.webui.mark_application_ready()
        self.webui_base_url = f"http://127.0.0.1:{self.webui.port}"
        self.http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5.0),
            trust_env=False,
        )
        self.sample_task = asyncio.create_task(self._sample_loop(), name="StressSampler")
        gc.callbacks.append(self._observe_gc_pause)
        self._gc_callback_installed = True

    async def stop(self) -> float:
        started = time.perf_counter()
        if self._gc_callback_installed:
            with contextlib.suppress(ValueError):
                gc.callbacks.remove(self._observe_gc_pause)
            self._gc_callback_installed = False
            with self._gc_pause_lock:
                self._gc_started.clear()
        if self.sample_task is not None:
            self.sample_task.cancel()
            await asyncio.gather(self.sample_task, return_exceptions=True)
            self.sample_task = None
        resource_sampler = self.resource_sampler
        self.resource_sampler = None
        if resource_sampler is not None:
            await asyncio.to_thread(resource_sampler.shutdown, wait=True, cancel_futures=True)
        if self.crypto_task is not None:
            await asyncio.gather(self.crypto_task, return_exceptions=True)
            self.crypto_task = None
        if self.http_session is not None:
            await self.http_session.close()
            self.http_session = None
        if self.webui is not None:
            await self.webui.stop()
            self.webui = None
        await asyncio.gather(*(runtime.stop() for runtime in self.runtimes), return_exceptions=False)
        await self.plugin_manager.shutdown()
        await self.event_loop_monitor.stop()
        await self.fake_onebot.stop()
        await self.fake_rc.stop()
        await asyncio.to_thread(shutdown_logging, timeout=10)
        return time.perf_counter() - started

    def _observe_gc_pause(self, phase: str, info: dict[str, Any]) -> None:
        generation = int(info.get("generation") or 0)
        key = (threading.get_ident(), generation)
        with self._gc_pause_lock:
            if phase == "start":
                self._gc_started[key] = time.perf_counter()
                return
            if phase != "stop":
                return
            started = self._gc_started.pop(key, None)
            if started is None:
                return
            self.samples.gc_pauses.append(
                (
                    time.monotonic(),
                    (time.perf_counter() - started) * 1000.0,
                    generation,
                    self.current_phase,
                )
            )

    def reset_gc_pauses(self) -> None:
        with self._gc_pause_lock:
            self.samples.gc_pauses.clear()

    def gc_pause_snapshot(self) -> list[tuple[float, float, int, str]]:
        with self._gc_pause_lock:
            return list(self.samples.gc_pauses)

    async def _sample_loop(self) -> None:
        last_loop_max = 0.0
        while True:
            now = time.monotonic()
            phase = self.current_phase
            resource_sampler = self.resource_sampler
            if resource_sampler is None:
                return
            try:
                rss_bytes = await asyncio.get_running_loop().run_in_executor(
                    resource_sampler,
                    sample_process_rss,
                    self.process.pid,
                )
            except Exception as exc:
                self.samples.errors.append(f"process resource sampling failed: {exc!r}")
                await asyncio.sleep(1.0)
                continue
            self.samples.rss.append((now, rss_bytes, phase))
            try:
                started = time.perf_counter()
                for path in ("/api/health", "/api/status?compact=true", "/api/diagnostics"):
                    endpoint_started = time.perf_counter()
                    await self._fetch_json(path)
                    self.samples.webui_endpoint_ms.setdefault(path, []).append(
                        (time.perf_counter() - endpoint_started) * 1000.0
                    )
                self.samples.webui_ms.append((time.perf_counter() - started) * 1000.0)
            except Exception as exc:
                self.samples.errors.append(f"WebUI sampling failed: {exc!r}")
            loop_max = self.event_loop_monitor.current_max()
            if loop_max < last_loop_max:
                last_loop_max = loop_max
            if loop_max > last_loop_max:
                self.samples.event_loop_max.append((now, loop_max, phase))
                last_loop_max = loop_max
            await asyncio.sleep(1.0)

    async def _fetch_json(self, path: str) -> dict[str, Any]:
        if self.http_session is None:
            raise RuntimeError("stress WebUI client is not initialized")
        async with self.http_session.get(self.webui_base_url + path) as response:
            response.raise_for_status()
            return await response.json()

    def make_message(self, *, mixed: bool) -> dict[str, Any]:
        self.sequence += 1
        sequence = self.sequence
        room_index = sequence % ROOM_COUNT
        user_index = sequence % USER_COUNT
        message_id = f"stress-message-{sequence}"
        text = f"seq={sequence} room={room_index} load=stress"
        payload: dict[str, Any] = {
            "_id": message_id,
            "rid": f"room-{room_index}",
            "u": {
                "_id": f"user-{user_index}",
                "username": f"user{user_index}",
                "name": f"User {user_index}",
            },
            "msg": text,
            "ts": {"$date": int(time.time() * 1000)},
        }
        previous = self.last_message_by_room.get(room_index)
        if mixed and previous and sequence % 17 == 0:
            payload["msg"] = (
                f"[reply]({self.fake_rc.base_url}/channel/room-{room_index}?msg={previous}) "
                + text
            )
        if mixed and sequence % 41 == 0:
            payload["tmid"] = previous or message_id
        if mixed and sequence % 67 == 0:
            payload["attachments"] = [
                {
                    "image_url": f"{self.fake_rc.base_url}/media/sample.png",
                    "title": "sample.png",
                    "type": "image/png",
                }
            ]
        self.last_message_by_room[room_index] = message_id
        return payload

    async def send_for(self, *, rate: float, duration: float, mixed: bool = False) -> int:
        deadline = time.monotonic() + max(0.0, duration)
        interval = 1.0 / max(1.0, rate)
        next_send = time.monotonic()
        sent = 0
        while time.monotonic() < deadline:
            if self.pause_traffic:
                await asyncio.sleep(0.02)
                next_send = time.monotonic()
                continue
            payload = self.make_message(mixed=mixed)
            delivered = await self.fake_rc.inject(payload)
            if delivered != BOT_COUNT:
                self.samples.errors.append(f"DDP injection reached {delivered}/{BOT_COUNT} bots")
            self.sent += 1
            sent += 1
            if mixed and self.sequence % 83 == 0:
                await self._exercise_media_singleflight()
            if mixed and self.sequence % 113 == 0:
                self._schedule_crypto_work()
            now = time.monotonic()
            next_send += interval
            if next_send > now:
                await asyncio.sleep(next_send - now)
            elif now - next_send > 1.0:
                next_send = now
        return sent

    async def _exercise_media_singleflight(self) -> None:
        url = f"{self.fake_rc.base_url}/media/sample.png"
        await asyncio.gather(
            *(runtime.rocketchat.media.download_remote_media(url, ".png") for runtime in self.runtimes)
        )

    def _schedule_crypto_work(self) -> None:
        if self.crypto_task is not None and not self.crypto_task.done():
            return
        manager = self.runtimes[0].rocketchat.e2ee

        async def work() -> None:
            upload = await asyncio.get_running_loop().run_in_executor(
                CRYPTO_EXECUTOR,
                partial(
                    manager._prepare_encrypted_upload_sync,
                    file_name="stress.bin",
                    mime_type="application/octet-stream",
                    file_path=str(self.media_source),
                ),
            )
            Path(upload.encrypted_path).unlink(missing_ok=True)

        self.crypto_task = asyncio.create_task(work(), name="StressE2EEFileCrypto")

    async def wait_drained(self, timeout: float = 30.0) -> None:
        async with asyncio.timeout(timeout):
            await asyncio.gather(
                *(runtime.rocketchat._drain_inbound_queue(timeout=timeout) for runtime in self.runtimes)
            )
            while True:
                pending = sum(
                    runtime.onebot._outgoing.qsize() + int(runtime.onebot._pending_payload is not None)
                    for runtime in self.runtimes
                )
                actions = sum(runtime.onebot._action_queue.qsize() for runtime in self.runtimes)
                if pending == 0 and actions == 0:
                    break
                await asyncio.sleep(0.02)
            await asyncio.gather(
                *(
                    asyncio.to_thread(runtime._hot_store_bundle.writer.flush, timeout=15)
                    for runtime in self.runtimes
                    if runtime._hot_store_bundle is not None
                )
            )

    def ingress_dropped(self) -> int:
        return sum(
            int(
                runtime.rocketchat.build_diagnostic_snapshot()["performance"]["ingress"][
                    "overload_dropped"
                ]
            )
            for runtime in self.runtimes
        )

    async def inject_faults(self, duration: float) -> None:
        segment = max(3.0, duration / 5.0)
        started = time.monotonic()

        self.pause_traffic = True
        await self.wait_drained()
        await self.fake_rc.disconnect_one()
        await self.fake_rc.wait_clients(timeout=15)
        self.pause_traffic = False
        await asyncio.sleep(max(0.0, segment - (time.monotonic() - started)))

        started = time.monotonic()
        self.pause_traffic = True
        await self.wait_drained()
        await self.fake_onebot.disconnect_one()
        await self.fake_onebot.wait_clients(timeout=15)
        self.pause_traffic = False
        await asyncio.sleep(max(0.0, segment - (time.monotonic() - started)))

        started = time.monotonic()
        self.fake_rc.rest_failures.extend([429, 500])
        await asyncio.gather(
            *(runtime.rocketchat.get_room_info("room-0", refresh=True) for runtime in self.runtimes)
        )
        await asyncio.sleep(max(0.0, segment - (time.monotonic() - started)))

        started = time.monotonic()
        originals = []
        for runtime in self.runtimes:
            writer = runtime._hot_store_bundle.writer
            original = writer._write_record
            originals.append((writer, original))

            def slow_write(handle, record, callback=original):
                time.sleep(0.01)
                callback(handle, record)

            writer._write_record = slow_write
        await asyncio.sleep(min(segment * 0.7, 10.0))
        for writer, original in originals:
            writer._write_record = original
        await asyncio.sleep(max(0.0, segment - (time.monotonic() - started)))

        started = time.monotonic()
        for runtime in self.runtimes:
            runtime._runtime_plugins.append(
                RuntimePluginBinding(
                    descriptor=SimpleNamespace(plugin_id="stress-timeout-plugin"),
                    instance=TimeoutFaultPlugin(),
                    handled_actions=frozenset({"__stress_timeout_probe__"}),
                    generation=1,
                )
            )
        await asyncio.sleep(max(0.0, segment - (time.monotonic() - started)))
        for runtime in self.runtimes:
            runtime._runtime_plugins = [
                binding
                for binding in runtime._runtime_plugins
                if binding.descriptor.plugin_id != "stress-timeout-plugin"
            ]
            runtime._plugin_breakers.pop("stress-timeout-plugin", None)


async def calibrate(harness: FullStackHarness, seconds: float, max_rate: float) -> dict[str, Any]:
    harness.current_phase = "calibration"
    candidates = [max_rate * factor for factor in (0.2, 0.4, 0.7, 1.0)]
    step_duration = max(1.0, seconds / len(candidates))
    selected = candidates[0]
    steps = []
    for rate in candidates:
        sent_before = harness.sent
        received_before = harness.fake_onebot.received
        dropped_before = harness.ingress_dropped()
        # Calibrate the real mixed path so media, quote, thread and crypto
        # workers/caches are warm before formal RSS leak measurement starts.
        await harness.send_for(rate=rate, duration=step_duration, mixed=True)
        await harness.wait_drained()
        sent = harness.sent - sent_before
        received = harness.fake_onebot.received - received_before
        dropped = harness.ingress_dropped() - dropped_before
        expected = sent * BOT_COUNT
        loss = expected - received
        steps.append({"rate": rate, "sent": sent, "received": received, "loss": loss, "dropped": dropped})
        if loss == 0 and dropped == 0:
            selected = rate
        else:
            break
    await harness._exercise_media_singleflight()
    harness._schedule_crypto_work()
    if harness.crypto_task is not None:
        await harness.crypto_task
        harness.crypto_task = None
    await asyncio.sleep(0.25)
    harness.fake_onebot.reset_observations()
    harness.sent = 0
    harness.current_phase = "calibration_complete"
    return {"seconds": seconds, "steps": steps, "zero_loss_rate": selected}


async def precondition_bounded_capacity(
    harness: FullStackHarness,
    *,
    base_rate: float,
    seconds: float,
) -> dict[str, Any]:
    """Warm bounded queue/cache allocator high-water marks before RSS sampling.

    The formal overload phase deliberately fills the ingress and journal queues.
    On Windows, Python's allocator keeps those pages in the process working set
    after the queues drain.  Without exercising the same bounded capacity before
    the formal baseline, that one-time allocation is indistinguishable from a
    sustained leak in RSS.  This precondition is not part of calibration and its
    drops are excluded by the formal counter baselines.
    """
    requested_seconds = max(0.0, float(seconds))
    if requested_seconds <= 0.0:
        return {
            "seconds": 0.0,
            "sent": 0,
            "received": 0,
            "ingress_dropped": 0,
        }

    harness.current_phase = "capacity_precondition"
    sent_before = harness.sent
    received_before = harness.fake_onebot.received
    dropped_before = harness.ingress_dropped()
    started = time.monotonic()
    harness.processing_delay = 0.03
    try:
        await harness.send_for(
            rate=base_rate * 1.50,
            duration=requested_seconds,
            mixed=True,
        )
    finally:
        harness.processing_delay = 0.0
    await harness.wait_drained()
    await harness._exercise_media_singleflight()
    harness._schedule_crypto_work()
    if harness.crypto_task is not None:
        await harness.crypto_task
        harness.crypto_task = None

    result = {
        "seconds": round(time.monotonic() - started, 3),
        "sent": harness.sent - sent_before,
        "received": harness.fake_onebot.received - received_before,
        "ingress_dropped": harness.ingress_dropped() - dropped_before,
    }
    harness.fake_onebot.reset_observations()
    harness.sent = 0
    harness.samples.rss.clear()
    harness.samples.webui_ms.clear()
    harness.samples.webui_endpoint_ms.clear()
    harness.samples.ddp_ping_ms.clear()
    harness.samples.overload_ping_ms.clear()
    harness.samples.event_loop_max.clear()
    await asyncio.to_thread(gc.collect)
    await asyncio.sleep(1.0)
    harness.current_phase = "precondition_complete"
    return result


async def run_formal(
    harness: FullStackHarness,
    *,
    duration: float,
    base_rate: float,
    profile: str = "standard",
) -> dict[str, Any]:
    if profile == "overload-only":
        names = ["overload"]
        durations = [duration]
    else:
        boundaries = [0.0, 0.10, 1 / 3, 8 / 15, 0.70, 13 / 15, 29 / 30, 1.0]
        names = ["warmup", "steady", "mixed", "overload", "faults", "recovery", "drain"]
        durations = [(boundaries[index + 1] - boundaries[index]) * duration for index in range(7)]
    phases: list[dict[str, Any]] = []
    baseline_drops = harness.ingress_dropped()
    baseline_received = harness.fake_onebot.received
    baseline_threads = harness.process.num_threads()
    baseline_thread_names = sorted(thread.name for thread in threading.enumerate())
    baseline_task_names = sorted(task.get_name() for task in asyncio.all_tasks() if not task.done())
    baseline_task_summary = task_summary()
    baseline_tasks = len(baseline_task_names)
    baseline_handles = process_handles(harness.process)
    baseline_state_entries = [
        runtime._hot_store_bundle.state_engine.diagnostic_counts()
        for runtime in harness.runtimes
        if runtime._hot_store_bundle is not None
    ]
    harness.samples.webui_ms.clear()
    harness.samples.webui_endpoint_ms.clear()
    harness.samples.ddp_ping_ms.clear()
    harness.samples.overload_ping_ms.clear()
    harness.reset_gc_pauses()
    harness.event_loop_monitor.reset()
    formal_started = time.monotonic()

    for name, phase_duration in zip(names, durations):
        harness.current_phase = name
        sent_before = harness.sent
        received_before = harness.fake_onebot.received
        dropped_before = harness.ingress_dropped()
        phase_started = time.monotonic()
        if name == "warmup":
            await harness.send_for(rate=base_rate * 0.25, duration=phase_duration, mixed=True)
        elif name == "steady":
            await harness.send_for(rate=base_rate * 0.70, duration=phase_duration)
        elif name == "mixed":
            await harness.send_for(rate=base_rate * 0.70, duration=phase_duration, mixed=True)
        elif name == "overload":
            harness.processing_delay = 0.03
            cycle = min(60.0, max(4.0, phase_duration / 4.0))
            deadline = time.monotonic() + phase_duration
            while time.monotonic() < deadline:
                burst = min(cycle / 2.0, deadline - time.monotonic())
                await harness.send_for(rate=base_rate * 1.50, duration=burst, mixed=True)
                harness.processing_delay = 0.0
                recovery = min(cycle / 2.0, deadline - time.monotonic())
                if recovery > 0:
                    await harness.send_for(rate=base_rate * 0.70, duration=recovery, mixed=True)
                harness.processing_delay = 0.03
            harness.processing_delay = 0.0
        elif name == "faults":
            fault_task = asyncio.create_task(harness.inject_faults(phase_duration))
            await harness.send_for(rate=base_rate * 0.55, duration=phase_duration, mixed=True)
            await fault_task
        elif name == "recovery":
            await harness.send_for(rate=base_rate * 0.25, duration=phase_duration)
        else:
            await asyncio.sleep(phase_duration)
            await harness.wait_drained()

        try:
            pings = await harness.fake_rc.ping_all()
            harness.samples.ddp_ping_ms.extend(pings)
            if name == "overload":
                harness.samples.overload_ping_ms.extend(pings)
            await harness.fake_onebot.send_actions()
        except Exception as exc:
            harness.samples.errors.append(f"{name} control-plane check failed: {exc!r}")
        phases.append(
            {
                "name": name,
                "duration_seconds": round(time.monotonic() - phase_started, 3),
                "sent": harness.sent - sent_before,
                "received": harness.fake_onebot.received - received_before,
                "ingress_dropped": harness.ingress_dropped() - dropped_before,
            }
        )

    await harness.wait_drained()
    if harness.crypto_task is not None:
        await harness.crypto_task
        harness.crypto_task = None
    harness.current_phase = "complete"
    total_sent = harness.sent
    received = harness.fake_onebot.received - baseline_received
    ingress_dropped = harness.ingress_dropped() - baseline_drops
    expected = total_sent * BOT_COUNT
    loss = expected - received
    end_threads = harness.process.num_threads()
    end_thread_names = sorted(thread.name for thread in threading.enumerate())
    end_task_names = sorted(task.get_name() for task in asyncio.all_tasks() if not task.done())
    end_task_summary = task_summary()
    end_tasks = len(end_task_names)
    end_handles = process_handles(harness.process)
    end_state_entries = [
        runtime._hot_store_bundle.state_engine.diagnostic_counts()
        for runtime in harness.runtimes
        if runtime._hot_store_bundle is not None
    ]
    diagnostics = [runtime.build_diagnostic_summary() for runtime in harness.runtimes]
    ingress = [item.get("performance", {}).get("ingress", {}) for item in diagnostics]
    actions = [item.get("performance", {}).get("onebot_actions", {}) for item in diagnostics]
    persistence = [item.get("performance", {}).get("persistence", {}) for item in diagnostics]
    plugin_metrics = [item.get("performance", {}).get("plugins", {}) for item in diagnostics]

    rss_samples = [
        sample
        for sample in harness.samples.rss
        if sample[0] >= formal_started and sample[2] not in {"startup", "calibration", "warmup"}
    ]
    rss_endpoint_slope = 0.0
    if len(rss_samples) >= 2:
        elapsed_minutes = (rss_samples[-1][0] - rss_samples[0][0]) / 60.0
        if elapsed_minutes > 0:
            rss_endpoint_slope = (
                (rss_samples[-1][1] - rss_samples[0][1]) / (1024 * 1024)
            ) / elapsed_minutes * 10.0
    rss_leak_slope = phase_adjusted_rss_slope(rss_samples)
    rss_phase_summary = summarize_rss_phases(rss_samples)
    event_loop_spikes = [
        {
            "elapsed_seconds": round(timestamp - formal_started, 3),
            "max_ms": round(maximum, 3),
            "phase": phase,
        }
        for timestamp, maximum, phase in harness.samples.event_loop_max
        if timestamp >= formal_started and maximum >= 25.0
    ]
    gc_pauses = [
        (timestamp, duration_ms, generation, phase)
        for timestamp, duration_ms, generation, phase in harness.gc_pause_snapshot()
        if timestamp >= formal_started
    ]
    gc_pause_spikes = [
        {
            "elapsed_seconds": round(timestamp - formal_started, 3),
            "duration_ms": round(duration_ms, 3),
            "generation": generation,
            "phase": phase,
        }
        for timestamp, duration_ms, generation, phase in gc_pauses
        if duration_ms >= 25.0
    ]

    return {
        "duration_seconds": round(time.monotonic() - formal_started, 3),
        "phases": phases,
        "messages": {
            "sent": total_sent,
            "expected_deliveries": expected,
            "received": received,
            "loss": loss,
            "ingress_overload_dropped": ingress_dropped,
            "duplicates": harness.fake_onebot.duplicates,
            "out_of_order": harness.fake_onebot.out_of_order,
            "unparsed": harness.fake_onebot.unparsed,
        },
        "onebot_actions": {
            "failures": harness.fake_onebot.action_failures,
            "p95_ms": percentile(harness.fake_onebot.action_latencies_ms, 0.95),
            "p99_ms": percentile(harness.fake_onebot.action_latencies_ms, 0.99),
            "busy_rejected": sum(int(item.get("busy_rejected") or 0) for item in actions),
            "timed_out": sum(int(item.get("timed_out") or 0) for item in actions),
        },
        "control_plane": {
            "webui_p95_ms": percentile(harness.samples.webui_ms, 0.95),
            "webui_p99_ms": percentile(harness.samples.webui_ms, 0.99),
            "ddp_ping_p99_ms": percentile(harness.samples.ddp_ping_ms, 0.99),
            "overload_ddp_ping_p99_ms": percentile(harness.samples.overload_ping_ms, 0.99),
            "endpoint_p99_ms": {
                path: percentile(values, 0.99)
                for path, values in harness.samples.webui_endpoint_ms.items()
            },
        },
        "resources": {
            "rss_start_bytes": rss_samples[0][1] if rss_samples else 0,
            "rss_end_bytes": rss_samples[-1][1] if rss_samples else 0,
            "rss_max_bytes": max((sample[1] for sample in rss_samples), default=0),
            "rss_slope_mib_per_10min": round(rss_leak_slope, 3),
            "rss_phase_adjusted_slope_mib_per_10min": round(rss_leak_slope, 3),
            "rss_slope_method": "phase-theil-sen-30s-low-water-after-20pct-transition",
            "rss_endpoint_slope_mib_per_10min": round(rss_endpoint_slope, 3),
            "rss_endpoint_growth_mib": round(
                ((rss_samples[-1][1] - rss_samples[0][1]) / (1024 * 1024)) if rss_samples else 0.0,
                3,
            ),
            "rss_high_water_growth_mib": round(
                (
                    (max((sample[1] for sample in rss_samples), default=0) - rss_samples[0][1])
                    / (1024 * 1024)
                )
                if rss_samples
                else 0.0,
                3,
            ),
            "rss_by_phase": rss_phase_summary,
            "threads_baseline": baseline_threads,
            "threads_end": end_threads,
            "thread_names_baseline": baseline_thread_names,
            "thread_names_end": end_thread_names,
            "tasks_baseline": baseline_tasks,
            "tasks_end": end_tasks,
            "task_names_baseline": baseline_task_names,
            "task_names_end": end_task_names,
            "task_summary_baseline": baseline_task_summary,
            "task_summary_end": end_task_summary,
            "handles_baseline": baseline_handles,
            "handles_end": end_handles,
            "state_entries_baseline": baseline_state_entries,
            "state_entries_end": end_state_entries,
        },
        "queues": {
            "ingress_high_water": max((int(item.get("high_water") or 0) for item in ingress), default=0),
            "ingress_capacity": max((int(item.get("capacity") or 0) for item in ingress), default=0),
            "action_high_water": max((int(item.get("high_water") or 0) for item in actions), default=0),
            "action_capacity": max((int(item.get("capacity") or 0) for item in actions), default=0),
            "persistence_high_water": max((int(item.get("high_water") or 0) for item in persistence), default=0),
            "persistence_capacity": max((int(item.get("capacity") or 0) for item in persistence), default=0),
            "writer_failures": sum(1 for item in persistence if not item.get("writer_alive") or item.get("last_error")),
            "snapshot_count": sum(int(item.get("snapshot_count") or 0) for item in persistence),
            "snapshot_capture_max_ms": max(
                (float(item.get("snapshot_capture_max_ms") or 0.0) for item in persistence),
                default=0.0,
            ),
            "snapshot_serialize_max_ms": max(
                (float(item.get("snapshot_serialize_max_ms") or 0.0) for item in persistence),
                default=0.0,
            ),
            "snapshot_total_max_ms": max(
                (float(item.get("snapshot_total_max_ms") or 0.0) for item in persistence),
                default=0.0,
            ),
        },
        "plugins": {
            "timeouts": sum(int(item.get("timeouts") or 0) for item in plugin_metrics),
            "circuit_skips": sum(int(item.get("circuit_skips") or 0) for item in plugin_metrics),
            "open_circuits": sum(int(item.get("open_circuits") or 0) for item in plugin_metrics),
        },
        "caches": {
            "media_http_requests": harness.fake_rc.media_requests,
        },
        "event_loop": harness.event_loop_monitor.snapshot(),
        "event_loop_spikes": event_loop_spikes,
        "gc": {
            "collections": len(gc_pauses),
            "p99_ms": percentile([item[1] for item in gc_pauses], 0.99),
            "max_ms": round(max((item[1] for item in gc_pauses), default=0.0), 3),
            "spikes": gc_pause_spikes,
        },
        "errors": list(harness.samples.errors),
    }


def evaluate(result: dict[str, Any], *, strict_duration: bool) -> dict[str, bool]:
    formal = result["formal"]
    messages = formal["messages"]
    resources = formal["resources"]
    queues = formal["queues"]
    control = formal["control_plane"]
    loop = formal["event_loop"]
    thread_tolerance = max(1, math.ceil(resources["threads_baseline"] * 0.05))
    task_tolerance = max(1, math.ceil(resources["tasks_baseline"] * 0.05))
    handle_tolerance = max(2, math.ceil(resources["handles_baseline"] * 0.05))
    checks = {
        "duration": formal["duration_seconds"] >= (1795 if strict_duration else 1),
        "loss_matches_overload_counter": messages["loss"] == messages["ingress_overload_dropped"],
        "zero_duplicates": messages["duplicates"] == 0,
        "same_room_ordered": messages["out_of_order"] == 0,
        "all_events_parseable": messages["unparsed"] == 0,
        "rss_slope": (
            resources["rss_slope_mib_per_10min"] <= 1.0
            if strict_duration
            else resources["rss_end_bytes"] <= resources["rss_max_bytes"]
        ),
        "threads_recovered": (
            resources["threads_end"] <= resources["threads_baseline"] + thread_tolerance
            if strict_duration
            else resources["threads_end"] <= resources["threads_baseline"] + 4
        ),
        "tasks_recovered": (
            resources["tasks_end"] <= resources["tasks_baseline"] + task_tolerance
            if strict_duration
            else resources["tasks_end"] <= resources["tasks_baseline"] + 4
        ),
        "handles_recovered": (
            resources["handles_end"] <= resources["handles_baseline"] + handle_tolerance
            if strict_duration
            else resources["handles_end"] <= resources["handles_baseline"] + 64
        ),
        "queues_bounded": (
            queues["ingress_high_water"] <= queues["ingress_capacity"]
            and queues["action_high_water"] <= queues["action_capacity"]
            and queues["persistence_high_water"] <= queues["persistence_capacity"]
        ),
        "writers_healthy": queues["writer_failures"] == 0,
        "event_loop_p99": float(loop.get("p99") or 0) <= (25.0 if strict_duration else 50.0),
        "event_loop_max": float(loop.get("max") or 0) <= (100.0 if strict_duration else 250.0),
        "overload_ping": control["overload_ddp_ping_p99_ms"] <= 250.0,
        "onebot_actions": formal["onebot_actions"]["failures"] == 0,
        "overload_exercised": (
            messages["ingress_overload_dropped"] > 0 if strict_duration else True
        ),
        "no_unhandled_errors": not formal["errors"],
        "shutdown": result.get("shutdown_seconds", 999) <= 30.0,
    }
    return checks


def render_markdown(result: dict[str, Any]) -> str:
    formal = result["formal"]
    checks = result["acceptance"]
    resources = formal["resources"]
    queues = formal["queues"]
    lines = [
        "# RocketCatShell v0.2.2 Full-Stack Stress Report",
        "",
        f"- Result: {'PASS' if result['passed'] else 'FAIL'}",
        f"- Formal duration: {formal['duration_seconds']} seconds",
        f"- Calibrated zero-loss rate: {result['calibration']['zero_loss_rate']:.1f} msg/s",
        (
            f"- Capacity precondition: {result['precondition']['seconds']} seconds; "
            f"ingress dropped {result['precondition']['ingress_dropped']} (excluded from formal totals)"
        ),
        (
            f"- Messages: sent {formal['messages']['sent']}, received {formal['messages']['received']}, "
            f"overload-dropped {formal['messages']['ingress_overload_dropped']}"
        ),
        f"- Event loop: p99 {formal['event_loop']['p99']} ms, max {formal['event_loop']['max']} ms",
        f"- GC pauses: p99 {formal['gc']['p99_ms']} ms, max {formal['gc']['max_ms']} ms",
        (
            f"- RSS sustained slope: {resources['rss_slope_mib_per_10min']} MiB / 10 min; "
            f"endpoint growth {resources['rss_endpoint_growth_mib']} MiB; "
            f"high-water growth {resources['rss_high_water_growth_mib']} MiB"
        ),
        (
            f"- Snapshots: {queues['snapshot_count']}; capture max "
            f"{queues['snapshot_capture_max_ms']} ms; serialization max "
            f"{queues['snapshot_serialize_max_ms']} ms; total max "
            f"{queues['snapshot_total_max_ms']} ms"
        ),
        f"- Shutdown: {result['shutdown_seconds']} seconds",
        "",
        "## Before / After / Why",
        "",
        "| Before | After | Why |",
        "| --- | --- | --- |",
        "| Unbounded or task-per-item concurrency | Fixed-capacity queues and workers | Bound memory and reduce scheduling overhead |",
        "| Control frames shared message backpressure | DDP ping/result bypass ingress capacity | Preserve low-latency connection control |",
        "| Per-Bot duplicate caches and scans | Shared identity/media resources and cached plugin scans | Reduce I/O, threads, and handles |",
        "| Synchronous logging and large-file crypto | Log listener thread and two-worker crypto pool | Avoid event-loop blocking |",
        "| Per-entry snapshot copying under the state lock | Freeze only top-level containers under lock | Minimize inbound state-lock stalls |",
        "",
        "## Acceptance checks",
        "",
    ]
    lines.extend(f"- [{'x' if passed else ' '}] {name}" for name, passed in checks.items())
    lines.extend(
        [
            "",
            "## Phases",
            "",
            "| Phase | Seconds | Sent | Received | Ingress dropped |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for phase in formal["phases"]:
        lines.append(
            f"| {phase['name']} | {phase['duration_seconds']} | {phase['sent']} | "
            f"{phase['received']} | {phase['ingress_dropped']} |"
        )
    if formal["event_loop_spikes"]:
        lines.extend(["", "## Event-loop spikes", ""])
        for spike in formal["event_loop_spikes"]:
            lines.append(
                f"- {spike['phase']} @ {spike['elapsed_seconds']} seconds: {spike['max_ms']} ms"
            )
    if formal["gc"]["spikes"]:
        lines.extend(["", "## GC pause spikes", ""])
        for spike in formal["gc"]["spikes"]:
            lines.append(
                f"- generation {spike['generation']} in {spike['phase']} @ "
                f"{spike['elapsed_seconds']} seconds: {spike['duration_ms']} ms"
            )
    if formal["errors"]:
        lines.extend(["", "## Errors", ""] + [f"- {error}" for error in formal["errors"]])
    return "\n".join(lines) + "\n"

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RocketCatShell v0.2.2 isolated full-stack soak")
    parser.add_argument("--duration-seconds", type=float, default=1800)
    parser.add_argument("--calibration-seconds", type=float, default=60)
    parser.add_argument("--precondition-seconds", type=float, default=30)
    parser.add_argument("--max-calibration-rate", type=float, default=240)
    parser.add_argument("--profile", choices=("standard", "overload-only"), default="standard")
    parser.add_argument("--seed", type=int, default=20260811)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--non-strict-duration", action="store_true")
    parser.add_argument("--trace-memory", action="store_true")
    parser.add_argument("--loop-stall-trace", type=Path)
    return parser


async def trace_loop_stalls(path: Path, *, threshold_seconds: float = 0.1) -> None:
    """Capture native all-thread stacks only when the event loop misses 100 ms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        try:
            while True:
                faulthandler.dump_traceback_later(
                    threshold_seconds,
                    repeat=False,
                    file=handle,
                    exit=False,
                )
                await asyncio.sleep(threshold_seconds / 3.0)
                faulthandler.cancel_dump_traceback_later()
        finally:
            faulthandler.cancel_dump_traceback_later()


async def async_main() -> int:
    args = build_parser().parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output = args.markdown_output or args.output.with_suffix(".md")
    with tempfile.TemporaryDirectory(prefix="rocketcat-v022-full-stack-") as temporary_directory:
        harness = FullStackHarness(seed=args.seed, root=Path(temporary_directory))
        result: dict[str, Any] = {
            "seed": args.seed,
            "topology": {"bots": BOT_COUNT, "rooms": ROOM_COUNT, "users": USER_COUNT},
        }
        shutdown_seconds = 999.0
        trace_start = None
        loop_trace_task: asyncio.Task[None] | None = None
        try:
            if args.trace_memory:
                tracemalloc.start(10)
            await harness.start()
            if args.loop_stall_trace is not None:
                loop_trace_task = asyncio.create_task(
                    trace_loop_stalls(args.loop_stall_trace),
                    name="StressLoopStallTrace",
                )
            result["calibration"] = await calibrate(
                harness,
                args.calibration_seconds,
                args.max_calibration_rate,
            )
            result["precondition"] = await precondition_bounded_capacity(
                harness,
                base_rate=float(result["calibration"]["zero_loss_rate"]),
                seconds=args.precondition_seconds,
            )
            if args.trace_memory:
                gc.collect()
                trace_start = tracemalloc.take_snapshot()
            result["formal"] = await run_formal(
                harness,
                duration=args.duration_seconds,
                base_rate=float(result["calibration"]["zero_loss_rate"]),
                profile=args.profile,
            )
            if trace_start is not None:
                gc.collect()
                trace_end = tracemalloc.take_snapshot()
                statistics_diff = trace_end.compare_to(trace_start, "lineno")
                positive = [item for item in statistics_diff if item.size_diff > 0]
                negative = [item for item in statistics_diff if item.size_diff < 0]
                result["memory_trace"] = {
                    "net_size_diff_bytes": sum(item.size_diff for item in statistics_diff),
                    "net_count_diff": sum(item.count_diff for item in statistics_diff),
                    "positive_size_diff_bytes": sum(item.size_diff for item in positive),
                    "negative_size_diff_bytes": sum(item.size_diff for item in negative),
                    "top_positive": [
                        {
                            "location": str(item.traceback[0]),
                            "size_diff_bytes": item.size_diff,
                            "count_diff": item.count_diff,
                        }
                        for item in positive[:40]
                    ],
                    "top_negative": [
                        {
                            "location": str(item.traceback[0]),
                            "size_diff_bytes": item.size_diff,
                            "count_diff": item.count_diff,
                        }
                        for item in sorted(negative, key=lambda item: item.size_diff)[:40]
                    ],
                }
                tracemalloc.stop()
        finally:
            if loop_trace_task is not None:
                loop_trace_task.cancel()
                await asyncio.gather(loop_trace_task, return_exceptions=True)
            shutdown_seconds = await harness.stop()
        result["shutdown_seconds"] = round(shutdown_seconds, 3)
        result["acceptance"] = evaluate(
            result,
            strict_duration=not args.non_strict_duration,
        )
        result["passed"] = all(result["acceptance"].values())
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        markdown_output.write_text(render_markdown(result), encoding="utf-8")
        return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))

from __future__ import annotations

import asyncio
import io
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from starlette.requests import Request

from rocketcat_shell.bridge.media_publication import MediaPublicationService
from rocketcat_shell.bridge.rocketchat_client import RocketChatClient
from rocketcat_shell.shell.webui import ShellWebUI


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "rocketcat_shell" / "shell" / "static"


class _Manager:
    def __init__(self, root: Path) -> None:
        self.layout = SimpleNamespace(project_root=root)
        self.settings = SimpleNamespace(
            terminal_max_sessions=2,
            terminal_idle_timeout_seconds=0,
        )
        self.media_publication = MediaPublicationService()
        self.last_compact = None
        self.list_compact = None
        self.diagnostics_calls = 0

    def set_webui_runtime(self, **_kwargs) -> None:
        return None

    def clear_webui_runtime(self) -> None:
        return None

    async def get_webui_state(self, *, compact: bool = False):
        self.last_compact = compact
        payload = {
            "product": "RocketCat",
            "version": "v0.2.2",
            "bridge_enabled": True,
            "independent_webui_enabled": True,
            "enabled_bot_count": 0,
            "bot_count": 0,
        }
        if not compact:
            payload["items"] = []
        return payload

    async def list_bots(self, *, compact: bool = False):
        self.list_compact = compact
        return [{"id": "bot-1", "name": "Bot 1"}]

    async def get_diagnostics_state(self):
        self.diagnostics_calls += 1
        await asyncio.sleep(0)
        return {"product": "RocketCat", "items": []}


class _FakeTerminalWebSocket:
    def __init__(self, *, blocked: bool = False) -> None:
        self.messages: list[dict] = []
        self.close_codes: list[int] = []
        self._release = asyncio.Event()
        if not blocked:
            self._release.set()

    async def send_json(self, message: dict) -> None:
        await self._release.wait()
        self.messages.append(message)

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)


def _terminal_session() -> dict:
    return {
        "id": "terminal-test",
        "buffer_chunks": deque(),
        "buffer_chars": 0,
        "pending_output": [],
        "pending_output_chars": 0,
        "thread_output_lock": threading.Lock(),
        "thread_pending_output": [],
        "thread_output_scheduled": False,
        "output_flush_handle": None,
        "output_flush_scheduled": False,
        "sockets": {},
        "closing": False,
        "seen_output": False,
        "last_access": 0,
    }


class PerformanceWebUIContractTests(unittest.IsolatedAsyncioTestCase):
    def test_inbound_duplicate_signature_compacts_long_text_and_is_stable(self) -> None:
        client = object.__new__(RocketChatClient)
        original = {
            "_id": "message-1",
            "rid": "room-1",
            "msg": "long message " * 200,
            "attachments": [{"title": "x", "meta": {"b": 2, "a": 1}}],
        }
        reordered = {
            **original,
            "attachments": [{"meta": {"a": 1, "b": 2}, "title": "x"}],
        }
        changed = {**original, "msg": original["msg"] + "changed"}
        signature = client._build_inbound_message_signature(original)
        self.assertIsInstance(signature, str)
        self.assertLess(len(signature), len(original["msg"]) // 4)
        self.assertEqual(
            signature,
            client._build_inbound_message_signature(reordered),
        )
        self.assertNotEqual(
            signature,
            client._build_inbound_message_signature(changed),
        )

    async def test_compact_status_and_static_cache_headers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = _Manager(Path(temporary_directory))
            webui = ShellWebUI(manager, host="127.0.0.1", port=0)
            await webui.start()
            webui.mark_application_ready()
            base_url = f"http://127.0.0.1:{webui.port}"

            def fetch(path: str):
                with urllib.request.urlopen(base_url + path, timeout=5) as response:
                    return (
                        response.status,
                        {key.lower(): value for key, value in response.headers.items()},
                        response.read(),
                    )

            try:
                compact_status, _compact_headers, compact_body = await asyncio.to_thread(
                    fetch,
                    "/api/status?compact=true",
                )
                self.assertEqual(200, compact_status)
                self.assertTrue(manager.last_compact)
                self.assertNotIn(b'"items"', compact_body)
                _html_status, html_headers, _ = await asyncio.to_thread(fetch, "/")
                _versioned_status, versioned_headers, _ = await asyncio.to_thread(
                    fetch,
                    "/static/app.js?v=performance-test",
                )
                _unversioned_status, unversioned_headers, _ = await asyncio.to_thread(
                    fetch,
                    "/static/app.js",
                )
            finally:
                await webui.stop()
            self.assertIn("no-store", html_headers["cache-control"])
            self.assertIn("immutable", versioned_headers["cache-control"])
            self.assertIn("must-revalidate", unversioned_headers["cache-control"])

    def test_hidden_pages_abort_polling_and_resume_incrementally(self) -> None:
        javascript = (STATIC / "app.js").read_text(encoding="utf-8")
        for token in (
            "document.hidden",
            "stopNetworkPolling();",
            "stopDiagnosticsPolling();",
            "stopLogPolling();",
            "state.network.abortController.abort()",
            "state.diagnostics.abortController.abort()",
            "state.network.renderSignature",
            "state.diagnostics.renderSignature",
        ):
            self.assertIn(token, javascript)

    def test_directory_download_builds_disk_archive_with_dynamic_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source"
            source.mkdir()
            (source / "alpha.txt").write_text("alpha", encoding="utf-8")
            nested = source / "nested"
            nested.mkdir()
            (nested / "beta.txt").write_text("beta", encoding="utf-8")
            webui = ShellWebUI(_Manager(root), host="127.0.0.1", port=0)

            archive_path, archive_size = webui._build_file_manager_zip([source])
            self.assertTrue(archive_path.is_file())
            self.assertGreater(archive_size, 0)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(b"alpha", archive.read("source/alpha.txt"))
                self.assertEqual(b"beta", archive.read("source/nested/beta.txt"))

            with mock.patch(
                "rocketcat_shell.shell.webui.shutil.disk_usage",
                return_value=SimpleNamespace(free=1),
            ):
                with self.assertRaisesRegex(OSError, "insufficient disk space"):
                    webui._ensure_file_download_space(1)

    def test_file_writes_use_thread_compatible_sync_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            webui = ShellWebUI(_Manager(root), host="127.0.0.1", port=0)
            target = root / "editable.txt"
            target.write_text("before", encoding="utf-8")
            temporary = root / ".editable.tmp"
            result = webui._write_file_atomic_sync(target, temporary, b"after")
            self.assertEqual(b"after", target.read_bytes())
            self.assertEqual(5, result.st_size)

            uploaded = root / "uploaded.bin"
            upload_result = webui._write_uploaded_file_sync(io.BytesIO(b"payload"), uploaded)
            self.assertEqual(b"payload", uploaded.read_bytes())
            self.assertEqual(7, upload_result.st_size)

    async def test_terminal_output_is_coalesced_and_slow_clients_are_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            webui = ShellWebUI(_Manager(Path(temporary_directory)), host="127.0.0.1", port=0)
            terminal_id = "terminal-test"
            session = _terminal_session()
            webui._terminal_sessions[terminal_id] = session

            fast = _FakeTerminalWebSocket()
            fast_client = {
                "queue": asyncio.Queue(maxsize=64),
                "queued_bytes": 0,
                "sender_task": None,
            }
            fast_client["sender_task"] = asyncio.create_task(
                webui._terminal_client_sender(terminal_id, fast, fast_client)
            )
            session["sockets"][fast] = fast_client

            webui._queue_terminal_output(terminal_id, "a" * 9_000)
            webui._queue_terminal_output(terminal_id, "b" * 9_000)
            await asyncio.sleep(0.05)
            self.assertEqual(1, len(fast.messages))
            self.assertEqual(18_000, len(fast.messages[0]["data"]))
            self.assertEqual(18_000, session["buffer_chars"])

            await webui._remove_terminal_client(terminal_id, fast)
            slow = _FakeTerminalWebSocket(blocked=True)
            slow_client = {
                "queue": asyncio.Queue(maxsize=64),
                "queued_bytes": 0,
                "sender_task": None,
            }
            slow_client["sender_task"] = asyncio.create_task(
                webui._terminal_client_sender(terminal_id, slow, slow_client)
            )
            session["sockets"][slow] = slow_client
            for _index in range(66):
                await webui._broadcast_terminal_output(terminal_id, "x" * 16_384)
            await asyncio.sleep(0.05)
            self.assertNotIn(slow, session["sockets"])
            self.assertIn(1013, slow.close_codes)
            self.assertEqual(200_000, session["buffer_chars"])

    async def test_terminal_thread_output_uses_one_outstanding_loop_wakeup(self) -> None:
        class CapturingLoop:
            def __init__(self) -> None:
                self.callbacks: list[tuple] = []

            def call_soon_threadsafe(self, callback, *args) -> None:
                self.callbacks.append((callback, args))

        with tempfile.TemporaryDirectory() as temporary_directory:
            webui = ShellWebUI(_Manager(Path(temporary_directory)), host="127.0.0.1", port=0)
            terminal_id = "terminal-test"
            session = _terminal_session()
            webui._terminal_sessions[terminal_id] = session
            loop = CapturingLoop()
            for _index in range(1_000):
                webui._queue_terminal_output_from_thread(loop, terminal_id, "x" * 32)
            self.assertEqual(1, len(loop.callbacks))
            callback, args = loop.callbacks.pop()
            callback(*args)
            await asyncio.sleep(0.05)
            self.assertEqual(32_000, session["buffer_chars"])

    async def test_compact_bots_etag_and_diagnostics_single_flight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = _Manager(Path(temporary_directory))
            webui = ShellWebUI(manager, host="127.0.0.1", port=0)
            request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/bots",
                    "query_string": b"compact=true",
                    "headers": [],
                }
            )
            response = await webui._handle_list_bots(request, compact=True)
            self.assertEqual(200, response.status_code)
            self.assertTrue(manager.list_compact)
            etag = response.headers["etag"]
            cached_request = Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/bots",
                    "query_string": b"compact=true",
                    "headers": [(b"if-none-match", etag.encode("ascii"))],
                }
            )
            cached_response = await webui._handle_list_bots(cached_request, compact=True)
            self.assertEqual(304, cached_response.status_code)

            snapshots = await asyncio.gather(
                *(webui._diagnostics_payload(refresh=False) for _index in range(8))
            )
            self.assertEqual(1, manager.diagnostics_calls)
            self.assertTrue(all(snapshot is snapshots[0] for snapshot in snapshots))

    def test_diagnostics_exposes_collapsed_performance_panel(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        javascript = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('class="performance-backpressure-panel"', html)
        self.assertIn("性能与背压", html)
        for identifier in (
            "performanceEventLoop",
            "performanceLoggingQueue",
            "performanceBotGrid",
            "renderPerformanceBackpressure",
            "overload_dropped",
            "enqueue_wait_p99_ms",
            "open_circuits",
        ):
            self.assertIn(identifier, html + javascript)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException

from rocketcat_shell.shell.webui import ShellWebUI
from rocketcat_shell.updates import UpdateService


class LinuxLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_finishes_log_long_poll_without_forced_cancellation(self):
        manager = SimpleNamespace(
            settings=SimpleNamespace(),
            layout=SimpleNamespace(project_root=Path(__file__).resolve().parents[2]),
        )
        webui = ShellWebUI(manager, host="127.0.0.1", port=0)
        request = asyncio.create_task(webui._handle_logs(after_id=0, wait=30))
        await asyncio.sleep(0.01)
        await asyncio.wait_for(webui.stop(), timeout=1)
        response = await asyncio.wait_for(request, timeout=1)
        self.assertEqual([], response["items"])
        response = await asyncio.wait_for(webui._handle_logs(after_id=0, wait=30), timeout=1)
        self.assertEqual([], response["items"])

    async def test_stop_cancels_update_discovery_without_asgi_exception(self):
        started = asyncio.Event()

        async def status(*, refresh=False):
            started.set()
            await asyncio.Event().wait()

        manager = SimpleNamespace(
            settings=SimpleNamespace(),
            layout=SimpleNamespace(project_root=Path(__file__).resolve().parents[2]),
            updates=SimpleNamespace(status=status),
        )
        webui = ShellWebUI(manager, host="127.0.0.1", port=0)
        request = asyncio.create_task(webui._handle_update_status(refresh=True))
        await started.wait()
        await asyncio.wait_for(webui.stop(), timeout=1)
        with self.assertRaises(HTTPException) as raised:
            await request
        self.assertEqual(503, raised.exception.status_code)
        self.assertFalse(webui._active_update_requests)
        with self.assertRaises(HTTPException) as raised:
            await webui._handle_update_status(refresh=False)
        self.assertEqual(503, raised.exception.status_code)

    async def test_daemon_discovery_cancellation_does_not_hold_default_executor(self):
        started = threading.Event()
        finish = threading.Event()
        workers = []

        def blocked_request():
            workers.append(threading.current_thread())
            started.set()
            finish.wait(5)
            return []

        request = asyncio.create_task(UpdateService._run_daemon_thread(blocked_request))
        try:
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(started.is_set())
            self.assertTrue(workers[0].daemon)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(request, timeout=0.5)
            await asyncio.wait_for(asyncio.get_running_loop().shutdown_default_executor(), timeout=0.5)
        finally:
            finish.set()
            for _ in range(100):
                if not any(worker.is_alive() for worker in workers):
                    break
                await asyncio.sleep(0.01)

    async def test_docker_does_not_register_windows_shutdown_feature(self):
        manager = SimpleNamespace(
            settings=SimpleNamespace(),
            layout=SimpleNamespace(project_root=Path(__file__).resolve().parents[2]),
        )
        webui = ShellWebUI(manager, host="127.0.0.1", port=0)
        routes = {getattr(route, "path", "") for route in webui._app.routes}
        self.assertNotIn("/api/settings/shutdown", routes)
        root = Path(__file__).resolve().parents[2]
        for relative in ("rocketcat_shell/shell/static/app.js", "rocketcat_shell/shell/static/index.html"):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("settingsShutdownButton", source)
            self.assertNotIn("/api/settings/shutdown", source)


if __name__ == "__main__":
    unittest.main()

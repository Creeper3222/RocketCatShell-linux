from __future__ import annotations

import asyncio
import tempfile
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from rocketcat_shell.bridge.media_publication import MediaPublicationService
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


class PerformanceWebUIContractTests(unittest.IsolatedAsyncioTestCase):
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

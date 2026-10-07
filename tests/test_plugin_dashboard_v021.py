from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

from starlette.requests import Request

from rocketcat_shell.layout import ProjectLayout
from rocketcat_shell.plugin_system import DashboardRequest
from rocketcat_shell.plugin_system.manager import RocketCatPluginManager
from rocketcat_shell.shell.manager import ShellManager
from rocketcat_shell.shell.webui import ShellWebUI


def _build_layout(root: Path) -> ProjectLayout:
    return ProjectLayout(
        project_root=root,
        package_root=root / "rocketcat_shell",
        config_dir=root / "config",
        plugins_config_dir=root / "config" / "plugins_config",
        data_dir=root / "data",
        temp_dir=root / "data" / "temp",
        bots_dir=root / "data" / "bots",
        plugins_dir=root / "data" / "plugins",
        plugin_data_dir=root / "data" / "plugin_data",
        logs_dir=root / "logs",
        shell_settings_path=root / "config" / "shell.json",
        bot_registry_path=root / "config" / "bots.json",
        log_file_path=root / "logs" / "rocketcat.log",
    )


def _write_plugin(
    layout: ProjectLayout,
    plugin_id: str,
    *,
    main_source: str | None = None,
    pages: tuple[str, ...] = (),
    dashboard_page: str | None = None,
) -> Path:
    plugin_dir = layout.plugins_dir / plugin_id
    plugin_dir.mkdir(parents=True, exist_ok=True)
    metadata = [
        f"name: {plugin_id}",
        f"display_name: {plugin_id}",
        "version: v0.2.1",
    ]
    if dashboard_page:
        metadata.append(f"dashboard_page: {dashboard_page}")
    (plugin_dir / "metadata.yaml").write_text(
        "\n".join(metadata) + "\n",
        encoding="utf-8",
    )
    if main_source is not None:
        (plugin_dir / "main.py").write_text(
            textwrap.dedent(main_source),
            encoding="utf-8",
        )
    for page_name in pages:
        page_dir = plugin_dir / "pages" / page_name
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "index.html").write_text(
            f"<html><head></head><body>{page_name}</body></html>",
            encoding="utf-8",
        )
    return plugin_dir


def _runtime(name: str):
    return SimpleNamespace(
        instance_name=name,
        bridge_config=SimpleNamespace(bot_id=name),
    )


def _request(
    method: str,
    path: str,
    *,
    query: str = "",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> Request:
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    normalized_headers = {
        "content-length": str(len(body)),
        **(headers or {}),
    }
    return Request(
        {
            "type": "http",
            "method": method.upper(),
            "path": path,
            "query_string": query.encode("utf-8"),
            "headers": [
                (key.lower().encode("latin-1"), value.encode("latin-1"))
                for key, value in normalized_headers.items()
            ],
        },
        receive,
    )


class PluginGlobalLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_runtimes_share_one_global_instance(self):
        source = """
            from rocketcat_shell.plugin_system.base import RocketCatPlugin

            class Plugin(RocketCatPlugin):
                def __init__(self, context, config):
                    super().__init__(context, config)
                    self.initialized = 0
                    self.loaded = []
                    self.unloaded = []

                async def on_initialize(self):
                    self.initialized += 1

                async def on_load(self, runtime):
                    self.loaded.append(runtime.instance_name)

                async def on_unload(self, runtime):
                    self.unloaded.append(runtime.instance_name)
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            layout = _build_layout(Path(temp_dir))
            layout.ensure_directories()
            _write_plugin(layout, "rocketcat_plugin_singleton", main_source=source)
            manager = RocketCatPluginManager(layout)
            await manager.initialize()

            runtime_a = _runtime("bot-a")
            runtime_b = _runtime("bot-b")
            bindings_a = await manager.create_runtime_plugins(runtime_a)
            bindings_b = await manager.create_runtime_plugins(runtime_b)

            self.assertEqual(1, len(bindings_a))
            self.assertEqual(1, len(bindings_b))
            self.assertIs(bindings_a[0].instance, bindings_b[0].instance)
            self.assertEqual(1, bindings_a[0].instance.initialized)
            self.assertEqual(["bot-a", "bot-b"], bindings_a[0].instance.loaded)

            await manager.shutdown_runtime_plugins(bindings_a, runtime_a)
            self.assertEqual(["bot-a"], bindings_b[0].instance.unloaded)
            self.assertIn("rocketcat_plugin_singleton", manager._states)
            await manager.shutdown()

    async def test_reload_reuses_binding_containers_and_rolls_back_failure(self):
        source = """
            from rocketcat_shell.plugin_system.base import RocketCatPlugin

            class Plugin(RocketCatPlugin):
                async def on_load(self, runtime):
                    return None
        """
        failing_source = """
            from rocketcat_shell.plugin_system.base import RocketCatPlugin

            class Plugin(RocketCatPlugin):
                async def on_load(self, runtime):
                    if runtime.instance_name == "bot-b":
                        raise RuntimeError("candidate rejected")
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            layout = _build_layout(Path(temp_dir))
            layout.ensure_directories()
            plugin_dir = _write_plugin(
                layout,
                "rocketcat_plugin_atomic",
                main_source=source,
                pages=("dashboard",),
            )
            manager = RocketCatPluginManager(layout)
            await manager.initialize()
            runtime_a = _runtime("bot-a")
            runtime_b = _runtime("bot-b")
            bindings_a = await manager.create_runtime_plugins(runtime_a)
            bindings_b = await manager.create_runtime_plugins(runtime_b)
            original_instance = bindings_a[0].instance
            original_binding_a = bindings_a[0]
            original_binding_b = bindings_b[0]
            dashboard_session = await manager.issue_dashboard_session(
                "rocketcat_plugin_atomic",
                "dashboard",
            )

            (plugin_dir / "main.py").write_text(
                textwrap.dedent(failing_source),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "candidate rejected"):
                await manager.reload_plugin("rocketcat_plugin_atomic")

            self.assertIs(original_binding_a, bindings_a[0])
            self.assertIs(original_binding_b, bindings_b[0])
            self.assertIs(original_instance, bindings_a[0].instance)
            self.assertIs(original_instance, bindings_b[0].instance)
            _, page, _ = await manager.resolve_dashboard_asset(
                dashboard_session.plugin_id,
                dashboard_session.page_name,
                dashboard_session.token,
                "index.html",
            )
            self.assertEqual("dashboard", page.name)
            await manager.shutdown()


class PluginDashboardManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_only_plugin_discovery_and_token_security(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            layout = _build_layout(Path(temp_dir))
            layout.ensure_directories()
            _write_plugin(
                layout,
                "rocketcat_plugin_pages",
                pages=("alpha", "dashboard"),
                dashboard_page="alpha",
            )
            _write_plugin(layout, "rocketcat_plugin_plain")
            manager = RocketCatPluginManager(layout)
            await manager.initialize()

            summaries = {item["id"]: item for item in manager.list_plugins()}
            pages = summaries["rocketcat_plugin_pages"]
            plain = summaries["rocketcat_plugin_plain"]
            self.assertTrue(pages["has_dashboard"])
            self.assertFalse(pages["runtime_available"])
            self.assertEqual("alpha", pages["default_page"])
            self.assertFalse(plain["has_dashboard"])

            session = await manager.issue_dashboard_session(
                "rocketcat_plugin_pages",
                "dashboard",
            )
            _, page, asset = await manager.resolve_dashboard_asset(
                session.plugin_id,
                session.page_name,
                session.token,
                "index.html",
            )
            self.assertEqual("dashboard", page.name)
            self.assertTrue(asset.is_file())
            with self.assertRaises(FileNotFoundError):
                await manager.resolve_dashboard_asset(
                    session.plugin_id,
                    session.page_name,
                    "invalid-token",
                    "index.html",
                )
            with self.assertRaises((ValueError, FileNotFoundError)):
                await manager.resolve_dashboard_asset(
                    session.plugin_id,
                    session.page_name,
                    session.token,
                    "../metadata.yaml",
                )
            await manager.revoke_dashboard_session(session.token)
            with self.assertRaises(FileNotFoundError):
                await manager.resolve_dashboard_asset(
                    session.plugin_id,
                    session.page_name,
                    session.token,
                    "index.html",
                )
            await manager.shutdown()

    async def test_registered_api_path_parameters_and_sse_cancellation(self):
        source = """
            import asyncio
            from rocketcat_shell.plugin_system.base import RocketCatPlugin

            class Plugin(RocketCatPlugin):
                async def on_initialize(self):
                    self.context.register_dashboard_api(
                        "echo/{name}",
                        self.echo,
                        methods={"POST"},
                    )
                    self.context.register_dashboard_sse("events", self.events)

                async def echo(self, request):
                    return {
                        "name": request.path_params["name"],
                        "body": request.json_value,
                    }

                async def events(self, request):
                    async def generate():
                        while True:
                            await asyncio.sleep(60)
                            yield {"ok": True}
                    return generate()
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            layout = _build_layout(Path(temp_dir))
            layout.ensure_directories()
            _write_plugin(
                layout,
                "rocketcat_plugin_api",
                main_source=source,
                pages=("dashboard",),
            )
            manager = RocketCatPluginManager(layout)
            await manager.initialize()
            _state, route, params = await manager.resolve_dashboard_api(
                "rocketcat_plugin_api",
                "echo/rocketcat",
                "POST",
            )
            result = await route.handler(
                DashboardRequest(
                    method="POST",
                    path="echo/rocketcat",
                    path_params=params,
                    json_value={"value": 21},
                )
            )
            self.assertEqual(
                {"name": "rocketcat", "body": {"value": 21}},
                result,
            )

            started = asyncio.Event()

            async def active_stream():
                started.set()
                await asyncio.sleep(60)

            task = asyncio.create_task(active_stream())
            await manager.register_dashboard_sse_task(
                "rocketcat_plugin_api",
                task,
            )
            await started.wait()
            await manager.reload_plugin("rocketcat_plugin_api")
            await asyncio.sleep(0)
            self.assertTrue(task.cancelled())
            await manager.shutdown()


class PluginDashboardWebUITests(unittest.IsolatedAsyncioTestCase):
    async def test_static_page_bridge_headers_and_api_upload(self):
        source = """
            from rocketcat_shell.plugin_system import DashboardFileResponse
            from rocketcat_shell.plugin_system.base import RocketCatPlugin

            class Plugin(RocketCatPlugin):
                async def on_initialize(self):
                    self.context.data_dir.mkdir(parents=True, exist_ok=True)
                    (self.context.data_dir / "download.txt").write_text(
                        "rocketcat-download",
                        encoding="utf-8",
                    )
                    self.context.register_dashboard_api(
                        "inspect",
                        self.inspect_request,
                        methods={"GET", "POST"},
                    )
                    self.context.register_dashboard_api(
                        "download",
                        self.download,
                        methods={"GET"},
                    )
                    self.context.register_dashboard_sse(
                        "events",
                        self.events,
                    )

                async def inspect_request(self, request):
                    return {
                        "method": request.method,
                        "query": request.query,
                        "json": request.json_value,
                        "file_count": sum(len(items) for items in request.files.values()),
                        "form": request.form,
                    }

                async def download(self, request):
                    return DashboardFileResponse(
                        self.context.data_dir / "download.txt",
                        filename="download.txt",
                        media_type="text/plain",
                    )

                async def events(self, request):
                    async def generate():
                        yield {"tick": 1}
                    return generate()
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            layout = _build_layout(Path(temp_dir))
            layout.ensure_directories()
            _write_plugin(
                layout,
                "rocketcat_plugin_web",
                main_source=source,
                pages=("dashboard",),
            )
            shell_manager = ShellManager(layout)
            await shell_manager.initialize(start_runtimes=False)
            webui = ShellWebUI(shell_manager, host="127.0.0.1", port=5751)

            created = await webui._handle_create_plugin_dashboard_session(
                "rocketcat_plugin_web",
                {"page": "dashboard"},
            )
            page = await webui._handle_plugin_dashboard_asset(
                "rocketcat_plugin_web",
                "dashboard",
                created["token"],
                "index.html",
            )
            self.assertIn("__rocketcat_bridge__.js", page.body.decode("utf-8"))
            self.assertEqual("no-store", page.headers["cache-control"])
            self.assertEqual("nosniff", page.headers["x-content-type-options"])
            self.assertIn("frame-ancestors 'self'", page.headers["content-security-policy"])

            get_response = await webui._handle_plugin_dashboard_api(
                "rocketcat_plugin_web",
                "inspect",
                _request(
                    "GET",
                    "/api/plugins/rocketcat_plugin_web/dashboard/api/inspect",
                    query="value=1",
                ),
            )
            get_payload = json.loads(get_response.body)
            self.assertEqual(["1"], get_payload["query"]["value"])

            post_body = json.dumps({"value": 2}).encode("utf-8")
            post_response = await webui._handle_plugin_dashboard_api(
                "rocketcat_plugin_web",
                "inspect",
                _request(
                    "POST",
                    "/api/plugins/rocketcat_plugin_web/dashboard/api/inspect",
                    headers={"content-type": "application/json"},
                    body=post_body,
                ),
            )
            self.assertEqual({"value": 2}, json.loads(post_response.body)["json"])

            boundary = "RocketCatBoundary"
            multipart_body = (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="label"\r\n\r\n'
                "demo\r\n"
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="files"; filename="demo.txt"\r\n'
                "Content-Type: text/plain\r\n\r\n"
                "rocketcat\r\n"
                f"--{boundary}--\r\n"
            ).encode("utf-8")
            upload_response = await webui._handle_plugin_dashboard_api(
                "rocketcat_plugin_web",
                "inspect",
                _request(
                    "POST",
                    "/api/plugins/rocketcat_plugin_web/dashboard/api/inspect",
                    headers={
                        "content-type": f"multipart/form-data; boundary={boundary}",
                    },
                    body=multipart_body,
                )
            )
            upload_payload = json.loads(upload_response.body)
            self.assertEqual(1, upload_payload["file_count"])
            self.assertEqual(["demo"], upload_payload["form"]["label"])

            download_response = await webui._handle_plugin_dashboard_api(
                "rocketcat_plugin_web",
                "download",
                _request(
                    "GET",
                    "/api/plugins/rocketcat_plugin_web/dashboard/api/download",
                ),
            )
            self.assertEqual("download.txt", download_response.filename)
            self.assertTrue(Path(download_response.path).is_file())

            sse_response = await webui._handle_plugin_dashboard_sse(
                "rocketcat_plugin_web",
                "events",
                _request(
                    "GET",
                    "/api/plugins/rocketcat_plugin_web/dashboard/sse/events",
                ),
            )
            sse_body = b"".join(
                [
                    chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                    async for chunk in sse_response.body_iterator
                ]
            )
            self.assertIn(b'data: {"tick": 1}', sse_body)
            self.assertEqual(
                0,
                shell_manager.plugin_manager.diagnostic_summary()["dashboard_sse_count"],
            )

            await shell_manager.shutdown()


class BuiltInPluginIsolationTests(unittest.IsolatedAsyncioTestCase):
    def _load_module(self, path: Path, name: str):
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    async def test_builtin_command_suppression_is_runtime_scoped(self):
        root = Path(__file__).resolve().parents[1]
        module = self._load_module(
            root / "data/plugins/rocketcat_plugin_built_in_command/main.py",
            "test_builtin_isolation",
        )
        context = SimpleNamespace(
            plugin_id="rocketcat_plugin_built_in_command",
            plugin_dir=root,
            metadata={},
        )
        plugin = module.Plugin(context, {})
        runtime_a = SimpleNamespace(
            runtime_key="bot-a",
            instance_name="bot-a",
            rocketchat=SimpleNamespace(user_id="self", bot_username="bot"),
            bridge_config=SimpleNamespace(username="bot"),
        )
        runtime_b = SimpleNamespace(
            runtime_key="bot-b",
            instance_name="bot-b",
            rocketchat=SimpleNamespace(user_id="self", bot_username="bot"),
            bridge_config=SimpleNamespace(username="bot"),
        )
        plugin._remember_suppressed_self_echoes(
            [{"_id": "same-message", "rid": "room", "msg": "reply"}],
            runtime_a,
        )
        raw = {
            "_id": "same-message",
            "rid": "room",
            "msg": "reply",
            "u": {"_id": "self", "username": "bot"},
        }
        self.assertTrue(plugin._consume_suppressed_self_echo(raw, runtime_a))
        self.assertFalse(plugin._consume_suppressed_self_echo(raw, runtime_b))

    async def test_iamthinking_reaction_state_is_runtime_scoped(self):
        root = Path(__file__).resolve().parents[1]
        module = self._load_module(
            root / "data/plugins/rocketcat_plugin_adapt_iamthinking/main.py",
            "test_iamthinking_isolation",
        )
        plugin = module.Plugin(
            SimpleNamespace(plugin_id="rocketcat_plugin_adapt_iamthinking"),
            {},
        )
        plugin._resolve_numeric_action_effects(
            66,
            source_message_id="same-message",
            should_react=True,
            runtime_scope="bot-a",
        )
        plugin._resolve_numeric_action_effects(
            66,
            source_message_id="same-message",
            should_react=True,
            runtime_scope="bot-b",
        )
        self.assertEqual(
            {("bot-a", "same-message"), ("bot-b", "same-message")},
            set(plugin._numeric_reaction_states),
        )


if __name__ == "__main__":
    unittest.main()

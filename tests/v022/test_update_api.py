from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from rocketcat_shell.shell.webui import ShellWebUI
from rocketcat_shell.shell.manager import CardOrderConflictError
from rocketcat_shell.update_manifest import UpdatePackageError


class FakeUpdates:
    def __init__(self) -> None:
        self.prepared = False
        self.active = None

    @staticmethod
    def action_for_tag(tag_name: str) -> str:
        if tag_name == "v0.2.2":
            return "reinstall"
        return "update"

    async def status(self, *, refresh: bool = False) -> dict:
        return {
            "current_version": "v0.2.2",
            "current_tag": "v0.2.2",
            "latest_version": "v0.2.3-rc.1",
            "latest_tag": "v0.2.3-rc.1",
            "minimum_compatible_tag": "v0.2.2",
            "update_available": True,
            "checked_at": 1.0,
            "stale": False,
            "error": "",
            "refresh_limited": refresh,
            "active_transaction": None,
        }

    async def releases(self, *, refresh: bool = False) -> dict:
        return {
            "checked_at": 1.0,
            "stale": False,
            "error": "",
            "refresh_limited": refresh,
            "releases": [
                {
                    "tag_name": "v0.2.3-rc.1",
                    "version": "v0.2.3-rc.1",
                    "name": "Preview",
                    "published_at": "2026-08-09T00:00:00Z",
                    "prerelease": True,
                    "notes": "preview notes",
                    "html_url": "https://example.invalid/private-page",
                    "asset": {
                        "name": "RocketCatShell-linux-v0.2.3-rc.1.zip",
                        "size": 123,
                        "digest": f"sha256:{'a' * 64}",
                        "url": "https://example.invalid/private-download",
                    },
                }
            ],
        }

    def transaction(self, transaction_id: str) -> dict | None:
        if transaction_id != "a" * 24:
            return None
        return {
            "transaction_id": transaction_id,
            "status": "completed",
            "stage": "completed",
            "current_version": "v0.2.2",
            "target_version": "v0.2.3-rc.1",
            "action": "update",
        }

    def active_transaction(self) -> dict | None:
        return self.active

    async def prepare_switch(self, *, tag_name: str, **_: object) -> dict:
        if tag_name != "v0.2.3-rc.1":
            raise UpdatePackageError("target release is incompatible")
        if self.prepared:
            raise UpdatePackageError("another version transaction is already active")
        self.prepared = True
        return {
            "transaction_id": "a" * 24,
            "status": "prepared",
            "stage": "prepared",
            "current_version": "v0.2.2",
            "target_version": tag_name,
            "action": "update",
        }


class FakeManager:
    def __init__(self, root: Path) -> None:
        self.layout = SimpleNamespace(project_root=root)
        self.settings = SimpleNamespace(
            terminal_max_sessions=2,
            terminal_idle_timeout_seconds=0,
        )
        self.updates = FakeUpdates()
        self.stop_requests = 0
        self.card_order = {
            "bots": ["bot-a", "bot-b"],
            "plugins": ["plugin-a", "plugin-b"],
        }

    def set_webui_runtime(self, **_: object) -> None:
        return None

    def clear_webui_runtime(self) -> None:
        return None

    def request_stop(self) -> None:
        self.stop_requests += 1

    async def get_card_order_state(self) -> dict[str, list[str]]:
        return {key: list(value) for key, value in self.card_order.items()}

    async def update_card_order(self, payload: dict) -> dict[str, list[str]]:
        for scope, expected in self.card_order.items():
            if scope not in payload:
                continue
            requested = payload[scope]
            if len(requested) != len(expected) or set(requested) != set(expected):
                raise CardOrderConflictError(f"{scope} 实体集合已变化")
            self.card_order[scope] = list(requested)
        return await self.get_card_order_state()


def http_json(
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    cookie: str = "",
) -> tuple[int, dict, dict[str, str]]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
            return response.status, payload, dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        payload = json.loads(exc.read().decode("utf-8"))
        return exc.code, payload, dict(exc.headers.items())


class UpdateApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rocketcat-update-api-")
        self.manager = FakeManager(Path(self.temporary.name))
        self.webui = ShellWebUI(
            self.manager,
            host="127.0.0.1",
            port=0,
            access_password="secret",
        )
        await self.webui.start()
        self.manager.settings.webui_port = self.webui.port
        self.webui.mark_application_ready()
        self.base_url = f"http://127.0.0.1:{self.webui.port}"

    async def asyncTearDown(self) -> None:
        await self.webui.stop()
        self.temporary.cleanup()

    async def _request(self, path: str, **kwargs):
        return await asyncio.to_thread(http_json, self.base_url + path, **kwargs)

    async def _login(self) -> str:
        status, payload, headers = await self._request(
            "/api/login",
            method="POST",
            body={"password": "secret"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        set_cookie = next(
            value for key, value in headers.items() if key.lower() == "set-cookie"
        )
        return set_cookie.split(";", 1)[0]

    async def test_health_is_public_and_minimal_while_updates_require_auth(self) -> None:
        self.webui._application_ready = False
        status, starting, _ = await self._request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(
            starting,
            {"status": "starting", "product": "RocketCatShell", "version": "v0.2.2"},
        )
        self.webui.mark_application_ready()
        status, payload, _ = await self._request("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {"status": "ok", "product": "RocketCatShell", "version": "v0.2.2"},
        )
        status, _, _ = await self._request("/api/updates/status")
        self.assertEqual(status, 401)

    async def test_card_order_requires_auth_and_reports_entity_conflicts(self) -> None:
        status, _, _ = await self._request("/api/settings/card-order")
        self.assertEqual(status, 401)
        cookie = await self._login()
        status, payload, _ = await self._request(
            "/api/settings/card-order",
            cookie=cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(["bot-a", "bot-b"], payload["bots"])

        status, payload, _ = await self._request(
            "/api/settings/card-order",
            method="PUT",
            body={"bots": ["bot-b", "bot-a"]},
            cookie=cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(["bot-b", "bot-a"], payload["bots"])

        status, payload, _ = await self._request(
            "/api/settings/card-order",
            method="PUT",
            body={"plugins": ["plugin-a"]},
            cookie=cookie,
        )
        self.assertEqual(status, 409)
        self.assertIn("实体集合已变化", payload["detail"])

        self.manager.updates.active = {"transaction_id": "a" * 24}
        status, payload, _ = await self._request(
            "/api/settings/card-order",
            method="PUT",
            body={"plugins": ["plugin-b", "plugin-a"]},
            cookie=cookie,
        )
        self.assertEqual(status, 409)
        self.assertIn("版本切换期间", payload["detail"])

    async def test_release_and_transaction_responses_do_not_expose_urls_or_paths(self) -> None:
        cookie = await self._login()
        status, releases, _ = await self._request(
            "/api/updates/releases",
            cookie=cookie,
        )
        self.assertEqual(status, 200)
        release = releases["releases"][0]
        self.assertTrue(release["prerelease"])
        self.assertEqual(release["action"], "update")
        self.assertNotIn("url", release["asset"])
        self.assertNotIn("html_url", release)

        status, transaction, _ = await self._request(
            f"/api/updates/transactions/{'a' * 24}",
            cookie=cookie,
        )
        self.assertEqual(status, 200)
        self.assertNotIn("source_root", transaction)
        self.assertNotIn("candidate_root", transaction)

    async def test_invalid_or_concurrent_switch_returns_conflict(self) -> None:
        cookie = await self._login()
        status, payload, _ = await self._request(
            "/api/updates/switch",
            method="POST",
            body={"tag_name": "v0.2.1"},
            cookie=cookie,
        )
        self.assertEqual(status, 409)
        self.assertIn("incompatible", payload["detail"])

        status, transaction, _ = await self._request(
            "/api/updates/switch",
            method="POST",
            body={"tag_name": "v0.2.3-rc.1"},
            cookie=cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(transaction["transaction_id"], "a" * 24)
        status, _, _ = await self._request(
            "/api/updates/switch",
            method="POST",
            body={"tag_name": "v0.2.3-rc.1"},
            cookie=cookie,
        )
        self.assertEqual(status, 409)

    async def test_finished_helper_readiness_task_does_not_block_future_requests(self) -> None:
        task = asyncio.create_task(
            self.webui._request_update_shutdown("a" * 24)
        )
        self.webui._update_shutdown_task = task
        await task
        self.assertIsNone(self.webui._update_shutdown_task)
        self.assertEqual(self.manager.stop_requests, 0)


if __name__ == "__main__":
    unittest.main()

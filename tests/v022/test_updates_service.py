from __future__ import annotations

import hashlib
import io
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from rocketcat_shell.update_manifest import UpdatePackageError
import rocketcat_shell.updates as updates_module
from rocketcat_shell.updates import UpdateService


def github_release(
    tag: str,
    *,
    prerelease: bool = False,
    draft: bool = False,
    digest: str | None = None,
    url: str | None = None,
) -> dict:
    asset_name = UpdateService.official_asset_name(tag)
    return {
        "tag_name": tag,
        "name": f"Release {tag}",
        "body": f"Notes for {tag}",
        "published_at": "2026-08-09T00:00:00Z",
        "prerelease": prerelease,
        "draft": draft,
        "html_url": f"https://github.com/Creeper3222/RocketCatShell-linux/releases/tag/{tag}",
        "assets": [
            {
                "name": asset_name,
                "browser_download_url": url or UpdateService.official_asset_url(tag),
                "size": 1234,
                "digest": digest or f"sha256:{'a' * 64}",
            }
        ],
    }


class FakeDownloadResponse(io.BytesIO):
    def __init__(self, content: bytes, final_url: str):
        super().__init__(content)
        self._final_url = final_url

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def geturl(self) -> str:
        return self._final_url


class UpdateDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rocketcat-updates-test-")
        self.root = Path(self.temporary.name)
        self.service = UpdateService(self.root, self.root)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_filters_old_drafts_and_untrusted_assets_then_sorts(self) -> None:
        raw = [
            github_release("v0.2.3-rc.1", prerelease=True),
            github_release("v0.2.1"),
            github_release("v0.2.4", draft=True),
            github_release("v0.2.3"),
            github_release("v0.2.5", digest="sha256:not-a-digest"),
            github_release("v0.2.6", url="https://example.com/release.zip"),
            github_release("v0.2.2"),
        ]
        with mock.patch.object(UpdateService, "_request_json", return_value=raw):
            payload = await self.service.releases()
        self.assertEqual(
            [item["tag_name"] for item in payload["releases"]],
            ["v0.2.3", "v0.2.3-rc.1", "v0.2.2"],
        )
        self.assertFalse(payload["releases"][0]["prerelease"])
        self.assertTrue(payload["releases"][1]["prerelease"])

    async def test_ten_minute_cache_and_manual_refresh_rate_limit(self) -> None:
        raw = [github_release("v0.2.2")]
        with mock.patch.object(UpdateService, "_request_json", return_value=raw) as request:
            await self.service.releases()
            await self.service.releases()
            self.assertEqual(request.call_count, 1)
            await self.service.releases(refresh=True)
            limited = await self.service.releases(refresh=True)
        self.assertTrue(limited["refresh_limited"])
        self.assertEqual(request.call_count, 2)

    async def test_api_failure_falls_back_to_release_feed(self) -> None:
        fallback = [
            {
                "tag_name": "v0.2.2",
                "version": "v0.2.2",
                "name": "v0.2.2",
                "published_at": "2026-08-09T00:00:00Z",
                "prerelease": False,
                "notes": "fallback",
                "html_url": "https://github.com/Creeper3222/RocketCatShell-linux/releases/tag/v0.2.2",
                "asset": {
                    "name": "RocketCatShell-linux-v0.2.2.zip",
                    "url": UpdateService.official_asset_url("v0.2.2"),
                    "size": 0,
                    "digest": f"sha256:{'b' * 64}",
                },
            }
        ]
        error = urllib.error.HTTPError("https://api.github.com", 403, "quota", {}, None)
        with (
            mock.patch.object(UpdateService, "_request_json", side_effect=error),
            mock.patch.object(UpdateService, "_fallback_release_feed", return_value=fallback),
        ):
            payload = await self.service.releases()
        self.assertEqual(payload["releases"], fallback)
        self.assertFalse(payload["stale"])

    async def test_offline_refresh_uses_persisted_cache(self) -> None:
        with mock.patch.object(
            UpdateService,
            "_request_json",
            return_value=[github_release("v0.2.2")],
        ):
            await self.service.releases()
        restored = UpdateService(self.root, self.root)
        with (
            mock.patch.object(UpdateService, "_request_json", side_effect=OSError("offline")),
            mock.patch.object(UpdateService, "_fallback_release_feed", side_effect=OSError("offline")),
        ):
            payload = await restored.releases(refresh=True)
        self.assertTrue(payload["stale"])
        self.assertEqual(payload["releases"][0]["tag_name"], "v0.2.2")

    async def test_corrupted_cached_assets_are_discarded(self) -> None:
        self.service.update_root.mkdir(parents=True)
        cached = {
            "checked_at": time.time(),
            "releases": [
                {
                    **self.service._normalize_release(github_release("v0.2.2")),
                    "asset": {
                        "name": "RocketCatShell-linux-v0.2.2.zip",
                        "url": "https://example.com/tampered.zip",
                        "size": 123,
                        "digest": f"sha256:{'a' * 64}",
                    },
                },
                {"tag_name": "not-a-version", "asset": {}},
            ],
        }
        self.service.cache_path.write_text(json.dumps(cached), encoding="utf-8")
        with mock.patch.object(UpdateService, "_request_json") as request:
            payload = await self.service.releases()
        self.assertEqual(payload["releases"], [])
        request.assert_not_called()

    async def test_status_and_actions_include_reinstall_without_old_releases(self) -> None:
        with mock.patch.object(
            UpdateService,
            "_request_json",
            return_value=[
                github_release("v0.2.4"),
                github_release("v0.2.3"),
                github_release("v0.2.2"),
            ],
        ):
            status = await self.service.status()
        self.assertTrue(status["update_available"])
        self.assertEqual(status["minimum_compatible_tag"], "v0.2.2")
        self.assertEqual(self.service.action_for_tag("v0.2.2"), "rollback")
        self.assertEqual(self.service.action_for_tag("v0.2.3"), "reinstall")
        self.assertEqual(self.service.action_for_tag("v0.2.4"), "update")


class UpdateTransactionMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rocketcat-transaction-test-")
        self.root = Path(self.temporary.name)
        self.service = UpdateService(self.root, self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_public_transaction_is_redacted(self) -> None:
        transaction_id = "a" * 24
        transaction_root = self.service.transactions_root / transaction_id
        transaction_root.mkdir(parents=True)
        payload = {
            "transaction_id": transaction_id,
            "status": "running",
            "stage": "replacing",
            "current_version": "v0.2.2",
            "target_version": "v0.2.3",
            "target_tag": "v0.2.3",
            "action": "update",
            "created_at": time.time(),
            "updated_at": time.time(),
            "source_root": r"D:\secret\install",
            "candidate_root": r"D:\secret\candidate",
            "health_urls": ["http://127.0.0.1:5751"],
            "asset_sha256": "c" * 64,
            "error": (
                r"permission denied: D:\secret\install\rocketcat_shell; "
                "health http://127.0.0.1:5751"
            ),
        }
        (transaction_root / "transaction.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        public = self.service.transaction(transaction_id)
        self.assertEqual(public["stage"], "replacing")
        for secret_key in (
            "source_root",
            "candidate_root",
            "health_urls",
            "asset_sha256",
        ):
            self.assertNotIn(secret_key, public)
        self.assertNotIn(r"D:\secret\install", public["error"])
        self.assertNotIn("http://127.0.0.1:5751", public["error"])
        self.assertIn("[redacted]", public["error"])
        self.assertEqual(self.service.active_transaction(), public)

        payload.update(status="recovery_required", stage="rollback_failed")
        (transaction_root / "transaction.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        blocked = self.service.active_transaction()
        self.assertEqual(blocked["status"], "recovery_required")

    def test_health_urls_must_be_loopback_http(self) -> None:
        self.assertEqual(
            self.service._validate_health_urls(["http://127.0.0.1:5751"]),
            ["http://127.0.0.1:5751"],
        )
        for url in (
            "https://127.0.0.1:5751",
            "http://127.0.0.1",
            "http://127.0.0.1:5751/path",
            "http://127.0.0.1:5751?query=yes",
            "http://example.com:5751",
            "http://user:pass@127.0.0.1:5751",
        ):
            with self.subTest(url=url), self.assertRaises(UpdatePackageError):
                self.service._validate_health_urls([url])

    def test_download_enforces_final_host_size_and_digest(self) -> None:
        content = b"verified release"
        expected_digest = "sha256:" + hashlib.sha256(content).hexdigest()
        destination = self.root / "release.zip"
        response = FakeDownloadResponse(
            content,
            "https://release-assets.githubusercontent.com/asset",
        )
        with mock.patch.object(updates_module.urllib.request, "urlopen", return_value=response):
            digest = self.service._download(
                self.service.official_asset_url("v0.2.2"),
                destination,
                expected_digest=expected_digest,
                expected_size=len(content),
            )
        self.assertEqual(digest, hashlib.sha256(content).hexdigest())
        self.assertEqual(destination.read_bytes(), content)

        cases = (
            (
                b"digest mismatch",
                "https://release-assets.githubusercontent.com/asset",
                f"sha256:{'0' * 64}",
                updates_module.MAX_LINUX_RELEASE_BYTES,
            ),
            (
                b"untrusted redirect",
                "https://example.com/asset",
                "sha256:" + hashlib.sha256(b"untrusted redirect").hexdigest(),
                updates_module.MAX_LINUX_RELEASE_BYTES,
            ),
            (
                b"oversize",
                "https://release-assets.githubusercontent.com/asset",
                "sha256:" + hashlib.sha256(b"oversize").hexdigest(),
                4,
            ),
        )
        for index, (body, final_url, digest_value, size_limit) in enumerate(cases):
            with self.subTest(index=index):
                target = self.root / f"invalid-{index}.zip"
                fake = FakeDownloadResponse(body, final_url)
                with (
                    mock.patch.object(
                        updates_module.urllib.request,
                        "urlopen",
                        return_value=fake,
                    ),
                    mock.patch.object(
                        updates_module,
                        "MAX_LINUX_RELEASE_BYTES",
                        size_limit,
                    ),
                    self.assertRaises(UpdatePackageError),
                ):
                    self.service._download(
                        self.service.official_asset_url("v0.2.2"),
                        target,
                        expected_digest=digest_value,
                    )


if __name__ == "__main__":
    unittest.main()

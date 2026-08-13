from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from .logger import logger
from .update_manifest import (
    CONTAINER_RUNTIME_GENERATION,
    MAX_LINUX_RELEASE_BYTES,
    MIN_UPDATE_TAG,
    PRODUCT_NAME,
    TAG_NAME,
    VERSION,
    UpdatePackageError,
    compare_tags,
    inspect_and_extract_zip,
    parse_tag,
)


GITHUB_REPOSITORY = "Creeper3222/RocketCatShell-linux"
GITHUB_RELEASES_URL = (
    f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases?per_page=100"
)
GITHUB_RELEASES_ATOM_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases.atom"
CACHE_SECONDS = 600
MANUAL_REFRESH_SECONDS = 60
DOWNLOAD_CHUNK_BYTES = 1024 * 1024
TRANSACTION_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")
SHA256_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
TERMINAL_TRANSACTION_STATUSES = frozenset(
    {"completed", "failed", "rolled_back"}
)


class UpdateService:
    def __init__(self, source_root: Path, state_root: Path):
        self.source_root = source_root.resolve()
        self.state_root = state_root.resolve()
        self.update_root = self.state_root / "data" / "update"
        self.transactions_root = self.update_root / "transactions"
        self.cache_path = self.update_root / "releases-cache.json"
        self.pending_handoff_path = self.update_root / "pending-handoff.json"
        self.runtime_marker_path = self.update_root / "runtime.json"
        self._cache: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._last_manual_refresh = 0.0

    def _assert_safe_update_state(self) -> None:
        cursor = self.state_root
        for component in ("data", "update", "transactions"):
            cursor /= component
            if cursor.is_symlink():
                raise UpdatePackageError(
                    "the update state path cannot contain symbolic links"
                )
        try:
            self.update_root.resolve().relative_to(self.state_root)
        except ValueError as exc:
            raise UpdatePackageError(
                "the update state path escaped the installation"
            ) from exc

    @staticmethod
    def official_asset_name(tag_name: str) -> str:
        return f"RocketCatShell-linux-{tag_name}.zip"

    @classmethod
    def official_asset_url(cls, tag_name: str) -> str:
        asset_name = cls.official_asset_name(tag_name)
        return (
            f"https://github.com/{GITHUB_REPOSITORY}/releases/download/"
            f"{tag_name}/{asset_name}"
        )

    @staticmethod
    def action_for_tag(tag_name: str) -> str:
        comparison = compare_tags(tag_name, TAG_NAME)
        if comparison > 0:
            return "update"
        if comparison < 0:
            return "rollback"
        return "reinstall"

    @classmethod
    def _normalize_cached_release(cls, raw: object) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        tag = str(raw.get("tag_name") or "")
        try:
            if compare_tags(tag, MIN_UPDATE_TAG) < 0:
                return None
        except UpdatePackageError:
            return None
        asset = raw.get("asset")
        if not isinstance(asset, dict):
            return None
        expected_name = cls.official_asset_name(tag)
        expected_url = cls.official_asset_url(tag)
        digest = str(asset.get("digest") or "").lower()
        try:
            size = int(asset.get("size") or 0)
        except (TypeError, ValueError):
            return None
        if (
            asset.get("name") != expected_name
            or asset.get("url") != expected_url
            or not SHA256_DIGEST_PATTERN.fullmatch(digest)
            or size < 0
        ):
            return None
        return {
            "tag_name": tag,
            "version": tag,
            "name": str(raw.get("name") or tag),
            "published_at": raw.get("published_at"),
            "prerelease": bool(raw.get("prerelease")),
            "notes": str(raw.get("notes") or "")[:20_000],
            "html_url": (
                f"https://github.com/{GITHUB_REPOSITORY}/releases/tag/{tag}"
            ),
            "asset": {
                "name": expected_name,
                "url": expected_url,
                "size": size,
                "digest": digest,
            },
        }

    def _read_cache(self) -> dict[str, Any] | None:
        if self._cache is not None:
            return self._cache
        try:
            self._assert_safe_update_state()
        except UpdatePackageError:
            return None
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("releases"), list):
            return None
        try:
            checked_at = float(payload.get("checked_at") or 0)
        except (TypeError, ValueError):
            return None
        if checked_at <= 0:
            return None
        releases = [
            release
            for raw in payload["releases"]
            if (release := self._normalize_cached_release(raw)) is not None
        ]
        releases.sort(key=lambda item: parse_tag(item["tag_name"]), reverse=True)
        normalized = {
            "checked_at": checked_at,
            "stale": bool(payload.get("stale")),
            "error": str(payload.get("error") or ""),
            "releases": releases,
        }
        self._cache = normalized
        return normalized

    def _write_cache(self, payload: dict[str, Any]) -> None:
        self._assert_safe_update_state()
        self.update_root.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_path.with_name(
            f"{self.cache_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.cache_path)
        self._cache = payload

    @staticmethod
    def _request_json(url: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"{PRODUCT_NAME}/{VERSION}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _request_text(url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": f"{PRODUCT_NAME}/{VERSION}"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read().decode("utf-8", "replace")

    @classmethod
    def _fallback_release_feed(cls) -> list[dict[str, Any]]:
        document = ET.fromstring(cls._request_text(GITHUB_RELEASES_ATOM_URL))
        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        releases: list[dict[str, Any]] = []
        for entry in document.findall("atom:entry", namespace):
            link = entry.find("atom:link[@rel='alternate']", namespace)
            html_url = str((link.attrib if link is not None else {}).get("href") or "")
            tag = html_url.rstrip("/").rsplit("/", 1)[-1]
            try:
                if compare_tags(tag, MIN_UPDATE_TAG) < 0:
                    continue
            except UpdatePackageError:
                continue
            expected_name = cls.official_asset_name(tag)
            assets_url = (
                f"https://github.com/{GITHUB_REPOSITORY}/releases/expanded_assets/{tag}"
            )
            assets_html = cls._request_text(assets_url)
            marker = f"/releases/download/{tag}/{expected_name}"
            marker_index = assets_html.find(marker)
            if marker_index < 0:
                continue
            digest_match = re.search(
                r"sha256:[0-9a-fA-F]{64}",
                assets_html[marker_index : marker_index + 5000],
            )
            if not digest_match:
                continue
            page_html = cls._request_text(html_url)
            content = entry.findtext("atom:content", default="", namespaces=namespace)
            notes = re.sub(
                r"\s+",
                " ",
                re.sub(r"<[^>]+>", " ", html.unescape(content)),
            ).strip()
            releases.append(
                {
                    "tag_name": tag,
                    "version": tag,
                    "name": entry.findtext(
                        "atom:title", default=tag, namespaces=namespace
                    ),
                    "published_at": entry.findtext(
                        "atom:updated", default="", namespaces=namespace
                    ),
                    "prerelease": bool(
                        re.search(r">\s*Pre-release\s*<", page_html, re.IGNORECASE)
                    ),
                    "notes": notes[:20_000],
                    "html_url": html_url,
                    "asset": {
                        "name": expected_name,
                        "url": cls.official_asset_url(tag),
                        "size": 0,
                        "digest": digest_match.group(0).lower(),
                    },
                }
            )
        return releases

    @classmethod
    def _normalize_release(cls, raw: dict[str, Any]) -> dict[str, Any] | None:
        tag = str(raw.get("tag_name") or "")
        try:
            if compare_tags(tag, MIN_UPDATE_TAG) < 0:
                return None
        except UpdatePackageError:
            return None
        if raw.get("draft"):
            return None
        expected_asset = cls.official_asset_name(tag)
        expected_url = cls.official_asset_url(tag)
        asset = next(
            (
                item
                for item in raw.get("assets") or []
                if isinstance(item, dict) and item.get("name") == expected_asset
            ),
            None,
        )
        digest = str((asset or {}).get("digest") or "").lower()
        if (
            not asset
            or asset.get("browser_download_url") != expected_url
            or not SHA256_DIGEST_PATTERN.fullmatch(digest)
        ):
            return None
        return {
            "tag_name": tag,
            "version": tag,
            "name": str(raw.get("name") or tag),
            "published_at": raw.get("published_at") or raw.get("created_at"),
            "prerelease": bool(raw.get("prerelease")),
            "notes": str(raw.get("body") or "")[:20_000],
            "html_url": str(raw.get("html_url") or ""),
            "asset": {
                "name": expected_asset,
                "url": expected_url,
                "size": int(asset.get("size") or 0),
                "digest": digest,
            },
        }

    async def releases(self, *, refresh: bool = False) -> dict[str, Any]:
        async with self._lock:
            now = time.time()
            cached = self._read_cache()
            if refresh and now - self._last_manual_refresh < MANUAL_REFRESH_SECONDS:
                if cached:
                    return {**cached, "refresh_limited": True}
                raise RuntimeError("update check refresh is rate limited")
            if (
                not refresh
                and cached
                and now - float(cached.get("checked_at") or 0) < CACHE_SECONDS
            ):
                return dict(cached)
            if refresh:
                self._last_manual_refresh = now
            try:
                try:
                    raw_releases = await asyncio.to_thread(
                        self._request_json,
                        GITHUB_RELEASES_URL,
                    )
                    normalized = [
                        release
                        for item in (
                            raw_releases if isinstance(raw_releases, list) else []
                        )
                        if isinstance(item, dict)
                        if (release := self._normalize_release(item)) is not None
                    ]
                except (OSError, ValueError, urllib.error.URLError):
                    normalized = await asyncio.to_thread(self._fallback_release_feed)
                normalized.sort(
                    key=lambda item: parse_tag(item["tag_name"]),
                    reverse=True,
                )
                payload = {
                    "checked_at": now,
                    "stale": False,
                    "error": "",
                    "releases": normalized,
                }
                self._write_cache(payload)
                return dict(payload)
            except (OSError, ValueError, urllib.error.URLError) as exc:
                logger.debug("GitHub update check unavailable: %s", exc)
                if cached:
                    return {
                        **cached,
                        "stale": True,
                        "error": "update service unavailable",
                    }
                return {
                    "checked_at": now,
                    "stale": True,
                    "error": "update service unavailable",
                    "releases": [],
                }

    async def status(self, *, refresh: bool = False) -> dict[str, Any]:
        payload = await self.releases(refresh=refresh)
        newer = [
            release
            for release in payload["releases"]
            if compare_tags(release["tag_name"], TAG_NAME) > 0
        ]
        latest = payload["releases"][0] if payload["releases"] else None
        image_version = str(os.environ.get("ROCKETCAT_IMAGE_VERSION") or VERSION).strip()
        return {
            "current_version": VERSION,
            "current_tag": TAG_NAME,
            "latest_version": latest["version"] if latest else VERSION,
            "latest_tag": latest["tag_name"] if latest else TAG_NAME,
            "minimum_compatible_tag": MIN_UPDATE_TAG,
            "update_available": bool(newer),
            "checked_at": payload["checked_at"],
            "stale": payload.get("stale", False),
            "error": payload.get("error", ""),
            "refresh_limited": payload.get("refresh_limited", False),
            "active_transaction": self.active_transaction(),
            "platform": "linux",
            "update_mode": "container_writable_layer",
            "container_runtime_generation": CONTAINER_RUNTIME_GENERATION,
            "image_version": image_version,
            "recreate_resets_runtime": True,
        }

    def transaction(self, transaction_id: str) -> dict[str, Any] | None:
        if not TRANSACTION_ID_PATTERN.fullmatch(str(transaction_id or "")):
            return None
        try:
            self._assert_safe_update_state()
        except UpdatePackageError:
            return None
        path = self.transactions_root / transaction_id / "transaction.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return self._public_transaction(payload)

    def active_transaction(self) -> dict[str, Any] | None:
        try:
            self._assert_safe_update_state()
        except UpdatePackageError:
            return None
        if not self.transactions_root.is_dir():
            return None
        candidates: list[dict[str, Any]] = []
        for path in self.transactions_root.glob("*/transaction.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("status") not in TERMINAL_TRANSACTION_STATUSES:
                candidates.append(payload)
        if not candidates:
            return None
        latest = max(candidates, key=lambda item: float(item.get("created_at") or 0))
        return self._public_transaction(latest)

    def fail_prepared_transaction(
        self,
        transaction_id: str,
        *,
        error: str,
    ) -> dict[str, Any] | None:
        if not TRANSACTION_ID_PATTERN.fullmatch(str(transaction_id or "")):
            return None
        try:
            self._assert_safe_update_state()
        except UpdatePackageError:
            return None
        path = self.transactions_root / transaction_id / "transaction.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("status") != "prepared" or payload.get("stage") != "prepared":
            return self._public_transaction(payload)
        now = time.time()
        payload.update(
            status="failed",
            stage="helper_start_timeout",
            error=str(error),
            completed_at=now,
            updated_at=now,
        )
        self._write_transaction(path, payload)
        return self._public_transaction(payload)

    @staticmethod
    def _public_transaction(payload: dict[str, Any]) -> dict[str, Any]:
        public = {
            key: payload.get(key)
            for key in (
                "transaction_id",
                "status",
                "stage",
                "current_version",
                "target_version",
                "target_tag",
                "action",
                "created_at",
                "updated_at",
                "completed_at",
                "error",
                "rollback_error",
                "forced_shutdown",
            )
            if key in payload
        }
        for key in ("error", "rollback_error"):
            if key in public:
                public[key] = UpdateService._sanitize_public_error(payload, public[key])
        return public

    @staticmethod
    def _sanitize_public_error(payload: dict[str, Any], value: object) -> str:
        message = str(value or "")[:2_000]
        secrets_to_remove = [
            str(payload.get(key) or "")
            for key in ("source_root", "state_root", "candidate_root")
        ]
        secrets_to_remove.extend(
            str(item) for item in payload.get("health_urls") or []
        )
        for secret in sorted(
            {item for item in secrets_to_remove if item},
            key=len,
            reverse=True,
        ):
            for variant in {secret, secret.replace("\\", "/")}:
                message = re.sub(
                    re.escape(variant),
                    "[redacted]",
                    message,
                    flags=re.IGNORECASE,
                )
        return message

    @staticmethod
    def _write_transaction(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    @staticmethod
    def _validate_health_urls(urls: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in urls:
            try:
                parsed = urllib.parse.urlsplit(str(value or ""))
                port = parsed.port
            except ValueError as exc:
                raise UpdatePackageError("update health URL is invalid") from exc
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or port is None
                or not 1 <= port <= 65535
            ):
                raise UpdatePackageError("update health URL must be loopback HTTP")
            normalized.append(str(value).rstrip("/"))
        if not normalized:
            raise UpdatePackageError("update health URL is unavailable")
        return list(dict.fromkeys(normalized))

    @staticmethod
    def _download(
        url: str,
        destination: Path,
        *,
        expected_digest: str,
        expected_size: int = 0,
    ) -> str:
        if not SHA256_DIGEST_PATTERN.fullmatch(expected_digest):
            raise UpdatePackageError("GitHub release asset digest is missing")
        request = urllib.request.Request(
            url,
            headers={"User-Agent": f"{PRODUCT_NAME}/{VERSION}"},
        )
        digest = hashlib.sha256()
        total = 0
        with urllib.request.urlopen(request, timeout=60) as response, destination.open(
            "xb"
        ) as output:
            final_url = urllib.parse.urlsplit(response.geturl())
            if (
                final_url.scheme.lower() != "https"
                or (final_url.hostname or "").lower() not in ALLOWED_DOWNLOAD_HOSTS
            ):
                raise UpdatePackageError("release download left the official TLS hosts")
            while chunk := response.read(DOWNLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_LINUX_RELEASE_BYTES:
                    raise UpdatePackageError("release asset exceeds the size limit")
                digest.update(chunk)
                output.write(chunk)
        if expected_size > 0 and total != expected_size:
            raise UpdatePackageError("GitHub release asset size mismatch")
        value = digest.hexdigest()
        if value != expected_digest.split(":", 1)[1]:
            raise UpdatePackageError("GitHub release asset digest mismatch")
        return value

    async def prepare_switch(
        self,
        *,
        tag_name: str,
        service_pid: int,
        service_create_time: float,
        health_urls: list[str],
    ) -> dict[str, Any]:
        if os.name != "posix" or sys.platform != "linux":
            raise UpdatePackageError("the Linux updater requires a Linux container")
        runtime_generation = str(
            os.environ.get("ROCKETCAT_CONTAINER_RUNTIME_GENERATION") or ""
        ).strip()
        if runtime_generation != str(CONTAINER_RUNTIME_GENERATION):
            raise UpdatePackageError(
                "container writable-layer updates are unavailable; update the image"
            )
        if os.getpid() != 1:
            raise UpdatePackageError(
                "container writable-layer updates require RocketCatShell to run as PID 1"
            )
        if compare_tags(tag_name, MIN_UPDATE_TAG) < 0:
            raise UpdatePackageError("versions below v0.2.2 are not update-compatible")
        self._assert_safe_update_state()
        health_urls = self._validate_health_urls(health_urls)
        releases_payload = await self.releases(refresh=False)
        async with self._lock:
            if self.active_transaction():
                raise UpdatePackageError("another version transaction is already active")
            release = next(
                (
                    item
                    for item in releases_payload["releases"]
                    if item["tag_name"] == tag_name
                ),
                None,
            )
            if release is None:
                raise UpdatePackageError(
                    "target release is unavailable or lacks the Linux asset"
                )
            expected_url = self.official_asset_url(tag_name)
            if release["asset"].get("url") != expected_url:
                raise UpdatePackageError(
                    "target release asset URL is not the official repository URL"
                )
            transaction_id = secrets.token_hex(12)
            self.transactions_root.mkdir(parents=True, exist_ok=True)
            transaction_root = self.transactions_root / transaction_id
            transaction_root.mkdir(parents=False, exist_ok=False)
            archive = transaction_root / release["asset"]["name"]
            extract_root = transaction_root / "candidate"
            try:
                asset_hash = await asyncio.to_thread(
                    self._download,
                    release["asset"]["url"],
                    archive,
                    expected_digest=release["asset"].get("digest") or "",
                    expected_size=int(release["asset"].get("size") or 0),
                )
                candidate_root, manifest = await asyncio.to_thread(
                    inspect_and_extract_zip,
                    archive,
                    extract_root,
                    expected_tag=tag_name,
                )
                now = time.time()
                payload = {
                    "transaction_id": transaction_id,
                    "status": "prepared",
                    "stage": "prepared",
                    "action": self.action_for_tag(tag_name),
                    "current_version": VERSION,
                    "current_tag": TAG_NAME,
                    "target_version": manifest["version"],
                    "target_tag": tag_name,
                    "asset_name": release["asset"]["name"],
                    "asset_sha256": asset_hash,
                    "source_root": str(self.source_root),
                    "state_root": str(self.state_root),
                    "candidate_root": str(candidate_root),
                    "candidate_files": manifest["files"],
                    "container_runtime_generation": manifest[
                        "container_runtime_generation"
                    ],
                    "image_version": str(
                        os.environ.get("ROCKETCAT_IMAGE_VERSION") or VERSION
                    ),
                    "old_python": sys.executable,
                    "service_pid": int(service_pid),
                    "service_create_time": float(service_create_time),
                    "health_urls": health_urls,
                    "created_at": now,
                    "updated_at": now,
                }
                transaction_file = transaction_root / "transaction.json"
                self._write_transaction(transaction_file, payload)
                helper = transaction_root / "update_helper.py"
                shutil.copy2(self.source_root / "tools" / "update_helper.py", helper)
                payload.update(
                    status="prepared",
                    stage="waiting_for_shutdown",
                    helper_path=str(helper),
                    transaction_file=str(transaction_file),
                    updated_at=time.time(),
                )
                self._write_transaction(transaction_file, payload)
                self._write_pending_handoff(
                    {
                        "transaction_id": transaction_id,
                        "transaction_file": str(transaction_file),
                        "helper_path": str(helper),
                    }
                )
                return self._public_transaction(payload)
            except Exception:
                resolved = transaction_root.resolve()
                if resolved.parent == self.transactions_root.resolve():
                    shutil.rmtree(resolved, ignore_errors=True)
                raise

    def _write_pending_handoff(self, payload: dict[str, Any]) -> None:
        self._assert_safe_update_state()
        self.update_root.mkdir(parents=True, exist_ok=True)
        temporary = self.pending_handoff_path.with_name(
            f"{self.pending_handoff_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.pending_handoff_path)


def exec_pending_container_update(source_root: Path) -> bool:
    """Replace PID 1 with the frozen helper after graceful application shutdown."""

    source_root = source_root.resolve()
    update_root = source_root / "data" / "update"
    pending_path = update_root / "pending-handoff.json"
    if not pending_path.is_file():
        return False
    if os.name != "posix" or sys.platform != "linux" or os.getpid() != 1:
        raise RuntimeError("pending container update cannot hand off outside Linux PID 1")
    if pending_path.is_symlink():
        raise RuntimeError("pending update handoff cannot be a symbolic link")
    payload = json.loads(pending_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("pending update handoff is invalid")
    transaction_id = str(payload.get("transaction_id") or "")
    if not TRANSACTION_ID_PATTERN.fullmatch(transaction_id):
        raise RuntimeError("pending update transaction id is invalid")
    transaction_root = (update_root / "transactions" / transaction_id).resolve()
    try:
        transaction_root.relative_to(update_root.resolve())
    except ValueError as exc:
        raise RuntimeError("pending update transaction escaped the update directory") from exc
    transaction_file = Path(str(payload.get("transaction_file") or "")).resolve()
    helper = Path(str(payload.get("helper_path") or "")).resolve()
    if transaction_file != transaction_root / "transaction.json":
        raise RuntimeError("pending update transaction file is invalid")
    if helper != transaction_root / "update_helper.py":
        raise RuntimeError("pending update helper is invalid")
    if not transaction_file.is_file() or not helper.is_file():
        raise RuntimeError("pending update handoff files are missing")
    transaction = json.loads(transaction_file.read_text(encoding="utf-8"))
    if (
        not isinstance(transaction, dict)
        or transaction.get("transaction_id") != transaction_id
        or transaction.get("status") != "prepared"
        or transaction.get("stage") != "waiting_for_shutdown"
    ):
        raise RuntimeError("pending update transaction is not ready for handoff")
    environment = dict(os.environ)
    environment["ROCKETCATSHELL_UPDATE_TRANSACTION"] = transaction_id
    os.chdir(source_root)
    os.execve(
        sys.executable,
        [sys.executable, str(helper), "apply", str(transaction_file)],
        environment,
    )
    return True

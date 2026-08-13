from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rocketcat_shell import update_manifest
from rocketcat_shell.updates import UpdateService


def test_linux_release_contract_separates_hot_and_image_paths() -> None:
    assert update_manifest.PLATFORM_NAME == "linux"
    assert update_manifest.PACKAGE_ROOT_DIRECTORY == "RocketCatShell-linux"
    assert update_manifest.CONTAINER_RUNTIME_GENERATION == 1
    assert "rocketcat_shell" in update_manifest.MANAGED_DIRECTORIES
    assert "docker/entrypoint.sh" in update_manifest.IMAGE_DEPLOYMENT_FILES
    assert "Dockerfile" in update_manifest.IMAGE_DEPLOYMENT_FILES
    assert "Dockerfile" not in update_manifest.MANAGED_FILES
    assert "docker/entrypoint.sh" not in update_manifest.MANAGED_FILES


def test_linux_release_discovery_uses_dedicated_repository() -> None:
    assert UpdateService.official_asset_name("v0.2.2") == "RocketCatShell-linux-v0.2.2.zip"
    assert UpdateService.official_asset_url("v0.2.2") == (
        "https://github.com/Creeper3222/RocketCatShell-linux/releases/download/"
        "v0.2.2/RocketCatShell-linux-v0.2.2.zip"
    )


def test_status_exposes_container_semantics(tmp_path: Path) -> None:
    service = UpdateService(tmp_path, tmp_path)
    service._cache = {
        "checked_at": 1.0,
        "stale": False,
        "error": "",
        "releases": [],
    }
    import asyncio

    status = asyncio.run(service.status(refresh=False))
    assert status["platform"] == "linux"
    assert status["update_mode"] == "container_writable_layer"
    assert status["container_runtime_generation"] == 1
    assert status["recreate_resets_runtime"] is True


def test_entrypoint_recovers_before_plugin_seed() -> None:
    source = (PROJECT_ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    assert source.index('recover "$APP_DIR"') < source.index("refresh_from_image")
    assert 'run "$APP_DIR"' in source
    assert "docker.sock" not in source


@pytest.mark.skipif(os.name != "posix", reason="executable mode is a Linux release contract")
def test_release_scripts_keep_executable_mode(tmp_path: Path) -> None:
    output = tmp_path / "RocketCatShell-linux-v0.2.2.zip"
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "tools" / "build_linux_release.py"),
            "--source",
            str(PROJECT_ROOT),
            "--output",
            str(output),
            "--tag",
            "v0.2.2",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    extract = tmp_path / "extract"
    root, manifest = update_manifest.inspect_and_extract_zip(
        output,
        extract,
        expected_tag="v0.2.2",
    )
    assert manifest["platform"] == "linux"
    assert os.access(root / "launcher.sh", os.X_OK)
    assert os.access(root / "docker" / "entrypoint.sh", os.X_OK)

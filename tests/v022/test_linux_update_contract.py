from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

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


def test_linux_pid_one_installs_graceful_sigterm_handler() -> None:
    source = (PROJECT_ROOT / "rocketcat_shell" / "shell" / "app.py").read_text(
        encoding="utf-8"
    )
    assert "loop.add_signal_handler(signal_number, manager.request_stop)" in source
    assert 'getattr(signal, "SIGTERM", None)' in source
    assert source.index("await webui.start()") < source.index(
        "installed_signals = _install_termination_signal_handlers(manager)",
        source.index("await webui.start()"),
    )

    helper_source = (PROJECT_ROOT / "tools" / "update_helper.py").read_text(
        encoding="utf-8"
    )
    assert "GRACEFUL_TERMINATION_TIMEOUT_SECONDS = 35.0" in helper_source
    assert "time.sleep(GRACEFUL_TERMINATION_TIMEOUT_SECONDS)" in helper_source


def test_webui_update_handoff_reuses_only_the_configured_port() -> None:
    from rocketcat_shell.shell.webui import ShellWebUI

    webui = object.__new__(ShellWebUI)
    probe = webui._bind_socket("127.0.0.1", 0)
    try:
        assert probe.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 1
        occupied_port = int(probe.getsockname()[1])
    finally:
        probe.close()

    attempted_ports: list[int] = []

    def fail_bind(_host: str, port: int) -> socket.socket:
        attempted_ports.append(port)
        raise OSError("configured port is temporarily busy")

    async def assert_no_fallback() -> None:
        with (
            patch.object(webui, "_bind_socket", side_effect=fail_bind),
            patch(
                "rocketcat_shell.shell.webui._UPDATE_PORT_RETRY_SECONDS",
                0.0,
            ),
        ):
            with pytest.raises(RuntimeError, match=str(occupied_port)):
                await webui._acquire_update_start_socket(
                    "127.0.0.1",
                    occupied_port,
                )

    import asyncio

    asyncio.run(assert_no_fallback())
    assert attempted_ports == [occupied_port]

    async def assert_exact_rebind() -> None:
        rebound, selected_port, fallback_reason = (
            await webui._acquire_update_start_socket(
                "127.0.0.1",
                occupied_port,
            )
        )
        try:
            assert selected_port == occupied_port
            assert fallback_reason is None
        finally:
            rebound.close()

    asyncio.run(assert_exact_rebind())


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

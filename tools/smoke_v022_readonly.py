from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import aiohttp

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rocketcat_shell import __version__
from rocketcat_shell.bridge.runtime import BridgeRuntime
from rocketcat_shell.layout import ProjectLayout
from rocketcat_shell.logger import configure_logging, shutdown_logging
from rocketcat_shell.settings import load_or_create_shell_settings
from rocketcat_shell.shell.instance_lock import ShellInstanceLock
from rocketcat_shell.shell.manager import ShellManager
from rocketcat_shell.shell.webui import ShellWebUI
from rocketcat_shell.update_manifest import PRODUCT_NAME


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _health(base_url: str) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=3.0)
    connector = aiohttp.TCPConnector(limit=2, ttl_dns_cache=0)
    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        trust_env=False,
    ) as session:
        async with session.get(f"{base_url}/api/health") as response:
            response.raise_for_status()
            payload = await response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("health response is not an object")
    return payload


def _render_markdown(result: dict[str, Any]) -> str:
    checks = result["checks"]
    lines = [
        "# RocketCatShell v0.2.2 真实链路只读冒烟报告",
        "",
        f"- 结果：{'通过' if result['passed'] else '未通过'}",
        f"- 持续时间：{result['duration_seconds']} 秒",
        f"- 健康采样：{result['health_samples']} 次",
        f"- 启用 Bot：{result['enabled_bot_count']} 个",
        f"- 状态分布：`{json.dumps(result['status_counts'], ensure_ascii=False)}`",
        "- 本工具不会调用任何发送消息 API。",
        "",
        "## 检查",
        "",
    ]
    lines.extend(f"- [{'x' if passed else ' '}] {name}" for name, passed in checks.items())
    return "\n".join(lines) + "\n"


async def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    layout = ProjectLayout.discover()
    layout.ensure_directories()
    settings = load_or_create_shell_settings(layout.shell_settings_path)
    configure_logging(
        layout.log_file_path,
        level_name=settings.log_level,
        max_bytes=settings.log_file_max_bytes,
        backup_count=settings.log_file_backup_count,
    )
    instance_lock = ShellInstanceLock(layout.logs_dir / "rocketcat_shell.instance.lock")
    manager: ShellManager | None = None
    webui: ShellWebUI | None = None
    started_at = 0.0
    health_samples = 0
    health_failures: list[str] = []
    status_counts: Counter[str] = Counter()
    writer_failures = 0
    onebot_wait_observed = False
    rocketchat_authenticated_observed = False
    subscription_activity_observed = False
    websocket_activity_observed = False
    heartbeat_observed = False
    authenticated_runtime_ids: set[str] = set()
    ready_runtime_ids: set[str] = set()
    heartbeat_runtime_ids: set[str] = set()
    onebot_wait_runtime_ids: set[str] = set()
    max_loop_lag = 0.0
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    config_hashes_before = {
        str(path.relative_to(layout.project_root)): _sha256(path)
        for path in (layout.shell_settings_path, layout.bot_registry_path)
        if path.is_file()
    }

    try:
        instance_lock.acquire({"project_root": str(layout.project_root), "purpose": "readonly-smoke"})
        manager = ShellManager(layout)
        await manager.initialize(start_runtimes=False)
        smoke_bots = [bot for bot in manager.bots if bot.server_url and bot.username and bot.password]
        if smoke_bots:
            temporary_directory = tempfile.TemporaryDirectory(prefix="rocketcat-v022-readonly-smoke-")
            temporary_root = Path(temporary_directory.name)
            for bot in smoke_bots:
                payload = bot.to_mapping()
                payload["enabled"] = True
                # Connection failures must never write the temporary enabled
                # state back to the user's disabled Bot configuration.
                runtime = BridgeRuntime(
                    plugin_root=layout.package_root,
                    raw_config=payload,
                    data_dir=temporary_root / "bots" / bot.bot_id,
                    media_temp_dir=temporary_root / "temp",
                    instance_name=bot.name or bot.bot_id,
                    message_index_max_entries=manager.settings.message_index_max_entries,
                    media_publication_service=manager.media_publication,
                    disable_callback=lambda: None,
                    plugin_manager=manager.plugin_manager,
                )
                await runtime.start()
                if runtime.started:
                    manager.runtimes[bot.bot_id] = runtime
        webui = ShellWebUI(
            manager,
            host="127.0.0.1",
            port=settings.webui_port,
            access_password=settings.webui_access_password,
        )
        await webui.start()
        webui.mark_application_ready()
        base_url = f"http://127.0.0.1:{webui.port}"
        started_at = time.monotonic()
        deadline = started_at + max(1.0, float(args.duration_seconds))
        while time.monotonic() < deadline:
            try:
                payload = await _health(base_url)
                if payload.get("product") != PRODUCT_NAME or payload.get("version") != __version__:
                    health_failures.append("health product/version mismatch")
                health_samples += 1
            except Exception as exc:
                health_failures.append(type(exc).__name__)

            diagnostics = await manager.get_diagnostics_state()
            host_performance = diagnostics.get("performance") or {}
            loop_snapshot = host_performance.get("event_loop_lag_ms") or {}
            max_loop_lag = max(max_loop_lag, float(loop_snapshot.get("max") or 0.0))
            for runtime_id, runtime in manager.runtimes.items():
                item = runtime.build_diagnostic_summary()
                status_counts[str(item.get("status_code") or "unknown")] += 1
                performance = item.get("performance") or {}
                persistence = performance.get("persistence") or {}
                if not persistence.get("writer_alive", True) or persistence.get("last_error"):
                    writer_failures += 1
                if not bool(item.get("onebot_connected")):
                    onebot_wait_observed = True
                    onebot_wait_runtime_ids.add(runtime_id)
                client = runtime.rocketchat
                if client is not None:
                    client_snapshot = client.build_diagnostic_snapshot()
                    rocketchat_authenticated_observed = (
                        rocketchat_authenticated_observed
                        or bool(client_snapshot.get("authenticated"))
                    )
                    if client_snapshot.get("authenticated"):
                        authenticated_runtime_ids.add(runtime_id)
                    subscription_activity_observed = (
                        subscription_activity_observed
                        or bool(client_snapshot.get("subscriptions_ready"))
                    )
                    if client_snapshot.get("subscriptions_ready"):
                        ready_runtime_ids.add(runtime_id)
                    websocket_activity_observed = (
                        websocket_activity_observed
                        or bool(client_snapshot.get("websocket_connected"))
                    )
                    heartbeat_observed = (
                        heartbeat_observed
                        or int(client_snapshot.get("ddp_ping_count") or 0) > 0
                        or float(client_snapshot.get("websocket_connected_seconds") or 0.0)
                        >= 35.0
                    )
                    if (
                        int(client_snapshot.get("ddp_ping_count") or 0) > 0
                        or float(client_snapshot.get("websocket_connected_seconds") or 0.0)
                        >= 35.0
                    ):
                        heartbeat_runtime_ids.add(runtime_id)
            await asyncio.sleep(min(5.0, max(0.0, deadline - time.monotonic())))

        elapsed = time.monotonic() - started_at
        enabled_count = len(manager.runtimes)
        config_hashes_after = {
            str(path.relative_to(layout.project_root)): _sha256(path)
            for path in (layout.shell_settings_path, layout.bot_registry_path)
            if path.is_file()
        }
        checks = {
            "duration": elapsed >= (295.0 if args.strict_duration else 1.0),
            "health_available": health_samples > 0 and not health_failures,
            "writer_healthy": writer_failures == 0,
            "event_loop_max": max_loop_lag <= 100.0,
            "runtime_started": enabled_count > 0,
            "rocketchat_authenticated": rocketchat_authenticated_observed,
            "subscription_activity": subscription_activity_observed,
            "websocket_activity": websocket_activity_observed,
            "heartbeat": heartbeat_observed,
            "onebot_wait_path": onebot_wait_observed,
            "read_only": config_hashes_before == config_hashes_after,
        }
        return {
            "schema_version": 1,
            "product": PRODUCT_NAME,
            "version": __version__,
            "duration_seconds": round(elapsed, 3),
            "health_samples": health_samples,
            "health_failures": health_failures,
            "enabled_bot_count": enabled_count,
            "status_counts": dict(status_counts),
            "onebot_wait_observed": onebot_wait_observed,
            "rocketchat_authenticated_observed": rocketchat_authenticated_observed,
            "subscription_activity_observed": subscription_activity_observed,
            "websocket_activity_observed": websocket_activity_observed,
            "heartbeat_observed": heartbeat_observed,
            "authenticated_runtime_count": len(authenticated_runtime_ids),
            "ready_runtime_count": len(ready_runtime_ids),
            "heartbeat_runtime_count": len(heartbeat_runtime_ids),
            "onebot_wait_runtime_count": len(onebot_wait_runtime_ids),
            "config_hashes_unchanged": config_hashes_before == config_hashes_after,
            "writer_failures": writer_failures,
            "event_loop_max_ms": round(max_loop_lag, 3),
            "checks": checks,
            "passed": all(checks.values()),
        }
    finally:
        if webui is not None:
            await webui.stop()
        if manager is not None:
            await manager.shutdown()
        if temporary_directory is not None:
            temporary_directory.cleanup()
        instance_lock.release()
        await asyncio.to_thread(shutdown_logging, timeout=10.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RocketCatShell read-only real-link smoke")
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--non-strict-duration", dest="strict_duration", action="store_false")
    parser.set_defaults(strict_duration=True)
    return parser


async def async_main() -> int:
    args = build_parser().parse_args()
    result = await run_smoke(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_output = args.markdown_output or args.output.with_suffix(".md")
    markdown_output.write_text(_render_markdown(result), encoding="utf-8")
    return 0 if result["passed"] else 1


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())

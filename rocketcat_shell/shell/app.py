from __future__ import annotations

import argparse
import asyncio
import json
import webbrowser
from typing import Any

from ..layout import ProjectLayout
from ..logger import configure_logging, logger
from ..models import DEFAULT_WEBUI_ACCESS_PASSWORD
from ..settings import load_or_create_shell_settings
from .instance_lock import ShellInstanceLock, SingleInstanceError
from .manager import ShellManager
from .webui import ShellWebUI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RocketCat standalone shell")
    parser.add_argument("--once", action="store_true", help="bootstrap and exit without starting the WebUI server")
    parser.add_argument("--no-browser", action="store_true", help="do not auto-open the local WebUI")
    parser.add_argument("--print-status", action="store_true", help="print the shell status payload to stdout")
    parser.add_argument("--verbose", action="store_true", help="force DEBUG log level for this run")
    return parser


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run_async(args))
    except KeyboardInterrupt:
        return 130


def _format_duplicate_instance_message(
    details: dict[str, Any],
    project_root: str,
) -> str:
    pid = str(details.get("pid") or "").strip()
    started_at = str(details.get("started_at") or "").strip()
    suffix_parts = [part for part in [f"pid={pid}" if pid else "", f"started_at={started_at}" if started_at else ""] if part]
    suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
    return (
        "RocketCat Shell 已在当前项目中运行，请勿重复启动"
        f"{suffix}。project_root={project_root}"
    )


async def _run_async(args: argparse.Namespace) -> int:
    layout = ProjectLayout.discover()
    layout.ensure_directories()

    bootstrap_settings = load_or_create_shell_settings(layout.shell_settings_path)
    log_level = "DEBUG" if args.verbose else bootstrap_settings.log_level
    configure_logging(layout.log_file_path, level_name=log_level)

    instance_lock = ShellInstanceLock(layout.logs_dir / "rocketcat_shell.instance.lock")
    try:
        instance_lock.acquire({"project_root": str(layout.project_root)})
    except SingleInstanceError as exc:
        message = _format_duplicate_instance_message(exc.details, str(layout.project_root))
        logger.error("[RocketCatShell] %s", message)
        print(message)
        return 1

    try:
        manager = ShellManager(layout)
        await manager.initialize(start_runtimes=False)

        if args.print_status:
            print(json.dumps(manager.build_status_payload(), ensure_ascii=False, indent=2))

        if args.once:
            await manager.start_enabled_runtimes("shell once bootstrap")
            await manager.shutdown()
            return 0

        settings = manager.settings
        if settings is None:
            raise RuntimeError("shell settings are unavailable after initialization")

        if settings.webui_access_password == DEFAULT_WEBUI_ACCESS_PASSWORD:
            logger.warning(
                "[RocketCatShell] 初次登录的webui的默认密码为123456，请到webui“设置”项自行修改"
            )

        webui = ShellWebUI(
            manager,
            host=settings.webui_host,
            port=settings.webui_port,
            access_password=settings.webui_access_password,
        )
        await webui.start()
        try:
            await manager.start_enabled_runtimes("webui ready")
            logger.info("[RocketCatShell] WebUI started at %s", webui.url)

            if settings.auto_open_browser and not args.no_browser:
                try:
                    webbrowser.open(webui.url)
                except Exception:
                    logger.warning("[RocketCatShell] Failed to open the browser automatically.")

            await manager.run_forever()
        finally:
            await webui.stop()
            await manager.shutdown()
    finally:
        instance_lock.release()

    return 0

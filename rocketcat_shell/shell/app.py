from __future__ import annotations

import argparse
import asyncio
import json
import webbrowser

from ..layout import ProjectLayout
from ..logger import configure_logging, logger
from ..models import DEFAULT_WEBUI_ACCESS_PASSWORD
from ..settings import load_or_create_shell_settings
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


async def _run_async(args: argparse.Namespace) -> int:
    layout = ProjectLayout.discover()
    layout.ensure_directories()

    bootstrap_settings = load_or_create_shell_settings(layout.shell_settings_path)
    log_level = "DEBUG" if args.verbose else bootstrap_settings.log_level
    configure_logging(layout.log_file_path, level_name=log_level)

    manager = ShellManager(layout)
    await manager.initialize()

    if args.print_status:
        print(json.dumps(manager.build_status_payload(), ensure_ascii=False, indent=2))

    if args.once:
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
    logger.info("[RocketCatShell] WebUI started at %s", webui.url)

    if settings.auto_open_browser and not args.no_browser:
        try:
            webbrowser.open(webui.url)
        except Exception:
            logger.warning("[RocketCatShell] Failed to open the browser automatically.")

    try:
        await manager.run_forever()
    finally:
        await webui.stop()
        await manager.shutdown()

    return 0
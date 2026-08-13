from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rocketcat_shell.layout import ProjectLayout
from rocketcat_shell.models import BotRecord, ShellSettings
from rocketcat_shell.registry import BotRegistry
from rocketcat_shell.settings import write_json
from rocketcat_shell.shell.manager import ShellManager
from rocketcat_shell.shell.webui import ShellWebUI

def build_layout(project_root: Path) -> ProjectLayout:
    config_dir = project_root / "config"
    data_dir = project_root / "data"
    logs_dir = project_root / "logs"
    return ProjectLayout(
        project_root=project_root,
        package_root=ROOT / "rocketcat_shell",
        config_dir=config_dir,
        plugins_config_dir=config_dir / "plugins_config",
        data_dir=data_dir,
        temp_dir=data_dir / "temp",
        bots_dir=data_dir / "bots",
        plugins_dir=data_dir / "plugins",
        plugin_data_dir=data_dir / "plugin_data",
        logs_dir=logs_dir,
        shell_settings_path=config_dir / "shell.json",
        bot_registry_path=config_dir / "bots.json",
        log_file_path=logs_dir / "rocketcat.log",
    )


def build_bots(settings: ShellSettings) -> list[BotRecord]:
    bots: list[BotRecord] = []
    for index in range(1, 7):
        bots.append(
            BotRecord.from_mapping(
                {
                    "id": f"validation-bot-{index}",
                    "name": f"验证 Bot {index}",
                    "enabled": index not in {2, 5},
                    "server_url": "http://127.0.0.1:3000",
                    "username": f"validation{index}",
                    "password": "validation-only",
                    "onebot_ws_url": f"ws://127.0.0.1:{6200 + index}/ws/",
                },
                defaults=settings,
            )
        )
    return bots


async def serve(project_root: Path, port: int) -> None:
    layout = build_layout(project_root)
    layout.ensure_directories()
    for plugin_id in (
        "rocketcat_plugin_adapt_iamthinking",
        "rocketcat_plugin_built_in_command",
    ):
        target = layout.plugins_dir / plugin_id
        if not target.exists():
            shutil.copytree(
                ROOT / "data" / "plugins" / plugin_id,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
    dashboard_fixture = layout.plugins_dir / "rocketcat_plugin_dashboard_fixture"
    if not dashboard_fixture.exists():
        shutil.copytree(
            ROOT / "tests" / "v022" / "fixtures" / "dashboard_plugin",
            dashboard_fixture,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    settings = ShellSettings.from_mapping(
        {
            "webui_host": "127.0.0.1",
            "webui_port": port,
            "webui_access_password": "123456",
            "auto_open_browser": False,
        }
    )
    write_json(layout.shell_settings_path, settings.to_mapping())
    BotRegistry(layout.bot_registry_path).save(build_bots(settings))

    manager = ShellManager(layout)
    await manager.initialize(start_runtimes=False)
    webui = ShellWebUI(
        manager,
        host="127.0.0.1",
        port=port,
        access_password=settings.webui_access_password,
    )
    await webui.start()
    webui.mark_application_ready()
    print(f"CARD_ORDER_VALIDATION_URL=http://127.0.0.1:{webui.port}/", flush=True)
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await webui.stop()
        await manager.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=58732)
    arguments = parser.parse_args()
    asyncio.run(serve(arguments.root.resolve(), arguments.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

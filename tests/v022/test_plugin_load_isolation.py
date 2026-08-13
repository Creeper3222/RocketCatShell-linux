from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from rocketcat_shell.layout import ProjectLayout
from rocketcat_shell.plugin_system.manager import RocketCatPluginManager


ROOT = Path(__file__).resolve().parents[2]


def build_layout(root: Path) -> ProjectLayout:
    config_dir = root / "config"
    data_dir = root / "data"
    logs_dir = root / "logs"
    return ProjectLayout(
        project_root=root,
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


class PluginLoadIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_iamthinking_config_is_isolated_and_repairable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            layout = build_layout(root)
            layout.ensure_directories()
            for plugin_id in (
                "rocketcat_plugin_adapt_iamthinking",
                "rocketcat_plugin_built_in_command",
            ):
                shutil.copytree(
                    ROOT / "data" / "plugins" / plugin_id,
                    layout.plugins_dir / plugin_id,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )

            conflict_config = {
                "enabled": True,
                "thinking_emoji_ids": [66],
                "using_tool_emoji_ids": [270],
                "error_emoji_ids": [264],
                "done_emoji_ids": [66],
            }
            config_path = (
                layout.plugins_config_dir
                / "rocketcat_plugin_adapt_iamthinking_config.json"
            )
            config_path.write_text(
                json.dumps(conflict_config, ensure_ascii=False),
                encoding="utf-8",
            )

            manager = RocketCatPluginManager(layout)
            await manager.initialize()
            try:
                plugins = {item["id"]: item for item in manager.list_plugins()}
                broken = plugins["rocketcat_plugin_adapt_iamthinking"]
                healthy = plugins["rocketcat_plugin_built_in_command"]
                self.assertFalse(broken["global_instance_active"])
                self.assertIn("不能交叉重复", broken["load_error"])
                self.assertTrue(healthy["global_instance_active"])

                repaired = await manager.update_plugin_config(
                    "rocketcat_plugin_adapt_iamthinking",
                    {"done_emoji_ids": [74]},
                )
                self.assertIsNone(repaired["load_error"])
                repaired_summary = {
                    item["id"]: item for item in manager.list_plugins()
                }["rocketcat_plugin_adapt_iamthinking"]
                self.assertTrue(repaired_summary["global_instance_active"])
            finally:
                await manager.shutdown()


if __name__ == "__main__":
    unittest.main()

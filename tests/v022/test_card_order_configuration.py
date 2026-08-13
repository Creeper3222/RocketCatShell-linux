from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from rocketcat_shell.layout import ProjectLayout
from rocketcat_shell.models import BotRecord, ShellSettings
from rocketcat_shell.shell.manager import (
    ROCKETCAT_CONFIG_MARKER_FIELD,
    CardOrderConflictError,
    ShellManager,
)


ROOT = Path(__file__).resolve().parents[2]
IAMTHINKING_ID = "rocketcat_plugin_adapt_iamthinking"
COMMAND_ID = "rocketcat_plugin_built_in_command"
V021_FIXTURE = ROOT / "tests" / "v022" / "fixtures" / "rocketcat_config_v021.json"


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


def make_bot(bot_id: str, name: str, settings: ShellSettings) -> BotRecord:
    return BotRecord.from_mapping(
        {"id": bot_id, "name": name, "enabled": False},
        defaults=settings,
    )


class CardOrderConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def build_manager(self, root: Path) -> ShellManager:
        layout = build_layout(root)
        layout.ensure_directories()
        for plugin_id in (IAMTHINKING_ID, COMMAND_ID):
            shutil.copytree(
                ROOT / "data" / "plugins" / plugin_id,
                layout.plugins_dir / plugin_id,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        manager = ShellManager(layout)
        await manager.plugin_manager.initialize()
        manager.settings = ShellSettings.from_mapping({})
        manager.bots = [
            make_bot("bot-a", "Bot A", manager.settings),
            make_bot("bot-b", "Bot B", manager.settings),
        ]
        manager._persist_after_bot_change_locked()

        async def no_runtime_work(*_args, **_kwargs):
            return None

        async def empty_message_index(*_args, **_kwargs):
            return {
                "bot_count": 0,
                "changed_bot_count": 0,
                "removed_message_mapping_count": 0,
            }

        manager._reconcile_runtimes = no_runtime_work
        manager._reload_runtime_plugins = no_runtime_work
        manager._apply_message_index_policy = empty_message_index
        return manager

    async def test_legacy_shell_settings_default_to_empty_orders(self):
        settings = ShellSettings.from_mapping({"webui_port": 5751})
        self.assertEqual({"bots": [], "plugins": []}, settings.ui_card_order)
        self.assertEqual(
            {"bots": [], "plugins": []},
            settings.to_mapping()["ui_card_order"],
        )

    async def test_card_order_save_only_changes_shell_ui_metadata(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = await self.build_manager(Path(temporary_directory))
            try:
                bot_registry_before = manager.layout.bot_registry_path.read_bytes()
                plugin_configs_before = {
                    path.name: path.read_bytes()
                    for path in manager.layout.plugins_config_dir.glob("*_config.json")
                }
                plugin_order = [COMMAND_ID, IAMTHINKING_ID]
                result = await manager.update_card_order(
                    {"bots": ["bot-b", "bot-a"], "plugins": plugin_order}
                )

                self.assertEqual(["bot-b", "bot-a"], result["bots"])
                self.assertEqual(plugin_order, result["plugins"])
                self.assertEqual(bot_registry_before, manager.layout.bot_registry_path.read_bytes())
                self.assertEqual(
                    plugin_configs_before,
                    {
                        path.name: path.read_bytes()
                        for path in manager.layout.plugins_config_dir.glob("*_config.json")
                    },
                )
                persisted = json.loads(manager.layout.shell_settings_path.read_text(encoding="utf-8"))
                self.assertEqual(result, persisted["ui_card_order"])

                with self.assertRaises(CardOrderConflictError):
                    await manager.update_card_order({"bots": ["bot-a"]})
                with self.assertRaisesRegex(ValueError, "重复 ID"):
                    await manager.update_card_order({"plugins": [COMMAND_ID, COMMAND_ID]})
            finally:
                await manager.plugin_manager.shutdown()

    async def test_v021_import_preserves_order_and_fills_iamthinking_defaults(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = await self.build_manager(Path(temporary_directory))
            try:
                manager.settings.ui_card_order = {
                    "bots": ["bot-b", "bot-a"],
                    "plugins": [COMMAND_ID, IAMTHINKING_ID],
                }
                manager._persist_shell_settings()
                payload = json.loads(V021_FIXTURE.read_text(encoding="utf-8"))

                await manager.import_configuration(payload)
                self.assertEqual(["bot-a", "bot-c"], manager.settings.ui_card_order["bots"])
                self.assertEqual(
                    [COMMAND_ID, IAMTHINKING_ID],
                    manager.settings.ui_card_order["plugins"],
                )
                config = json.loads(
                    (
                        manager.layout.plugins_config_dir
                        / f"{IAMTHINKING_ID}_config.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual([66], config["thinking_emoji_ids"])
                self.assertEqual([270], config["using_tool_emoji_ids"])
                self.assertEqual([264], config["error_emoji_ids"])
                self.assertEqual([74], config["done_emoji_ids"])
                self.assertEqual(":tools:", config["llm_using_tool_reaction"])
                self.assertEqual(":octagonal_sign:", config["llm_error_reaction"])
                self.assertEqual(":coffee:", config["llm_thinking_reaction"])
                self.assertEqual(":sparkles:", config["llm_done_reaction"])
                self.assertTrue(config["enabled"])
                self.assertFalse(config["enable_reactions"])
                self.assertTrue(config["enable_typing_indicator"])

                exported = await manager.export_configuration()
                self.assertEqual(
                    manager.settings.ui_card_order,
                    exported["shell_settings"]["ui_card_order"],
                )
                self.assertEqual(config, exported["plugin_configs"][IAMTHINKING_ID])
            finally:
                await manager.plugin_manager.shutdown()

    async def test_invalid_iamthinking_import_is_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = await self.build_manager(Path(temporary_directory))
            try:
                tracked_paths = [
                    manager.layout.shell_settings_path,
                    manager.layout.bot_registry_path,
                    *sorted(manager.layout.plugins_config_dir.glob("*_config.json")),
                ]
                before = {path: path.read_bytes() for path in tracked_paths}
                payload = {
                    ROCKETCAT_CONFIG_MARKER_FIELD: True,
                    "shell_settings": {
                        "webui_access_password": "changed-secret",
                        "webui_port": 6001,
                        "message_index_max_entries": 1000,
                        "ui_card_order": {"bots": ["bot-b", "bot-a"]},
                    },
                    "bots": [bot.to_mapping() for bot in reversed(manager.bots)],
                    "plugin_configs": {
                        IAMTHINKING_ID: {
                            "thinking_emoji_ids": [66, 66],
                            "using_tool_emoji_ids": [270],
                            "error_emoji_ids": [264],
                            "done_emoji_ids": [66],
                        }
                    },
                }

                with self.assertRaisesRegex(ValueError, "四种状态互不重复"):
                    await manager.import_configuration(payload)
                self.assertEqual(before, {path: path.read_bytes() for path in tracked_paths})
            finally:
                await manager.plugin_manager.shutdown()

    async def test_iamthinking_schema_validation_is_shared_by_save_and_import(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = await self.build_manager(Path(temporary_directory))
            try:
                config_path = (
                    manager.layout.plugins_config_dir
                    / f"{IAMTHINKING_ID}_config.json"
                )
                before = config_path.read_bytes()
                with self.assertRaisesRegex(ValueError, "非负安全整数"):
                    await manager.plugin_manager.update_plugin_config(
                        IAMTHINKING_ID,
                        {"thinking_emoji_ids": ["66"]},
                    )
                with self.assertRaisesRegex(ValueError, "四种状态互不重复"):
                    await manager.plugin_manager.update_plugin_config(
                        IAMTHINKING_ID,
                        {
                            "thinking_emoji_ids": [66],
                            "done_emoji_ids": [66],
                        },
                    )
                self.assertEqual(before, config_path.read_bytes())

                normalized = manager.plugin_manager.normalize_import_configs(
                    {IAMTHINKING_ID: {"thinking_emoji_ids": [66, 66]}}
                )
                self.assertEqual(
                    [66],
                    normalized[IAMTHINKING_ID]["thinking_emoji_ids"],
                )
            finally:
                await manager.plugin_manager.shutdown()

    async def test_invalid_import_card_order_is_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = await self.build_manager(Path(temporary_directory))
            try:
                payload = await manager.export_configuration()
                payload["shell_settings"]["ui_card_order"] = {
                    "bots": ["bot-a", "bot-a"],
                    "plugins": [IAMTHINKING_ID, COMMAND_ID],
                }
                tracked_paths = [
                    manager.layout.shell_settings_path,
                    manager.layout.bot_registry_path,
                    *sorted(manager.layout.plugins_config_dir.glob("*_config.json")),
                ]
                before = {path: path.read_bytes() for path in tracked_paths}

                with self.assertRaisesRegex(ValueError, "重复 ID"):
                    await manager.import_configuration(payload)
                self.assertEqual(before, {path: path.read_bytes() for path in tracked_paths})
            finally:
                await manager.plugin_manager.shutdown()

    async def test_explicit_import_order_ignores_unknown_ids_and_appends_missing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = await self.build_manager(Path(temporary_directory))
            try:
                payload = await manager.export_configuration()
                payload["shell_settings"]["ui_card_order"] = {
                    "bots": ["removed-bot", "bot-b"],
                    "plugins": ["removed-plugin", IAMTHINKING_ID],
                }
                await manager.import_configuration(payload)
                self.assertEqual(
                    ["bot-b", "bot-a"],
                    manager.settings.ui_card_order["bots"],
                )
                self.assertEqual(
                    [IAMTHINKING_ID, COMMAND_ID],
                    manager.settings.ui_card_order["plugins"],
                )
            finally:
                await manager.plugin_manager.shutdown()


if __name__ == "__main__":
    unittest.main()

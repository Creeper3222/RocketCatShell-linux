from __future__ import annotations

import asyncio
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..__init__ import __version__
from ..bridge.id_map import DurableIdMap
from ..bridge.runtime import BridgeRuntime
from ..bridge.storage import JsonStore, MessageStore
from ..layout import ProjectLayout
from ..logger import logger
from ..models import BotRecord, DEFAULT_WEBUI_ACCESS_PASSWORD, ShellSettings
from ..plugin_system import RocketCatPluginManager
from ..registry import BotRegistry
from ..settings import load_or_create_shell_settings, read_json, write_json


ROCKETCAT_CONFIG_MARKER_FIELD = "Is rocketcat config"


class ShellManager:
    def __init__(self, layout: ProjectLayout):
        self.layout = layout
        self.settings: ShellSettings | None = None
        self.registry = BotRegistry(layout.bot_registry_path)
        self.plugin_manager = RocketCatPluginManager(layout)
        self.bots: list[BotRecord] = []
        self.runtimes: dict[str, BridgeRuntime] = {}
        self._lock = asyncio.Lock()
        self._runtime_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._webui_host: str | None = None
        self._webui_requested_port: int | None = None
        self._webui_actual_port: int | None = None

    async def initialize(self) -> None:
        self.layout.ensure_directories()
        await self.plugin_manager.initialize()
        self.settings = load_or_create_shell_settings(self.layout.shell_settings_path)
        self.bots = self.registry.load(defaults=self.settings)

        suggested = self.registry.next_suggested_self_id(self.bots)
        if self.settings.next_onebot_self_id < suggested:
            self.settings.next_onebot_self_id = suggested

        self._persist_shell_settings()
        self.registry.save(self.bots)
        await self._reconcile_runtimes("shell initialize")

        logger.info(
            "[RocketCatShell] bootstrap ready | bots=%s | enabled=%s | active_runtimes=%s | project_root=%s",
            len(self.bots),
            sum(1 for bot in self.bots if bot.enabled),
            len(self.runtimes),
            self.layout.project_root,
        )

    async def shutdown(self) -> None:
        self._stop_event.set()
        await self._stop_all_runtimes()
        self.clear_webui_runtime()
        logger.info("[RocketCatShell] shutdown complete.")

    async def run_forever(self) -> None:
        await self._stop_event.wait()

    def request_stop(self) -> None:
        self._stop_event.set()

    def set_webui_runtime(self, *, host: str, requested_port: int, actual_port: int) -> None:
        self._webui_host = str(host)
        self._webui_requested_port = int(requested_port)
        self._webui_actual_port = int(actual_port)

    def clear_webui_runtime(self) -> None:
        self._webui_host = None
        self._webui_requested_port = None
        self._webui_actual_port = None

    def build_status_payload(self) -> dict[str, Any]:
        settings = self._require_settings()
        actual_port = self._webui_actual_port or settings.webui_port
        return {
            "product": "RocketCat",
            "version": __version__,
            "webui": {
                "host": self._webui_host or settings.webui_host,
                "requested_port": settings.webui_port,
                "port": actual_port,
                "auth_enabled": bool(settings.webui_access_password),
                "access_url": f"http://{self._webui_host or settings.webui_host}:{actual_port}/",
            },
            "paths": {
                "project_root": str(self.layout.project_root),
                "config_dir": str(self.layout.config_dir),
                "plugins_config_dir": str(self.layout.plugins_config_dir),
                "data_dir": str(self.layout.data_dir),
                "bots_dir": str(self.layout.bots_dir),
                "plugins_dir": str(self.layout.plugins_dir),
                "plugin_data_dir": str(self.layout.plugin_data_dir),
                "logs_dir": str(self.layout.logs_dir),
            },
            "summary": {
                "bot_count": len(self.bots),
                "enabled_bot_count": sum(1 for bot in self.bots if bot.enabled),
                "active_runtime_count": sum(1 for runtime in self.runtimes.values() if runtime.started),
                "plugin_count": len(self.plugin_manager.list_plugins()),
                "suggested_onebot_self_id": self.registry.next_suggested_self_id(self.bots),
            },
            "shell_settings": self._serialize_shell_settings(settings, mask_secrets=True),
            "bots": [self._serialize_bot(bot, mask_secrets=True) for bot in self.bots],
            "notes": [
                "RocketCatShell is using a unified bot registry.",
                "Main bot / sub bot semantics are intentionally removed.",
                "Local RocketCat plugin management is enabled.",
            ],
        }

    async def get_settings_state(self) -> dict[str, Any]:
        settings = self._require_settings()
        actual_port = self._webui_actual_port or settings.webui_port
        host = self._webui_host or settings.webui_host
        return {
            "webui_auth_enabled": bool(settings.webui_access_password),
            "webui_access_password_is_default": self._is_default_webui_access_password(settings),
            "webui_access_password_hint": "首次登录默认密码为 123456，请在这里修改。",
            "webui_configured_port": settings.webui_port,
            "webui_actual_port": actual_port,
            "webui_access_url": f"http://{host}:{actual_port}/",
            "webui_port_hint": self._build_webui_port_hint(settings),
            "message_index_max_entries": settings.message_index_max_entries,
            "message_index_hint": self._build_message_index_hint(settings.message_index_max_entries),
            "message_index_reset_surrogate_id": DurableIdMap.message_reset_surrogate_id(
                settings.message_index_max_entries
            ),
        }

    async def export_configuration(self) -> dict[str, Any]:
        settings = self._require_settings()
        plugin_configs: dict[str, Any] = {}
        if self.layout.plugins_config_dir.exists():
            for config_path in sorted(self.layout.plugins_config_dir.glob("*_config.json")):
                plugin_id = config_path.name[:-len("_config.json")]
                plugin_configs[plugin_id] = read_json(config_path, {})

        async with self._lock:
            bots = [bot.to_mapping() for bot in self.bots]

        return {
            ROCKETCAT_CONFIG_MARKER_FIELD: True,
            "shell_settings": {
                "webui_access_password": settings.webui_access_password,
                "webui_port": settings.webui_port,
                "message_index_max_entries": settings.message_index_max_entries,
            },
            "bots": bots,
            "plugin_configs": plugin_configs,
        }

    async def import_configuration(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or ROCKETCAT_CONFIG_MARKER_FIELD not in payload:
            raise ValueError("配置导入失败，json文件不为rocketcat配置文件")

        raw_shell_settings = payload.get("shell_settings")
        raw_bots = payload.get("bots")
        raw_plugin_configs = payload.get("plugin_configs", {})

        if not isinstance(raw_shell_settings, dict):
            raise ValueError("配置导入失败，shell_settings 字段无效")
        if not isinstance(raw_bots, list):
            raise ValueError("配置导入失败，bots 字段无效")
        if not isinstance(raw_plugin_configs, dict):
            raise ValueError("配置导入失败，plugin_configs 字段无效")

        settings = self._require_settings()
        candidate_password = str(
            raw_shell_settings.get("webui_access_password", settings.webui_access_password) or ""
        ).strip()
        if not candidate_password:
            raise ValueError("配置导入失败，WebUI 登录密码不能为空")

        try:
            candidate_port = int(raw_shell_settings.get("webui_port", settings.webui_port))
        except (TypeError, ValueError) as exc:
            raise ValueError("配置导入失败，WebUI 访问端口必须是整数") from exc
        if candidate_port < 1 or candidate_port > 65535:
            raise ValueError("配置导入失败，WebUI 访问端口必须在 1 到 65535 之间")

        try:
            candidate_message_index_max_entries = int(
                raw_shell_settings.get(
                    "message_index_max_entries",
                    settings.message_index_max_entries,
                )
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("配置导入失败，最大 message 双向索引储存条数必须是正整数") from exc
        if candidate_message_index_max_entries <= 0:
            raise ValueError("配置导入失败，最大 message 双向索引储存条数必须是正整数")

        candidate_bots = self._build_import_bots(raw_bots, defaults=settings)

        imported_plugin_count = 0
        async with self._lock:
            settings.webui_access_password = candidate_password
            settings.webui_port = candidate_port
            settings.message_index_max_entries = candidate_message_index_max_entries
            self.bots = candidate_bots
            self._persist_after_bot_change_locked()

            for plugin_id, plugin_config in raw_plugin_configs.items():
                normalized_plugin_id = str(plugin_id or "").strip()
                if not normalized_plugin_id:
                    continue
                if not isinstance(plugin_config, dict):
                    raise ValueError(
                        f"配置导入失败，插件 {normalized_plugin_id} 的主配置格式无效"
                    )
                write_json(
                    self.layout.plugins_config_dir / f"{normalized_plugin_id}_config.json",
                    plugin_config,
                )
                imported_plugin_count += 1

        self.plugin_manager.refresh()
        await self._reconcile_runtimes("configuration imported")
        message_index_summary = await self._apply_message_index_policy(
            force_compact=False,
            reason="configuration imported",
        )
        logger.info(
            "[RocketCatShell] 配置导入完成 | bots=%s | plugin_configs=%s | message_index_max_entries=%s",
            len(candidate_bots),
            imported_plugin_count,
            candidate_message_index_max_entries,
        )
        return {
            "bot_count": len(candidate_bots),
            "plugin_config_count": imported_plugin_count,
            "webui_port": candidate_port,
            "message_index_max_entries": candidate_message_index_max_entries,
            "message_index_summary": message_index_summary,
        }

    async def update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = self._require_settings()
        changes: list[str] = []
        should_apply_message_index_policy = False

        async with self._lock:
            if "webui_access_password" in payload:
                candidate_password = str(payload.get("webui_access_password", "") or "").strip()
                if not candidate_password:
                    raise ValueError("请设置登录密码")
                if len(candidate_password) > 128:
                    raise ValueError("WebUI 登录密码不能超过 128 个字符")
                settings.webui_access_password = candidate_password
                changes.append("password")

            if "webui_port" in payload:
                raw_port = payload.get("webui_port")
                try:
                    candidate_port = int(raw_port)
                except (TypeError, ValueError) as exc:
                    raise ValueError("WebUI 访问端口必须是整数") from exc
                if candidate_port < 1 or candidate_port > 65535:
                    raise ValueError("WebUI 访问端口必须在 1 到 65535 之间")
                settings.webui_port = candidate_port
                changes.append("port")

            if "message_index_max_entries" in payload:
                raw_max_entries = payload.get("message_index_max_entries")
                try:
                    candidate_max_entries = int(raw_max_entries)
                except (TypeError, ValueError) as exc:
                    raise ValueError("最大 message 双向索引储存条数必须是正整数") from exc
                if candidate_max_entries <= 0:
                    raise ValueError("最大 message 双向索引储存条数必须是正整数")
                settings.message_index_max_entries = candidate_max_entries
                changes.append("message_index")
                should_apply_message_index_policy = True

            if not changes:
                raise ValueError("未提供可更新的设置项")

            self._persist_shell_settings()

        if should_apply_message_index_policy:
            await self._apply_message_index_policy(
                force_compact=False,
                reason="message index settings updated",
            )

        if "password" in changes:
            logger.info("[RocketCatShell] WebUI 登录认证密码已更新。")
        if "port" in changes:
            logger.info(
                "[RocketCatShell] WebUI 访问端口配置已更新为 %s；当前运行中的监听端口保持不变，重启后优先尝试新端口。",
                settings.webui_port,
            )
        if "message_index" in changes:
            logger.info(
                "[RocketCatShell] 最大 message 双向索引储存条数已更新为 %s；现有索引窗口已按新规则整理。",
                settings.message_index_max_entries,
            )
        return await self.get_settings_state()

    async def rebuild_message_indexes(self) -> dict[str, Any]:
        return await self._apply_message_index_policy(
            force_compact=True,
            reason="manual message index rebuild",
        )

    async def get_webui_state(self) -> dict[str, Any]:
        settings = self._require_settings()
        items = await self.list_bots()
        actual_port = self._webui_actual_port or settings.webui_port
        host = self._webui_host or settings.webui_host
        enabled_count = sum(1 for bot in self.bots if bot.enabled)
        return {
            "bridge_enabled": True,
            "main_bot_enabled": False,
            "independent_webui_enabled": True,
            "independent_webui_port": settings.webui_port,
            "independent_webui_actual_port": actual_port,
            "access_url": f"http://{host}:{actual_port}/",
            "main_bot_onebot_self_id": None,
            "suggested_onebot_self_id": self.registry.next_suggested_self_id(self.bots),
            "enabled_bot_count": enabled_count,
            "bot_count": len(self.bots),
            "items": items,
        }

    async def get_basic_info_state(self) -> dict[str, Any]:
        async with self._lock:
            enabled_bots = [bot for bot in self.bots if bot.enabled]
            runtimes = dict(self.runtimes)

        items: list[dict[str, Any]] = []
        for bot in enabled_bots:
            runtime = runtimes.get(bot.bot_id)
            item = await self._build_basic_info_item(bot=bot, runtime=runtime)
            if item is not None:
                items.append(item)

        items.sort(key=lambda item: str(item.get("client_name") or ""))
        online_count = sum(1 for item in items if item.get("status_code") == "online")
        return {
            "items": items,
            "summary": {
                "enabled_count": len(items),
                "online_count": online_count,
            },
        }

    async def list_bots(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [self._serialize_bot(bot, mask_secrets=False) for bot in self.bots]

    async def create_bot(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            candidate = self._normalize_bot_payload(dict(payload or {}), forced_id=None, existing=None)
            errors = self._validate_bot(candidate, exclude_bot_id=None)
            if errors:
                raise ValueError("；".join(errors))
            self.bots.append(candidate)
            self._persist_after_bot_change_locked()

        await self._reconcile_runtimes("bot created")
        return self._serialize_bot(candidate, mask_secrets=False)

    async def update_bot(self, bot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            target_index = self._find_bot_index(bot_id)
            if target_index < 0:
                raise KeyError(bot_id)

            existing = self.bots[target_index]
            candidate = self._normalize_bot_payload(dict(payload or {}), forced_id=bot_id, existing=existing)
            errors = self._validate_bot(candidate, exclude_bot_id=bot_id)
            if errors:
                raise ValueError("；".join(errors))

            self.bots[target_index] = candidate
            self._persist_after_bot_change_locked()

        await self._reconcile_runtimes("bot updated")
        return self._serialize_bot(candidate, mask_secrets=False)

    async def delete_bot(self, bot_id: str) -> None:
        async with self._lock:
            target_index = self._find_bot_index(bot_id)
            if target_index < 0:
                raise KeyError(bot_id)
            self.bots.pop(target_index)
            self._persist_after_bot_change_locked()

        await self._reconcile_runtimes("bot deleted")

    async def list_plugins(self) -> list[dict[str, Any]]:
        return self.plugin_manager.list_plugins()

    async def get_plugin(self, plugin_id: str) -> dict[str, Any]:
        return self.plugin_manager.get_plugin(plugin_id)

    async def get_plugin_logo_path(self, plugin_id: str) -> Path | None:
        return self.plugin_manager.get_logo_path(plugin_id)

    async def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        plugin = self.plugin_manager.set_plugin_enabled(plugin_id, enabled)
        await self._reconcile_runtimes("plugin enabled updated")
        return plugin

    async def update_plugin_config(self, plugin_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        plugin = self.plugin_manager.update_plugin_config(plugin_id, payload)
        await self._reconcile_runtimes("plugin config updated")
        return plugin

    async def reload_plugin(self, plugin_id: str) -> dict[str, Any]:
        plugin = self.plugin_manager.reload_plugin(plugin_id)
        await self._reconcile_runtimes("plugin reloaded")
        return plugin

    async def uninstall_plugin(
        self,
        plugin_id: str,
        *,
        delete_config: bool = False,
        delete_data: bool = False,
    ) -> dict[str, Any]:
        result = self.plugin_manager.uninstall_plugin(
            plugin_id,
            delete_config=delete_config,
            delete_data=delete_data,
        )
        await self._reconcile_runtimes("plugin uninstalled")
        return result

    def _find_bot_index(self, bot_id: str) -> int:
        for index, bot in enumerate(self.bots):
            if bot.bot_id == bot_id:
                return index
        return -1

    def _serialize_bot(self, bot: BotRecord, *, mask_secrets: bool) -> dict[str, Any]:
        payload = bot.to_mapping()
        if mask_secrets:
            payload["password"] = "***" if payload.get("password") else ""
            payload["e2ee_password"] = "***" if payload.get("e2ee_password") else ""
        payload["validation_errors"] = bot.validate()
        payload["data_dir"] = str(self.layout.bots_dir / bot.bot_id)
        runtime = self.runtimes.get(bot.bot_id)
        payload["runtime_active"] = bool(runtime and runtime.started)
        return payload

    def _serialize_shell_settings(self, settings: ShellSettings, *, mask_secrets: bool) -> dict[str, Any]:
        payload = asdict(settings)
        if mask_secrets:
            payload["webui_access_password"] = "***" if payload.get("webui_access_password") else ""
        return payload

    def _normalize_bot_payload(
        self,
        payload: dict[str, Any],
        *,
        forced_id: str | None,
        existing: BotRecord | None,
    ) -> BotRecord:
        settings = self._require_settings()
        merged = existing.to_mapping() if existing is not None else {}
        merged.update(payload)

        if forced_id is not None:
            merged["id"] = forced_id

        if existing is not None:
            if merged.get("password") == "***":
                merged["password"] = existing.password
            if merged.get("e2ee_password") == "***":
                merged["e2ee_password"] = existing.e2ee_password

        if self._should_fill_default_self_id(payload):
            merged["onebot_self_id"] = self.registry.next_suggested_self_id(self.bots)

        bot = BotRecord.from_mapping(merged, defaults=settings)
        if not bot.bot_id:
            bot.bot_id = self._generate_bot_id()
        if not bot.name:
            bot.name = bot.bot_id
        return bot

    def _validate_bot(self, candidate: BotRecord, *, exclude_bot_id: str | None) -> list[str]:
        errors = candidate.validate()
        for bot in self.bots:
            if bot.bot_id == exclude_bot_id:
                continue
            if bot.bot_id == candidate.bot_id:
                errors.append(f"bot_id {candidate.bot_id} 已存在")
            if candidate.enabled and bot.enabled and bot.onebot_self_id == candidate.onebot_self_id:
                errors.append(f"onebot_self_id {candidate.onebot_self_id} 已被 {bot.name or bot.bot_id} 占用")
        return errors

    def _persist_after_bot_change_locked(self) -> None:
        settings = self._require_settings()
        suggested = self.registry.next_suggested_self_id(self.bots)
        if settings.next_onebot_self_id < suggested:
            settings.next_onebot_self_id = suggested
        self._persist_shell_settings()
        self.registry.save(self.bots)

    async def _reconcile_runtimes(self, reason: str) -> None:
        async with self._runtime_lock:
            await self._stop_all_runtimes()

            started = 0
            for bot in self.bots:
                if not bot.enabled:
                    continue

                runtime = BridgeRuntime(
                    plugin_root=self.layout.package_root,
                    raw_config=bot.to_mapping(),
                    data_dir=self.layout.bots_dir / bot.bot_id,
                    instance_name=bot.name or bot.bot_id,
                    message_index_max_entries=self._require_settings().message_index_max_entries,
                    disable_callback=lambda bot_id=bot.bot_id: self._disable_bot_after_failure(bot_id),
                    plugin_manager=self.plugin_manager,
                )
                await runtime.start()
                if runtime.started:
                    self.runtimes[bot.bot_id] = runtime
                    started += 1

            logger.info(
                "[RocketCatShell] runtime reconcile complete | reason=%s | started=%s | enabled=%s",
                reason,
                started,
                sum(1 for bot in self.bots if bot.enabled),
            )

    async def _stop_all_runtimes(self) -> None:
        if not self.runtimes:
            return

        runtimes = list(self.runtimes.items())
        self.runtimes = {}
        for _, runtime in runtimes:
            await runtime.stop()

    async def _disable_bot_after_failure(self, bot_id: str) -> None:
        changed = False
        async with self._lock:
            for bot in self.bots:
                if bot.bot_id != bot_id:
                    continue
                if bot.enabled:
                    bot.enabled = False
                    changed = True
                break
            if changed:
                self._persist_after_bot_change_locked()

        if changed:
            logger.error("[RocketCatShell] bot %s has been disabled after reconnect failure.", bot_id)

        runtime = self.runtimes.pop(bot_id, None)
        if runtime is not None:
            await runtime.stop()

    async def _build_basic_info_item(
        self,
        *,
        bot: BotRecord,
        runtime: BridgeRuntime | None,
    ) -> dict[str, Any] | None:
        if not bot.enabled:
            return None

        if runtime is not None:
            summary = await runtime.get_basic_info_summary()
            if summary is not None:
                return summary

        username = str(bot.username or "").strip()
        return {
            "bot_id": bot.bot_id,
            "client_name": bot.name or bot.bot_id,
            "login_username": username,
            "nickname": username or bot.name or bot.bot_id,
            "avatar_url": self._guess_avatar_url(bot.server_url, username),
            "status_code": "pending",
            "status_label": "等待连接",
            "server_url": bot.server_url,
            "onebot_self_id": bot.onebot_self_id,
            "server_display_name": "",
            "server_avatar_url": "",
            "is_main_bot": False,
            "user_id": "",
        }

    def _guess_avatar_url(self, server_url: str, username: str) -> str:
        normalized_server = str(server_url or "").strip().rstrip("/")
        normalized_username = str(username or "").strip()
        if not normalized_server or not normalized_username:
            return ""
        return f"{normalized_server}/avatar/{quote(normalized_username, safe='')}"

    def _should_fill_default_self_id(self, payload: dict[str, Any]) -> bool:
        if "onebot_self_id" not in payload:
            return True

        value = payload.get("onebot_self_id")
        if value is None:
            return True
        if isinstance(value, str) and not value.strip():
            return True

        try:
            return int(value) <= 0
        except (TypeError, ValueError):
            return True

    def _generate_bot_id(self) -> str:
        return f"bot_{secrets.token_hex(4)}"

    def _persist_shell_settings(self) -> None:
        settings = self._require_settings()
        write_json(self.layout.shell_settings_path, settings.to_mapping())

    def _build_import_bots(
        self,
        raw_bots: list[Any],
        *,
        defaults: ShellSettings,
    ) -> list[BotRecord]:
        candidate_bots: list[BotRecord] = []
        seen_bot_ids: set[str] = set()
        enabled_self_ids: dict[int, str] = {}

        for index, raw_item in enumerate(raw_bots):
            if not isinstance(raw_item, dict):
                raise ValueError(f"配置导入失败，第 {index + 1} 个 bot 配置不是对象")

            candidate = BotRecord.from_mapping(raw_item, defaults=defaults)
            errors = candidate.validate()
            if candidate.bot_id in seen_bot_ids:
                errors.append(f"bot_id {candidate.bot_id} 重复")
            if candidate.enabled:
                occupied_by = enabled_self_ids.get(candidate.onebot_self_id)
                if occupied_by is not None:
                    errors.append(
                        f"onebot_self_id {candidate.onebot_self_id} 已被 {occupied_by} 占用"
                    )

            if errors:
                raise ValueError(
                    f"配置导入失败，bot {candidate.name or candidate.bot_id or index + 1}: {'；'.join(errors)}"
                )

            seen_bot_ids.add(candidate.bot_id)
            if candidate.enabled:
                enabled_self_ids[candidate.onebot_self_id] = candidate.name or candidate.bot_id
            candidate_bots.append(candidate)

        return candidate_bots

    def _is_default_webui_access_password(self, settings: ShellSettings) -> bool:
        return str(settings.webui_access_password or "") == DEFAULT_WEBUI_ACCESS_PASSWORD

    def _build_webui_port_hint(self, settings: ShellSettings) -> str:
        actual_port = self._webui_actual_port or settings.webui_port
        if actual_port == settings.webui_port:
            return (
                f"当前 WebUI 正在使用配置端口 {settings.webui_port}；若该端口未来被占用，启动时仍会自动回退到可用端口。"
            )
        return (
            f"当前实际访问端口为 {actual_port}，配置端口为 {settings.webui_port}。"
            f" 重启 RocketCat Shell 后会优先尝试 {settings.webui_port}；若被占用仍会自动回退。"
        )

    def _build_message_index_hint(self, max_entries: int) -> str:
        normalized_max_entries = DurableIdMap.normalize_message_window_size(max_entries)
        lower_surrogate_id = DurableIdMap.message_window_lower_surrogate_id()
        upper_surrogate_id = DurableIdMap.message_window_upper_surrogate_id(normalized_max_entries)
        pre_compact_start = upper_surrogate_id + 1
        reset_surrogate_id = DurableIdMap.message_reset_surrogate_id(normalized_max_entries)
        return (
            f"当前最多保留 {normalized_max_entries} 条 message 双向索引。"
            f" 当最新 message 编号达到 {reset_surrogate_id} 时，"
            f"会自动把当前窗口 {pre_compact_start} ~ {reset_surrogate_id} 重新映射为 "
            f"{lower_surrogate_id} ~ {upper_surrogate_id}。"
        )

    async def _apply_message_index_policy(
        self,
        *,
        force_compact: bool,
        reason: str,
    ) -> dict[str, Any]:
        settings = self._require_settings()
        max_entries = DurableIdMap.normalize_message_window_size(settings.message_index_max_entries)

        async with self._runtime_lock:
            async with self._lock:
                bots = list(self.bots)
                runtimes = dict(self.runtimes)

            items: list[dict[str, Any]] = []
            changed_bot_count = 0
            compacted_bot_count = 0
            removed_message_mapping_count = 0

            for bot in bots:
                runtime = runtimes.get(bot.bot_id)
                if runtime is not None:
                    runtime.set_message_index_window_size(max_entries)
                    result = await runtime.rebuild_message_indexes(force_compact=force_compact)
                    runtime_active = bool(runtime.started)
                else:
                    result = await self._apply_offline_message_index_policy(
                        data_dir=self.layout.bots_dir / bot.bot_id,
                        max_entries=max_entries,
                        force_compact=force_compact,
                    )
                    runtime_active = False

                if result.get("changed"):
                    changed_bot_count += 1
                if result.get("compacted"):
                    compacted_bot_count += 1
                removed_message_mapping_count += int(result.get("removed_count") or 0)
                items.append(
                    {
                        "bot_id": bot.bot_id,
                        "name": bot.name or bot.bot_id,
                        "runtime_active": runtime_active,
                        **result,
                    }
                )

        summary = {
            "bot_count": len(bots),
            "changed_bot_count": changed_bot_count,
            "compacted_bot_count": compacted_bot_count,
            "removed_message_mapping_count": removed_message_mapping_count,
            "max_entries": max_entries,
            "reset_surrogate_id": DurableIdMap.message_reset_surrogate_id(max_entries),
            "items": items,
        }
        logger.info(
            "[RocketCatShell] message index policy applied | reason=%s | bots=%s | changed=%s | compacted=%s | removed=%s | max_entries=%s",
            reason,
            summary["bot_count"],
            summary["changed_bot_count"],
            summary["compacted_bot_count"],
            summary["removed_message_mapping_count"],
            max_entries,
        )
        return summary

    async def _apply_offline_message_index_policy(
        self,
        *,
        data_dir: Path,
        max_entries: int,
        force_compact: bool,
    ) -> dict[str, Any]:
        message_store = MessageStore(JsonStore(data_dir / "message_registry.json"))
        id_map = DurableIdMap(
            JsonStore(data_dir / "id_map.json"),
            message_window_size=max_entries,
            on_message_window_changed=message_store.rebuild_for_active_mappings,
        )
        return await id_map.rebuild_message_window(force_compact=force_compact)

    def _require_settings(self) -> ShellSettings:
        if self.settings is None:
            raise RuntimeError("shell settings are not loaded")
        return self.settings

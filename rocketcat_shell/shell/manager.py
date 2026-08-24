from __future__ import annotations

import asyncio
import secrets
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from ..__init__ import __version__
from ..bridge.hot_storage import build_runtime_hot_stores
from ..bridge.id_map import DurableIdMap
from ..bridge.media_publication import MediaPublicationService
from ..bridge.runtime import BridgeRuntime
from ..bridge.transports import (
    TransportValidationError,
    get_transport_spec,
    normalize_transport,
    transport_catalog,
)
from ..bridge.user_identity import UserIdentityRegistry
from ..diagnostics import build_runtime_diagnostic_item, collect_cached_host_diagnostics_with_meta
from ..layout import ProjectLayout
from ..logger import logger, logging_diagnostic_snapshot
from ..models import BotRecord, DEFAULT_WEBUI_ACCESS_PASSWORD, ShellSettings
from ..plugin_system import RocketCatPluginManager
from ..performance import EventLoopLagMonitor
from ..registry import BotRegistry
from ..settings import load_or_create_shell_settings, read_json, write_json
from ..updates import UpdateService


ROCKETCAT_CONFIG_MARKER_FIELD = "Is rocketcat config"
_HOST_DIAGNOSTICS_CACHE_TTL_SECONDS = 3.0
_CARD_ORDER_MAX_ITEMS = 10_000
_CARD_ORDER_MAX_ID_LENGTH = 256


class CardOrderConflictError(ValueError):
    """Raised when a saved order no longer matches the current entity set."""


class BotTransportTypeConflictError(ValueError):
    """Raised when an existing bot attempts to change its transport type."""


class ShellManager:
    def __init__(self, layout: ProjectLayout):
        self.layout = layout
        self.settings: ShellSettings | None = None
        self.registry = BotRegistry(
            layout.bot_registry_path,
            layout.onebot_transports_path,
        )
        self.plugin_manager = RocketCatPluginManager(layout)
        self.media_publication = MediaPublicationService()
        self.updates = UpdateService(layout.project_root, layout.project_root)
        self.bots: list[BotRecord] = []
        self.runtimes: dict[str, BridgeRuntime] = {}
        self._runtime_reload_counts: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._runtime_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()
        self._webui_host: str | None = None
        self._webui_requested_port: int | None = None
        self._webui_actual_port: int | None = None
        self.event_loop_lag = EventLoopLagMonitor()

    async def initialize(self, *, start_runtimes: bool = True) -> None:
        self.layout.ensure_directories()
        await self.event_loop_lag.start()
        await self.plugin_manager.initialize()
        self.settings = load_or_create_shell_settings(self.layout.shell_settings_path)
        self.bots = self.registry.load(defaults=self.settings)

        if self.registry.loaded_transport_state_valid:
            self.registry.save(self.bots)
        if start_runtimes:
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
        try:
            async with asyncio.timeout(30.0):
                await self._stop_all_runtimes()
                await self.plugin_manager.shutdown()
        except asyncio.TimeoutError:
            logger.error("[RocketCatShell] shutdown exceeded the 30 second safety limit")
        await self.event_loop_lag.stop()
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
        self.media_publication.configure_webui(port=actual_port)

    def clear_webui_runtime(self) -> None:
        self.media_publication.clear_webui()
        self._webui_host = None
        self._webui_requested_port = None
        self._webui_actual_port = None

    async def start_enabled_runtimes(self, reason: str = "webui ready") -> None:
        await self._reconcile_runtimes(reason)

    def build_status_payload(self, *, compact: bool = False) -> dict[str, Any]:
        settings = self._require_settings()
        actual_port = self._webui_actual_port or settings.webui_port
        plugin_diagnostics = self.plugin_manager.diagnostic_summary()
        summary = {
            "bot_count": len(self.bots),
            "enabled_bot_count": sum(1 for bot in self.bots if bot.enabled),
            "active_runtime_count": sum(1 for runtime in self.runtimes.values() if runtime.started),
            "plugin_count": (
                self.plugin_manager.cached_plugin_count()
                if compact
                else len(self.plugin_manager.list_plugins())
            ),
            "plugin_global_instance_count": plugin_diagnostics["global_instance_count"],
            "plugin_runtime_binding_count": plugin_diagnostics["runtime_binding_count"],
            "user_identity_algorithm": "sha256-linear-v1",
        }
        webui = {
            "host": self._webui_host or settings.webui_host,
            "requested_port": settings.webui_port,
            "port": actual_port,
            "auth_enabled": bool(settings.webui_access_password),
            "access_url": f"http://{self._webui_host or settings.webui_host}:{actual_port}/",
        }
        if compact:
            return {
                "product": "RocketCat",
                "version": __version__,
                "webui": {"port": actual_port},
                "summary": summary,
            }
        return {
            "product": "RocketCat",
            "version": __version__,
            "webui": webui,
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
            "summary": summary,
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
            "performance_profile": settings.performance_profile,
            "inbound_worker_count": settings.inbound_worker_count,
            "onebot_outgoing_queue_max_entries": settings.onebot_outgoing_queue_max_entries,
            "identity_cache_max_entries": settings.identity_cache_max_entries,
            "media_cache_max_bytes": settings.media_cache_max_bytes,
            "media_cache_max_age_hours": settings.media_cache_max_age_hours,
            "log_file_max_bytes": settings.log_file_max_bytes,
            "log_file_backup_count": settings.log_file_backup_count,
            "terminal_max_sessions": settings.terminal_max_sessions,
            "terminal_idle_timeout_seconds": settings.terminal_idle_timeout_seconds,
            "ui_card_order": await self.get_card_order_state(),
        }

    async def get_card_order_state(self) -> dict[str, list[str]]:
        async with self._lock:
            settings = self._require_settings()
            plugin_ids = self._current_plugin_ids()
            bot_ids = [bot.bot_id for bot in self.bots]
            preferred = settings.ui_card_order
            return {
                "bots": self._canonicalize_card_order(
                    preferred.get("bots", []),
                    bot_ids,
                ),
                "plugins": self._canonicalize_card_order(
                    preferred.get("plugins", []),
                    plugin_ids,
                ),
            }

    async def update_card_order(self, payload: dict[str, Any]) -> dict[str, list[str]]:
        if not isinstance(payload, dict):
            raise ValueError("卡片顺序必须是对象")
        supplied_scopes = [scope for scope in ("bots", "plugins") if scope in payload]
        if not supplied_scopes:
            raise ValueError("请提供 bots 或 plugins 卡片顺序")
        unexpected = sorted(set(payload) - {"bots", "plugins"})
        if unexpected:
            raise ValueError(f"不支持的卡片顺序字段：{', '.join(unexpected)}")
        requested = {
            scope: self._validate_card_order_list(
                payload[scope],
                context=f"{scope} 卡片顺序",
            )
            for scope in supplied_scopes
        }

        async with self._lock:
            settings = self._require_settings()
            plugin_ids = self._current_plugin_ids()
            entity_ids = {
                "bots": [bot.bot_id for bot in self.bots],
                "plugins": plugin_ids,
            }
            current = {
                scope: self._canonicalize_card_order(
                    settings.ui_card_order.get(scope, []),
                    entity_ids[scope],
                )
                for scope in ("bots", "plugins")
            }
            for scope in supplied_scopes:
                if (
                    len(requested[scope]) != len(entity_ids[scope])
                    or set(requested[scope]) != set(entity_ids[scope])
                ):
                    raise CardOrderConflictError(
                        f"{scope} 实体集合已变化，请刷新列表后重新排序"
                    )
                current[scope] = list(requested[scope])
            settings.ui_card_order = current
            self._persist_shell_settings()
            return {
                "bots": list(current["bots"]),
                "plugins": list(current["plugins"]),
            }

    async def export_configuration(self) -> dict[str, Any]:
        settings = self._require_settings()
        plugin_configs = self.plugin_manager.export_configuration_configs()
        plugin_ids = self._current_plugin_ids()

        async with self._lock:
            bots = [bot.to_mapping() for bot in self.bots]
            ui_card_order = {
                "bots": self._canonicalize_card_order(
                    settings.ui_card_order.get("bots", []),
                    [bot.bot_id for bot in self.bots],
                ),
                "plugins": self._canonicalize_card_order(
                    settings.ui_card_order.get("plugins", []),
                    plugin_ids,
                ),
            }

        return {
            ROCKETCAT_CONFIG_MARKER_FIELD: True,
            "shell_settings": {
                "webui_access_password": settings.webui_access_password,
                "webui_port": settings.webui_port,
                "message_index_max_entries": settings.message_index_max_entries,
                "performance_profile": settings.performance_profile,
                "inbound_worker_count": settings.inbound_worker_count,
                "onebot_outgoing_queue_max_entries": settings.onebot_outgoing_queue_max_entries,
                "identity_cache_max_entries": settings.identity_cache_max_entries,
                "media_cache_max_bytes": settings.media_cache_max_bytes,
                "media_cache_max_age_hours": settings.media_cache_max_age_hours,
                "log_file_max_bytes": settings.log_file_max_bytes,
                "log_file_backup_count": settings.log_file_backup_count,
                "terminal_max_sessions": settings.terminal_max_sessions,
                "terminal_idle_timeout_seconds": settings.terminal_idle_timeout_seconds,
                "ui_card_order": ui_card_order,
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
            raise ValueError("配置导入失败，最大消息映射窗口条数必须是正整数") from exc
        if candidate_message_index_max_entries <= 0:
            raise ValueError("配置导入失败，最大消息映射窗口条数必须是正整数")

        candidate_bots = self._build_import_bots(
            raw_bots,
            defaults=settings,
            webui_port=candidate_port,
        )
        candidate_plugin_configs = self.plugin_manager.normalize_import_configs(
            self._normalize_import_plugin_configs(raw_plugin_configs)
        )
        candidate_settings = ShellSettings.from_mapping(
            {**settings.to_mapping(), **raw_shell_settings}
        )
        current_card_order = await self.get_card_order_state()
        candidate_settings.ui_card_order = self._build_import_card_order(
            raw_shell_settings,
            current_order=current_card_order,
            candidate_bot_ids=[bot.bot_id for bot in candidate_bots],
            plugin_ids=self._current_plugin_ids(),
        )

        imported_plugin_count = len(candidate_plugin_configs)
        async with self._lock:
            previous_settings_mapping = settings.to_mapping()
            previous_bots = [bot.to_mapping() for bot in self.bots]
            previous_plugin_config_files = self._snapshot_plugin_config_files()
            try:
                settings.webui_access_password = candidate_password
                settings.webui_port = candidate_port
                settings.message_index_max_entries = candidate_message_index_max_entries
                for field_name in (
                    "performance_profile",
                    "inbound_worker_count",
                    "onebot_outgoing_queue_max_entries",
                    "identity_cache_max_entries",
                    "media_cache_max_bytes",
                    "media_cache_max_age_hours",
                    "log_file_max_bytes",
                    "log_file_backup_count",
                    "terminal_max_sessions",
                    "terminal_idle_timeout_seconds",
                ):
                    setattr(settings, field_name, getattr(candidate_settings, field_name))
                settings.ui_card_order = candidate_settings.ui_card_order
                self.bots = candidate_bots
                self._persist_after_bot_change_locked()
                self._apply_import_plugin_configs(candidate_plugin_configs)
            except Exception as exc:
                try:
                    restored_settings = ShellSettings.from_mapping(previous_settings_mapping)
                    self.settings = restored_settings
                    self.bots = [
                        BotRecord.from_mapping(bot_mapping, defaults=restored_settings)
                        for bot_mapping in previous_bots
                    ]
                    self._persist_after_bot_change_locked()
                    self._restore_plugin_config_files(previous_plugin_config_files)
                except Exception as rollback_exc:
                    logger.exception(
                        "[RocketCatShell] 配置导入失败且回滚失败 | error=%r | rollback_error=%r",
                        exc,
                        rollback_exc,
                    )
                    raise RuntimeError(
                        "配置导入失败，且回滚失败，请检查配置目录与日志。"
                    ) from rollback_exc

                logger.warning(
                    "[RocketCatShell] 配置导入失败，已回滚到导入前状态 | error=%r",
                    exc,
                )
                raise

        self.plugin_manager.refresh(force=True)
        await self._reconcile_runtimes("configuration imported")
        await self._reload_runtime_plugins("configuration imported")
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
        should_reconcile_runtimes = False
        should_apply_media_cache_policy = False

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
                    raise ValueError("最大消息映射窗口条数必须是正整数") from exc
                if candidate_max_entries <= 0:
                    raise ValueError("最大消息映射窗口条数必须是正整数")
                settings.message_index_max_entries = candidate_max_entries
                changes.append("message_index")
                should_apply_message_index_policy = True

            integer_settings = {
                "inbound_worker_count": (0, 8),
                "onebot_outgoing_queue_max_entries": (1, 100000),
                "identity_cache_max_entries": (128, 1000000),
                "media_cache_max_bytes": (1024 * 1024, 1024**4),
                "media_cache_max_age_hours": (1, 24 * 3650),
                "log_file_max_bytes": (1024 * 1024, 1024**4),
                "log_file_backup_count": (0, 100),
                "terminal_max_sessions": (1, 64),
                "terminal_idle_timeout_seconds": (0, 7 * 24 * 3600),
            }
            for field_name, (minimum, maximum) in integer_settings.items():
                if field_name not in payload:
                    continue
                try:
                    candidate_value = int(payload.get(field_name))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{field_name} 必须是整数") from exc
                if candidate_value < minimum or candidate_value > maximum:
                    raise ValueError(
                        f"{field_name} 必须在 {minimum} 到 {maximum} 之间"
                    )
                setattr(settings, field_name, candidate_value)
                changes.append(field_name)
                if field_name in {
                    "inbound_worker_count",
                    "onebot_outgoing_queue_max_entries",
                    "identity_cache_max_entries",
                }:
                    should_reconcile_runtimes = True
                if field_name in {"media_cache_max_bytes", "media_cache_max_age_hours"}:
                    should_apply_media_cache_policy = True

            if "performance_profile" in payload:
                profile = str(payload.get("performance_profile") or "").strip().lower()
                if profile != "balanced":
                    raise ValueError("当前版本仅支持 balanced 性能策略")
                settings.performance_profile = profile
                changes.append("performance_profile")

            if not changes:
                raise ValueError("未提供可更新的设置项")

            self._persist_shell_settings()

        if should_apply_message_index_policy:
            await self._apply_message_index_policy(
                force_compact=False,
                reason="message mapping window settings updated",
            )
        if should_apply_media_cache_policy:
            await asyncio.gather(
                *(
                    runtime.update_media_cache_policy(
                        max_bytes=settings.media_cache_max_bytes,
                        max_age_hours=settings.media_cache_max_age_hours,
                    )
                    for runtime in self.runtimes.values()
                )
            )
        if should_reconcile_runtimes:
            await self._reconcile_runtimes("performance settings updated")

        if "password" in changes:
            logger.info("[RocketCatShell] WebUI 登录认证密码已更新。")
        if "port" in changes:
            logger.info(
                "[RocketCatShell] WebUI 访问端口配置已更新为 %s；当前运行中的监听端口保持不变，重启后优先尝试新端口。",
                settings.webui_port,
            )
        if "message_index" in changes:
            logger.info(
                "[RocketCatShell] 最大消息映射窗口条数已更新为 %s；现有映射窗口已按新规则整理。",
                settings.message_index_max_entries,
            )
        return await self.get_settings_state()

    async def rebuild_message_indexes(self) -> dict[str, Any]:
        return await self._apply_message_index_policy(
            force_compact=True,
            reason="manual message mapping window rebuild",
        )

    async def get_webui_state(self, *, compact: bool = False) -> dict[str, Any]:
        settings = self._require_settings()
        items = [] if compact else await self.list_bots()
        actual_port = self._webui_actual_port or settings.webui_port
        host = self._webui_host or settings.webui_host
        enabled_count = sum(1 for bot in self.bots if bot.enabled)
        payload = {
            "product": "RocketCat",
            "version": __version__,
            "bridge_enabled": True,
            "main_bot_enabled": False,
            "independent_webui_enabled": True,
            "independent_webui_port": settings.webui_port,
            "independent_webui_actual_port": actual_port,
            "access_url": f"http://{host}:{actual_port}/",
            "main_bot_onebot_self_id": None,
            "user_identity_algorithm": "sha256-linear-v1",
            "enabled_bot_count": enabled_count,
            "bot_count": len(self.bots),
        }
        if not compact:
            payload["items"] = items
        return payload

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
            "version": __version__,
            "items": items,
            "summary": {
                "enabled_count": len(items),
                "online_count": online_count,
            },
        }

    async def get_diagnostics_state(self) -> dict[str, Any]:
        settings = self._require_settings()
        async with self._lock:
            bots = list(self.bots)
            runtimes = dict(self.runtimes)

        try:
            host_snapshot, host_cache = await asyncio.to_thread(
                collect_cached_host_diagnostics_with_meta,
                product_version=__version__,
                cache_ttl_seconds=_HOST_DIAGNOSTICS_CACHE_TTL_SECONDS,
            )
            host_error = ""
        except RuntimeError as exc:
            host_snapshot = None
            host_cache = {
                "cache_enabled": _HOST_DIAGNOSTICS_CACHE_TTL_SECONDS > 0,
                "cache_hit": False,
                "cache_status": "error",
                "cache_ttl_seconds": _HOST_DIAGNOSTICS_CACHE_TTL_SECONDS,
                "captured_at": None,
                "snapshot_age_seconds": None,
            }
            host_error = str(exc)
        except Exception as exc:
            logger.warning("[RocketCatShell] 采集主机诊断快照失败: %r", exc)
            host_snapshot = None
            host_cache = {
                "cache_enabled": _HOST_DIAGNOSTICS_CACHE_TTL_SECONDS > 0,
                "cache_hit": False,
                "cache_status": "error",
                "cache_ttl_seconds": _HOST_DIAGNOSTICS_CACHE_TTL_SECONDS,
                "captured_at": None,
                "snapshot_age_seconds": None,
            }
            host_error = "主机诊断快照采集失败，请检查日志。"

        items: list[dict[str, Any]] = []
        for bot in bots:
            runtime = runtimes.get(bot.bot_id)
            if runtime is not None:
                item = runtime.build_diagnostic_summary()
            else:
                item = build_runtime_diagnostic_item(
                    instance_name=bot.name or bot.bot_id,
                    config=bot,
                    rocketchat=None,
                    started=False,
                    data_dir=self.layout.bots_dir / bot.bot_id,
                    message_index_max_entries=settings.message_index_max_entries,
                )
                item["onebot_self_id"] = self._read_persisted_self_id(bot.bot_id) or None
            item.setdefault("onebot_transport_type", bot.onebot_transport_type)
            try:
                item.setdefault(
                    "onebot_transport_label",
                    get_transport_spec(bot.onebot_transport_type).label,
                )
            except TransportValidationError:
                item.setdefault("onebot_transport_label", bot.onebot_transport_type)
            item.setdefault(
                "onebot_transport_status",
                "已停用" if not bot.enabled else "等待启动",
            )
            items.append(item)

        items.sort(
            key=lambda item: (
                item.get("status_code") != "online",
                not item.get("enabled"),
                str(item.get("client_name") or ""),
            )
        )

        online_bot_count = sum(1 for item in items if item.get("status_code") == "online")
        enabled_bot_count = sum(1 for item in items if item.get("enabled"))
        reconnecting_bot_count = sum(
            1
            for item in items
            if item.get("enabled") and int(item.get("reconnect_failures") or 0) > 0
        )
        total_runtime_snapshot_bytes = sum(int(item.get("runtime_snapshot_bytes") or 0) for item in items)
        total_runtime_journal_bytes = sum(int(item.get("runtime_journal_bytes") or 0) for item in items)
        total_inbound_queue_depth = sum(int(item.get("inbound_queue_depth") or 0) for item in items)
        total_outgoing_queue_depth = sum(int(item.get("outgoing_queue_depth") or 0) for item in items)
        # All Windows runtimes share the Shell media coordinator and directory;
        # report it once instead of multiplying the same scan by Bot count.
        total_media_cache_files = max(
            (int((item.get("media_cache") or {}).get("file_count") or 0) for item in items),
            default=0,
        )
        total_media_cache_bytes = max(
            (int((item.get("media_cache") or {}).get("total_bytes") or 0) for item in items),
            default=0,
        )
        total_runtime_restarts = sum(
            int(item.get("runtime_restart_count") or 0) for item in items
        )

        return {
            "version": __version__,
            "host": host_snapshot,
            "host_cache": host_cache,
            "host_error": host_error,
            "performance": {
                "event_loop_lag_ms": self.event_loop_lag.snapshot(),
                "logging": logging_diagnostic_snapshot(),
            },
            "items": items,
            "summary": {
                "bot_count": len(items),
                "enabled_bot_count": enabled_bot_count,
                "online_bot_count": online_bot_count,
                "reconnecting_bot_count": reconnecting_bot_count,
                "total_runtime_snapshot_bytes": total_runtime_snapshot_bytes,
                "total_runtime_journal_bytes": total_runtime_journal_bytes,
                "total_inbound_queue_depth": total_inbound_queue_depth,
                "total_outgoing_queue_depth": total_outgoing_queue_depth,
                "total_media_cache_files": total_media_cache_files,
                "total_media_cache_bytes": total_media_cache_bytes,
                "total_runtime_restarts": total_runtime_restarts,
            },
        }

    async def list_bots(self, *, compact: bool = False) -> list[dict[str, Any]]:
        async with self._lock:
            serializer = self._serialize_bot_compact if compact else self._serialize_bot
            return [serializer(bot, mask_secrets=False) for bot in self.bots]

    async def get_bot(self, bot_id: str) -> dict[str, Any]:
        async with self._lock:
            bot = self._get_bot_or_raise(bot_id)
            return self._serialize_bot(bot, mask_secrets=False)

    def get_onebot_transport_catalog(self) -> list[dict[str, Any]]:
        return transport_catalog(self._require_settings())

    async def create_bot(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            candidate = self._normalize_bot_payload(dict(payload or {}), forced_id=None, existing=None)
            errors = self._validate_bot(candidate, exclude_bot_id=None)
            if errors:
                raise ValueError("；".join(errors))
            self.bots.append(candidate)
            self._persist_after_bot_change_locked()

        await self._reconcile_runtimes("bot created", target_bot_ids={candidate.bot_id})
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

        await self._reconcile_runtimes("bot updated", target_bot_ids={candidate.bot_id})
        return self._serialize_bot(candidate, mask_secrets=False)

    async def list_user_mappings(
        self,
        bot_id: str,
        *,
        search: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        bot = self._get_bot_or_raise(bot_id)
        registry = self._identity_registry_for_bot(bot)
        if registry is None:
            return {
                "ready": False,
                "items": [],
                "total": 0,
                "offset": max(0, int(offset)),
                "limit": max(1, min(200, int(limit))),
                "bot_id": bot.bot_id,
                "bot_name": bot.name or bot.bot_id,
            }
        payload = await registry.list_mappings(
            bot_id=bot.bot_id,
            search=search,
            offset=offset,
            limit=limit,
        )
        payload.update(
            {
                "ready": True,
                "bot_id": bot.bot_id,
                "bot_name": bot.name or bot.bot_id,
            }
        )
        return payload

    async def update_user_mapping(
        self,
        bot_id: str,
        user_id: str,
        *,
        onebot_id: int | str,
        revision: int,
    ) -> dict[str, Any]:
        bot = self._get_bot_or_raise(bot_id)
        registry = self._identity_registry_for_bot(bot)
        if registry is None:
            raise ValueError("该 bot 尚未建立用户映射")
        result = await registry.override_onebot_id(
            bot_id=bot.bot_id,
            user_id=user_id,
            onebot_id=onebot_id,
            revision=revision,
        )
        affected_bots, restart_errors = await self._finalize_identity_mapping_change(
            registry,
            user_id=user_id,
            reason=f"user identity override: {user_id}",
        )
        result["restarted_bot_ids"] = [
            item.bot_id for item in affected_bots if item.enabled
        ]
        result["restart_errors"] = restart_errors
        return result

    async def delete_user_mapping(
        self,
        bot_id: str,
        user_id: str,
        *,
        revision: int,
    ) -> dict[str, Any]:
        bot = self._get_bot_or_raise(bot_id)
        registry = self._identity_registry_for_bot(bot)
        if registry is None:
            raise ValueError("该 bot 尚未建立用户映射")
        result = await registry.delete_mapping(
            bot_id=bot.bot_id,
            user_id=user_id,
            revision=revision,
        )
        affected_bots, restart_errors = await self._finalize_identity_mapping_change(
            registry,
            user_id=user_id,
            reason=f"user identity delete: {user_id}",
        )
        result["restarted_bot_ids"] = [
            item.bot_id for item in affected_bots if item.enabled
        ]
        result["restart_errors"] = restart_errors
        return result

    async def _finalize_identity_mapping_change(
        self,
        registry: UserIdentityRegistry,
        *,
        user_id: str,
        reason: str,
    ) -> tuple[list[BotRecord], list[dict[str, str]]]:
        affected_bots = self._bots_for_identity_database(registry.database_path)
        for affected_bot in affected_bots:
            runtime = self.runtimes.get(affected_bot.bot_id)
            if runtime is not None and runtime.identity_registry is not None:
                runtime.identity_registry.invalidate_cache(user_id)
        await self._sync_identity_warning_files(
            affected_bots,
            registry.database_path,
        )
        restart_errors = await self._restart_bots_after_identity_change(
            affected_bots,
            reason=reason,
        )
        return affected_bots, restart_errors

    async def delete_bot(self, bot_id: str) -> None:
        async with self._lock:
            target_index = self._find_bot_index(bot_id)
            if target_index < 0:
                raise KeyError(bot_id)
            self.bots.pop(target_index)
            self._persist_after_bot_change_locked()

        await self._reconcile_runtimes("bot deleted", target_bot_ids={bot_id})

    async def list_plugins(self) -> list[dict[str, Any]]:
        return self.plugin_manager.list_plugins()

    async def get_plugin(self, plugin_id: str) -> dict[str, Any]:
        return self.plugin_manager.get_plugin(plugin_id)

    async def get_plugin_logo_path(self, plugin_id: str) -> Path | None:
        return self.plugin_manager.get_logo_path(plugin_id)

    async def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        return await self.plugin_manager.set_plugin_enabled(plugin_id, enabled)

    async def update_plugin_config(self, plugin_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.plugin_manager.update_plugin_config(plugin_id, payload)

    async def reload_plugin(self, plugin_id: str) -> dict[str, Any]:
        return await self.plugin_manager.reload_plugin(plugin_id)

    async def uninstall_plugin(
        self,
        plugin_id: str,
        *,
        delete_config: bool = False,
        delete_data: bool = False,
    ) -> dict[str, Any]:
        return await self.plugin_manager.uninstall_plugin(
            plugin_id,
            delete_config=delete_config,
            delete_data=delete_data,
        )

    def _find_bot_index(self, bot_id: str) -> int:
        for index, bot in enumerate(self.bots):
            if bot.bot_id == bot_id:
                return index
        return -1

    def _get_bot_or_raise(self, bot_id: str) -> BotRecord:
        index = self._find_bot_index(str(bot_id))
        if index < 0:
            raise KeyError(bot_id)
        return self.bots[index]

    def _identity_registry_for_bot(
        self,
        bot: BotRecord,
    ) -> UserIdentityRegistry | None:
        runtime = self.runtimes.get(bot.bot_id)
        if runtime is not None and runtime.identity_registry is not None:
            return runtime.identity_registry

        scope_path = self.layout.bots_dir / bot.bot_id / "identity_scope.json"
        payload = read_json(scope_path, {})
        scope_key = str(payload.get("scope_key") or "").strip()
        database_path_text = str(payload.get("database_path") or "").strip()
        if not scope_key or not database_path_text:
            return None
        database_path = Path(database_path_text)
        expected_root = (self.layout.data_dir / "user_identity").resolve()
        try:
            resolved_database_path = database_path.resolve()
        except OSError:
            return None
        if expected_root not in resolved_database_path.parents:
            raise ValueError("用户映射数据库路径超出允许目录")
        if not resolved_database_path.exists():
            return None
        return UserIdentityRegistry(
            resolved_database_path,
            scope_key=scope_key,
            bot_id=bot.bot_id,
            warning_path=self.layout.bots_dir / bot.bot_id / "re_waring.json",
            cache_max_entries=self._require_settings().identity_cache_max_entries,
        )

    def _bots_for_identity_database(self, database_path: Path) -> list[BotRecord]:
        target = database_path.resolve()
        matched: list[BotRecord] = []
        for bot in self.bots:
            registry = self._identity_registry_for_bot(bot)
            if registry is not None and registry.database_path.resolve() == target:
                matched.append(bot)
        return matched

    async def _sync_identity_warning_files(
        self,
        bots: list[BotRecord],
        database_path: Path,
    ) -> None:
        target = database_path.resolve()
        for bot in bots:
            registry = self._identity_registry_for_bot(bot)
            if registry is None or registry.database_path.resolve() != target:
                continue
            await registry.sync_warning_file(
                bot_id=bot.bot_id,
                warning_path=self.layout.bots_dir / bot.bot_id / "re_waring.json",
            )
    async def _restart_bots_after_identity_change(
        self,
        bots: list[BotRecord],
        *,
        reason: str,
    ) -> list[dict[str, str]]:
        errors: list[dict[str, str]] = []
        async with self._runtime_lock:
            for bot in bots:
                try:
                    runtime = self.runtimes.get(bot.bot_id)
                    if runtime is not None:
                        if runtime._hot_store_bundle is not None:
                            runtime._hot_store_bundle.state_engine.purge_legacy_user_dependent_state()
                        await runtime.restart_connections(reason)
                        continue
                    data_dir = self.layout.bots_dir / bot.bot_id
                    if not data_dir.exists():
                        continue
                    stores = build_runtime_hot_stores(
                        data_dir,
                        message_window_size=self._require_settings().message_index_max_entries,
                    )
                    try:
                        stores.state_engine.purge_legacy_user_dependent_state()
                    finally:
                        stores.close()
                except Exception as exc:
                    logger.exception(
                        "[RocketCatShell] user identity override 后重启失败 | bot_id=%s",
                        bot.bot_id,
                    )
                    errors.append(
                        {
                            "bot_id": bot.bot_id,
                            "error": repr(exc),
                        }
                    )
        return errors

    def _serialize_bot(self, bot: BotRecord, *, mask_secrets: bool) -> dict[str, Any]:
        payload = bot.to_mapping()
        if mask_secrets:
            payload["password"] = "***" if payload.get("password") else ""
            payload["e2ee_password"] = "***" if payload.get("e2ee_password") else ""
        payload["validation_errors"] = bot.validate()
        try:
            spec = get_transport_spec(bot.onebot_transport_type)
            payload["onebot_transport_label"] = spec.label
            payload["onebot_transport_card_fields"] = [
                item.to_public_mapping() for item in spec.card_fields
            ]
        except TransportValidationError as exc:
            payload["onebot_transport_label"] = bot.onebot_transport_type
            payload["onebot_transport_card_fields"] = []
            payload["validation_errors"].append(str(exc))
        payload["data_dir"] = str(self.layout.bots_dir / bot.bot_id)
        runtime = self.runtimes.get(bot.bot_id)
        payload["runtime_active"] = bool(runtime and runtime.started)
        if runtime is not None and runtime.onebot is not None:
            transport_snapshot = runtime.onebot.build_diagnostic_snapshot()
            payload["onebot_transport_status"] = transport_snapshot.get(
                "onebot_transport_status", "运行中"
            )
        else:
            payload["onebot_transport_status"] = (
                "已停用" if not bot.enabled else "等待连接"
            )
        payload["onebot_self_id"] = (
            int(runtime.config.onebot_self_id)
            if runtime is not None and runtime.config.onebot_self_id > 0
            else self._read_persisted_self_id(bot.bot_id)
        )
        payload["user_mapping_ready"] = bool(payload["onebot_self_id"])
        return payload

    def _serialize_bot_compact(
        self,
        bot: BotRecord,
        *,
        mask_secrets: bool = False,
    ) -> dict[str, Any]:
        """Serialize only fields used by cards and fallback summaries."""
        del mask_secrets
        validation_errors = bot.validate()
        try:
            spec = get_transport_spec(bot.onebot_transport_type)
            transport_label = spec.label
            card_fields = [item.to_public_mapping() for item in spec.card_fields]
        except TransportValidationError as exc:
            transport_label = bot.onebot_transport_type
            card_fields = []
            validation_errors.append(str(exc))

        card_setting_keys = {
            str(item.get("key") or "")
            for item in card_fields
            if str(item.get("key") or "")
        }
        transport_settings = {
            key: bot.onebot_transport_settings.get(key)
            for key in card_setting_keys
        }
        runtime = self.runtimes.get(bot.bot_id)
        onebot_self_id = (
            int(runtime.config.onebot_self_id)
            if runtime is not None and runtime.config.onebot_self_id > 0
            else self._read_persisted_self_id(bot.bot_id)
        )
        if runtime is not None and runtime.onebot is not None:
            transport_status = runtime.onebot.build_diagnostic_snapshot().get(
                "onebot_transport_status",
                "运行中",
            )
        else:
            transport_status = "已停用" if not bot.enabled else "等待连接"
        return {
            "id": bot.bot_id,
            "name": bot.name,
            "enabled": bot.enabled,
            "server_url": bot.server_url,
            "username": bot.username,
            "onebot_transport": {
                "type": bot.onebot_transport_type,
                "settings": transport_settings,
            },
            "onebot_transport_type": bot.onebot_transport_type,
            "onebot_transport_label": transport_label,
            "onebot_transport_card_fields": card_fields,
            "onebot_transport_status": transport_status,
            "runtime_active": bool(runtime and runtime.started),
            "onebot_self_id": onebot_self_id,
            "user_mapping_ready": bool(onebot_self_id),
            "validation_errors": validation_errors,
        }

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

        incoming_transport = payload.get("onebot_transport")
        if existing is not None and isinstance(incoming_transport, dict):
            incoming_type = str(
                incoming_transport.get("type") or existing.onebot_transport_type
            ).strip().lower()
            if incoming_type != existing.onebot_transport_type:
                raise BotTransportTypeConflictError(
                    "OneBot 网络类型创建后不可修改；请新建 Bot 以使用其他类型"
                )

        legacy_transport_payload: dict[str, Any] | None
        if isinstance(incoming_transport, dict):
            raw_transport = incoming_transport
            legacy_transport_payload = None
        elif existing is not None:
            raw_transport = existing.onebot_transport_mapping()
            legacy_transport_payload = payload
        else:
            raw_transport = None
            legacy_transport_payload = merged

        try:
            normalized_transport = normalize_transport(
                raw_transport,
                shell_defaults=settings,
                for_create=existing is None,
                legacy_payload=legacy_transport_payload,
            )
        except TransportValidationError as exc:
            raise ValueError(str(exc)) from exc

        bot = BotRecord.from_mapping(merged, defaults=settings)
        bot.apply_onebot_transport(normalized_transport)
        if not bot.bot_id:
            bot.bot_id = self._generate_bot_id()
        if not bot.name:
            bot.name = bot.bot_id
        return bot

    def _validate_bot(self, candidate: BotRecord, *, exclude_bot_id: str | None) -> list[str]:
        errors = candidate.validate()
        try:
            normalized = normalize_transport(
                candidate.onebot_transport_mapping(),
                shell_defaults=self._require_settings(),
            )
            candidate.apply_onebot_transport(normalized)
        except TransportValidationError as exc:
            errors.append(str(exc))
        for bot in self.bots:
            if bot.bot_id == exclude_bot_id:
                continue
            if bot.bot_id == candidate.bot_id:
                errors.append(f"bot_id {candidate.bot_id} 已存在")
        errors.extend(
            self._validate_transport_listener_conflicts(
                candidate,
                bots=self.bots,
                exclude_bot_id=exclude_bot_id,
            )
        )
        return errors

    def _validate_transport_listener_conflicts(
        self,
        candidate: BotRecord,
        *,
        bots: list[BotRecord],
        exclude_bot_id: str | None,
        webui_host: str | None = None,
        webui_port: int | None = None,
    ) -> list[str]:
        endpoint = self._transport_listener_endpoint(candidate)
        if endpoint is None:
            return []
        candidate_host, candidate_port = endpoint
        errors: list[str] = []
        for other in bots:
            if other.bot_id == exclude_bot_id or other.bot_id == candidate.bot_id:
                continue
            other_endpoint = self._transport_listener_endpoint(other)
            if other_endpoint is None:
                continue
            other_host, other_port = other_endpoint
            if candidate_port == other_port and self._listener_hosts_overlap(
                candidate_host, other_host
            ):
                errors.append(
                    f"OneBot 监听端口 {candidate_port} 与 Bot {other.name or other.bot_id} 冲突"
                )
        settings = self._require_settings()
        resolved_webui_port = int(
            webui_port or self._webui_actual_port or settings.webui_port
        )
        resolved_webui_host = str(
            webui_host or self._webui_host or settings.webui_host or "127.0.0.1"
        )
        if candidate_port == resolved_webui_port and self._listener_hosts_overlap(
            candidate_host, resolved_webui_host
        ):
            errors.append(f"OneBot 监听端口 {candidate_port} 与 WebUI 冲突")
        return errors

    @staticmethod
    def _transport_listener_endpoint(bot: BotRecord) -> tuple[str, int] | None:
        if not bot.enabled:
            return None
        try:
            spec = get_transport_spec(bot.onebot_transport_type)
        except TransportValidationError:
            return None
        if not spec.server:
            return None
        settings = bot.onebot_transport_settings
        try:
            return str(settings.get("host") or "127.0.0.1"), int(
                settings.get("port")
            )
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _listener_hosts_overlap(first: str, second: str) -> bool:
        def normalize(value: str) -> str:
            normalized = str(value or "").strip().lower().strip("[]")
            if normalized in {"localhost", "::1"}:
                return "127.0.0.1"
            return normalized

        first_normalized = normalize(first)
        second_normalized = normalize(second)
        wildcards = {"", "0.0.0.0", "::", "*"}
        return (
            first_normalized in wildcards
            or second_normalized in wildcards
            or first_normalized == second_normalized
        )

    def _persist_after_bot_change_locked(self) -> None:
        self._persist_shell_settings()
        self.registry.save(self.bots)

    async def _reconcile_runtimes(
        self,
        reason: str,
        *,
        target_bot_ids: set[str] | None = None,
    ) -> None:
        async with self._runtime_lock:
            bots_by_id = {bot.bot_id: bot for bot in self.bots}
            target_ids = (
                set(bots_by_id) | set(self.runtimes)
                if target_bot_ids is None
                else set(target_bot_ids)
            )
            stop_ids: list[str] = []
            start_bots: list[BotRecord] = []
            for bot_id in target_ids:
                bot = bots_by_id.get(bot_id)
                runtime = self.runtimes.get(bot_id)
                if bot is None or not bot.enabled:
                    if runtime is not None:
                        stop_ids.append(bot_id)
                    if bot is None:
                        self._runtime_reload_counts.pop(bot_id, None)
                    continue
                desired_config = self._runtime_config_mapping(bot)
                if (
                    runtime is not None
                    and runtime.started
                    and dict(runtime.raw_config) == desired_config
                ):
                    continue
                if runtime is not None:
                    self._runtime_reload_counts[bot_id] = (
                        max(
                            self._runtime_reload_counts.get(bot_id, 0),
                            int(getattr(runtime, "_restart_count", 0) or 0),
                        )
                        + 1
                    )
                    stop_ids.append(bot_id)
                start_bots.append(bot)

            for bot_id in stop_ids:
                runtime = self.runtimes.pop(bot_id, None)
                if runtime is not None:
                    await runtime.stop()

            semaphore = asyncio.Semaphore(2)

            async def start_one(bot: BotRecord) -> tuple[str, BridgeRuntime | None]:
                async with semaphore:
                    runtime = self._build_runtime(bot)
                    try:
                        await runtime.start()
                    except Exception:
                        logger.exception(
                            "[RocketCatShell] runtime 启动失败 | bot_id=%s | reason=%s",
                            bot.bot_id,
                            reason,
                        )
                        return bot.bot_id, None
                    return bot.bot_id, runtime if runtime.started else None

            results = (
                await asyncio.gather(*(start_one(bot) for bot in start_bots))
                if start_bots
                else []
            )
            for bot_id, runtime in results:
                if runtime is not None:
                    self.runtimes[bot_id] = runtime

            logger.info(
                "[RocketCatShell] runtime incremental reconcile complete | reason=%s | stopped=%s | started=%s | unchanged=%s | enabled=%s",
                reason,
                len(stop_ids),
                sum(1 for _, runtime in results if runtime is not None),
                max(0, len(target_ids) - len(stop_ids) - len(start_bots)),
                sum(1 for bot in self.bots if bot.enabled),
            )

    def _runtime_config_mapping(self, bot: BotRecord) -> dict[str, Any]:
        settings = self._require_settings()
        payload = bot.to_mapping()
        payload.update(
            {
                "inbound_worker_count": settings.inbound_worker_count,
                "onebot_outgoing_queue_max_entries": settings.onebot_outgoing_queue_max_entries,
                "identity_cache_max_entries": settings.identity_cache_max_entries,
                "media_cache_max_bytes": settings.media_cache_max_bytes,
                "media_cache_max_age_hours": settings.media_cache_max_age_hours,
            }
        )
        return payload

    def _build_runtime(self, bot: BotRecord) -> BridgeRuntime:
        runtime = BridgeRuntime(
            plugin_root=self.layout.package_root,
            raw_config=self._runtime_config_mapping(bot),
            data_dir=self.layout.bots_dir / bot.bot_id,
            media_temp_dir=self.layout.temp_dir,
            instance_name=bot.name or bot.bot_id,
            message_index_max_entries=self._require_settings().message_index_max_entries,
            media_publication_service=self.media_publication,
            disable_callback=lambda bot_id=bot.bot_id: self._disable_bot_after_rocketchat_failure(bot_id),
            plugin_manager=self.plugin_manager,
        )
        runtime._restart_count = self._runtime_reload_counts.get(bot.bot_id, 0)
        return runtime

    async def _reload_runtime_plugins(self, reason: str) -> None:
        await self.plugin_manager.reconcile_all(force=True)
        logger.info(
            "[RocketCatShell] 全局插件实例已原子重载 | reason=%s | runtimes=%s",
            reason,
            len(self.runtimes),
        )

    async def _stop_all_runtimes(self) -> None:
        if not self.runtimes:
            return

        runtimes = list(self.runtimes.items())
        self.runtimes = {}
        await asyncio.gather(*(runtime.stop() for _, runtime in runtimes))

    async def _disable_bot_after_rocketchat_failure(self, bot_id: str) -> None:
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
            logger.error(
                "[RocketCatShell] bot %s has been disabled after Rocket.Chat reconnect failure.",
                bot_id,
            )

        runtime = self.runtimes.pop(bot_id, None)
        if runtime is not None:
            self._runtime_reload_counts[bot_id] = max(
                self._runtime_reload_counts.get(bot_id, 0),
                int(getattr(runtime, "_restart_count", 0) or 0),
            )
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
                summary["avatar_url"] = self._build_basic_info_avatar_proxy_url(bot.bot_id)
                if summary.get("server_avatar_url"):
                    summary["server_avatar_url"] = self._build_basic_info_server_avatar_proxy_url(bot.bot_id)
                return summary

        username = str(bot.username or "").strip()
        try:
            transport_label = get_transport_spec(bot.onebot_transport_type).label
        except TransportValidationError:
            transport_label = bot.onebot_transport_type
        return {
            "bot_id": bot.bot_id,
            "client_name": bot.name or bot.bot_id,
            "login_username": username,
            "nickname": username or bot.name or bot.bot_id,
            "avatar_url": self._guess_avatar_url(bot.server_url, username),
            "status_code": "pending",
            "status_label": "等待连接",
            "server_url": bot.server_url,
            "server_version": "unknown",
            "compatibility_status": "unknown",
            "onebot_self_id": self._read_persisted_self_id(bot.bot_id),
            "onebot_transport_type": bot.onebot_transport_type,
            "onebot_transport_label": transport_label,
            "onebot_transport_status": "等待连接",
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

    def _read_persisted_self_id(self, bot_id: str) -> int:
        scope_path = self.layout.bots_dir / str(bot_id) / "identity_scope.json"
        payload = read_json(scope_path, {})
        try:
            value = int(payload.get("onebot_self_id") or 0)
        except (TypeError, ValueError):
            return 0
        return value if value > 0 else 0

    def _build_basic_info_avatar_proxy_url(self, bot_id: str) -> str:
        normalized_bot_id = str(bot_id or "").strip()
        if not normalized_bot_id:
            return ""
        return f"/api/basic-info/avatar?bot_id={quote(normalized_bot_id, safe='')}"

    def _build_basic_info_server_avatar_proxy_url(self, bot_id: str) -> str:
        normalized_bot_id = str(bot_id or "").strip()
        if not normalized_bot_id:
            return ""
        return f"/api/basic-info/server-avatar?bot_id={quote(normalized_bot_id, safe='')}"

    def _get_basic_info_runtime_client(self, bot_id: str):
        normalized_bot_id = str(bot_id or "").strip()
        if not normalized_bot_id:
            return None

        runtime = self.runtimes.get(normalized_bot_id)
        if runtime is None or runtime.rocketchat is None:
            return None

        client = runtime.rocketchat
        if client._http_session is None:
            return None
        return client

    async def _fetch_basic_info_media_content(
        self,
        bot_id: str,
        media_url: str,
        *,
        asset_label: str,
    ) -> tuple[bytes, str] | None:
        normalized_bot_id = str(bot_id or "").strip()
        client = self._get_basic_info_runtime_client(normalized_bot_id)
        if client is None:
            return None

        normalized_media_url = str(media_url or "").strip()
        if not normalized_media_url:
            return None

        fetch_url = await client._normalize_media_url(normalized_media_url)
        try:
            async with client._http_session.get(
                fetch_url,
                timeout=aiohttp.ClientTimeout(total=15, connect=5),
                allow_redirects=True,
                max_redirects=3,
            ) as resp:
                if resp.status >= 400:
                    logger.warning(
                        "[RocketCatShell] 获取基础信息%s失败: bot_id=%s status=%s url=%s",
                        asset_label,
                        normalized_bot_id,
                        resp.status,
                        normalized_media_url,
                    )
                    return None
                content = await resp.read()
                if not content:
                    return None
                content_type = str(resp.headers.get("Content-Type") or "application/octet-stream")
                return content, content_type
        except Exception as exc:
            logger.warning(
                "[RocketCatShell] 获取基础信息%s异常: bot_id=%s url=%s err=%r",
                asset_label,
                normalized_bot_id,
                normalized_media_url,
                exc,
            )
            return None

    async def get_basic_info_avatar_content(self, bot_id: str) -> tuple[bytes, str] | None:
        normalized_bot_id = str(bot_id or "").strip()
        if not normalized_bot_id:
            return None

        client = self._get_basic_info_runtime_client(normalized_bot_id)
        if client is None:
            return None

        try:
            user_info = await client.get_current_user_info()
        except Exception as exc:
            logger.warning(
                "[RocketCatShell] 读取基础信息头像用户资料失败: bot_id=%s err=%r",
                normalized_bot_id,
                exc,
            )
            user_info = {}

        username = str(
            (user_info or {}).get("username")
            or client.bot_username
            or client.config.username
            or ""
        ).strip()
        avatar_url = client.resolve_avatar_url(user_info if isinstance(user_info, dict) else None, username)
        if not avatar_url:
            return None

        return await self._fetch_basic_info_media_content(
            normalized_bot_id,
            avatar_url,
            asset_label="头像",
        )

    async def get_basic_info_server_avatar_content(self, bot_id: str) -> tuple[bytes, str] | None:
        normalized_bot_id = str(bot_id or "").strip()
        if not normalized_bot_id:
            return None

        client = self._get_basic_info_runtime_client(normalized_bot_id)
        if client is None:
            return None

        try:
            server_branding_summary = await client.get_server_branding_summary()
        except Exception as exc:
            logger.warning(
                "[RocketCatShell] 读取基础信息服务器头像品牌信息失败: bot_id=%s err=%r",
                normalized_bot_id,
                exc,
            )
            server_branding_summary = None

        server_avatar_url = ""
        if server_branding_summary:
            server_avatar_url = str(server_branding_summary.get("avatar_url") or "").strip()
        if not server_avatar_url:
            server_avatar_url = str(client.build_server_logo_url() or "").strip()
        if not server_avatar_url:
            return None

        return await self._fetch_basic_info_media_content(
            normalized_bot_id,
            server_avatar_url,
            asset_label="服务器头像",
        )

    def _generate_bot_id(self) -> str:
        return f"bot_{secrets.token_hex(4)}"

    def _persist_shell_settings(self) -> None:
        settings = self._require_settings()
        write_json(self.layout.shell_settings_path, settings.to_mapping())

    def _current_plugin_ids(self) -> list[str]:
        return [
            str(item.get("id") or "")
            for item in self.plugin_manager.list_plugins()
            if str(item.get("id") or "")
        ]

    def _validate_card_order_list(
        self,
        value: Any,
        *,
        context: str,
    ) -> list[str]:
        if not isinstance(value, list):
            raise ValueError(f"{context}必须是数组")
        if len(value) > _CARD_ORDER_MAX_ITEMS:
            raise ValueError(f"{context}不能超过 {_CARD_ORDER_MAX_ITEMS} 项")
        normalized: list[str] = []
        seen: set[str] = set()
        for index, raw_item in enumerate(value):
            if not isinstance(raw_item, str):
                raise ValueError(f"{context}第 {index + 1} 项必须是字符串")
            item = raw_item.strip()
            if not item:
                raise ValueError(f"{context}第 {index + 1} 项不能为空")
            if len(item) > _CARD_ORDER_MAX_ID_LENGTH:
                raise ValueError(
                    f"{context}第 {index + 1} 项不能超过 {_CARD_ORDER_MAX_ID_LENGTH} 个字符"
                )
            if item in seen:
                raise ValueError(f"{context}包含重复 ID：{item}")
            seen.add(item)
            normalized.append(item)
        return normalized

    def _canonicalize_card_order(
        self,
        preferred: Any,
        entity_ids: list[str],
    ) -> list[str]:
        available = set(entity_ids)
        ordered: list[str] = []
        seen: set[str] = set()
        if isinstance(preferred, list):
            for raw_item in preferred:
                if not isinstance(raw_item, str):
                    continue
                item = raw_item.strip()
                if item in available and item not in seen:
                    seen.add(item)
                    ordered.append(item)
        for item in entity_ids:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        return ordered

    def _build_import_card_order(
        self,
        raw_shell_settings: dict[str, Any],
        *,
        current_order: dict[str, list[str]],
        candidate_bot_ids: list[str],
        plugin_ids: list[str],
    ) -> dict[str, list[str]]:
        entity_ids = {"bots": candidate_bot_ids, "plugins": plugin_ids}
        raw_order = raw_shell_settings.get("ui_card_order", None)
        if "ui_card_order" not in raw_shell_settings:
            return {
                scope: self._canonicalize_card_order(current_order[scope], entity_ids[scope])
                for scope in ("bots", "plugins")
            }
        if not isinstance(raw_order, dict):
            raise ValueError("配置导入失败，ui_card_order 字段必须是对象")
        unexpected = sorted(set(raw_order) - {"bots", "plugins"})
        if unexpected:
            raise ValueError(
                f"配置导入失败，ui_card_order 包含不支持的字段：{', '.join(unexpected)}"
            )
        normalized: dict[str, list[str]] = {}
        for scope in ("bots", "plugins"):
            if scope in raw_order:
                preferred = self._validate_card_order_list(
                    raw_order[scope],
                    context=f"配置导入失败，ui_card_order.{scope} ",
                )
            else:
                preferred = current_order[scope]
            normalized[scope] = self._canonicalize_card_order(
                preferred,
                entity_ids[scope],
            )
        return normalized

    def _normalize_import_plugin_configs(
        self,
        raw_plugin_configs: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        normalized_plugin_configs: dict[str, dict[str, Any]] = {}
        for plugin_id, plugin_config in raw_plugin_configs.items():
            normalized_plugin_id = str(plugin_id or "").strip()
            if not normalized_plugin_id:
                continue
            if not isinstance(plugin_config, dict):
                raise ValueError(
                    f"配置导入失败，插件 {normalized_plugin_id} 的主配置格式无效"
                )
            normalized_plugin_configs[normalized_plugin_id] = plugin_config
        return normalized_plugin_configs

    def _snapshot_plugin_config_files(self) -> dict[str, bytes]:
        if not self.layout.plugins_config_dir.exists():
            return {}

        snapshot: dict[str, bytes] = {}
        for config_path in sorted(self.layout.plugins_config_dir.glob("*_config.json")):
            snapshot[config_path.name] = config_path.read_bytes()
        return snapshot

    def _apply_import_plugin_configs(self, plugin_configs: dict[str, dict[str, Any]]) -> None:
        for plugin_id, plugin_config in plugin_configs.items():
            write_json(
                self.layout.plugins_config_dir / f"{plugin_id}_config.json",
                plugin_config,
            )

    def _restore_plugin_config_files(self, snapshot: dict[str, bytes]) -> None:
        self.layout.plugins_config_dir.mkdir(parents=True, exist_ok=True)
        expected_names = set(snapshot)
        for file_name, payload in snapshot.items():
            (self.layout.plugins_config_dir / file_name).write_bytes(payload)
        for config_path in self.layout.plugins_config_dir.glob("*_config.json"):
            if config_path.name not in expected_names:
                config_path.unlink(missing_ok=True)

    def _build_import_bots(
        self,
        raw_bots: list[Any],
        *,
        defaults: ShellSettings,
        webui_port: int,
    ) -> list[BotRecord]:
        candidate_bots: list[BotRecord] = []
        seen_bot_ids: set[str] = set()
        for index, raw_item in enumerate(raw_bots):
            if not isinstance(raw_item, dict):
                raise ValueError(f"配置导入失败，第 {index + 1} 个 bot 配置不是对象")

            candidate = BotRecord.from_mapping(raw_item, defaults=defaults)
            tagged_transport = (
                raw_item.get("onebot_transport")
                if isinstance(raw_item.get("onebot_transport"), dict)
                else None
            )
            try:
                candidate.apply_onebot_transport(
                    normalize_transport(
                        tagged_transport,
                        shell_defaults=defaults,
                        legacy_payload=None if tagged_transport is not None else raw_item,
                    )
                )
            except TransportValidationError as exc:
                raise ValueError(
                    f"配置导入失败，bot {candidate.name or candidate.bot_id or index + 1}: {exc}"
                ) from exc
            errors = candidate.validate()
            if candidate.bot_id in seen_bot_ids:
                errors.append(f"bot_id {candidate.bot_id} 重复")
            if errors:
                raise ValueError(
                    f"配置导入失败，bot {candidate.name or candidate.bot_id or index + 1}: {'；'.join(errors)}"
                )

            seen_bot_ids.add(candidate.bot_id)
            candidate_bots.append(candidate)

        for candidate in candidate_bots:
            conflicts = self._validate_transport_listener_conflicts(
                candidate,
                bots=candidate_bots,
                exclude_bot_id=candidate.bot_id,
                webui_port=webui_port,
            )
            if conflicts:
                raise ValueError(
                    f"配置导入失败，bot {candidate.name or candidate.bot_id}: {'；'.join(conflicts)}"
                )

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
            f"当前最多保留 {normalized_max_entries} 条最近 message 映射。"
            f" 当最新 message 编号达到 {reset_surrogate_id} 时，"
            f"会自动把当前映射窗口 {pre_compact_start} ~ {reset_surrogate_id} 重新映射为 "
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
            "[RocketCatShell] message mapping window policy applied | reason=%s | bots=%s | changed=%s | compacted=%s | removed=%s | max_entries=%s",
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
        hot_stores = build_runtime_hot_stores(
            data_dir,
            message_window_size=max_entries,
        )
        try:
            return await hot_stores.id_map.rebuild_message_window(force_compact=force_compact)
        finally:
            hot_stores.close()

    def _require_settings(self) -> ShellSettings:
        if self.settings is None:
            raise RuntimeError("shell settings are not loaded")
        return self.settings

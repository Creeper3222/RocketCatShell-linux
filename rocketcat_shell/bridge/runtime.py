from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Awaitable, Callable

from rocketcat_shell.diagnostics import build_runtime_diagnostic_item
from rocketcat_shell.logger import logger
from rocketcat_shell.plugin_system import (
    PluginExecutionContext,
    RocketCatPluginManager,
    RuntimePluginBinding,
)

from .config import BridgeConfig
from .hot_storage import RuntimeHotStoreBundle, build_runtime_hot_stores
from .id_map import DurableIdMap
from .media_publication import MediaPublicationService
from .onebot_actions import OneBotActionHandler
from .onebot_client import OneBotReverseWsClient
from .paths import resolve_plugin_data_dir
from .perf import maybe_trace, perf_enabled, perf_stage
from .rocketchat_client import RocketChatClient
from .storage import JsonStore
from .translator_inbound import InboundTranslator
from .translator_outbound import OutboundMessageTranslator
from .user_identity import UserIdentityIdMap, UserIdentityRegistry


DisableCallback = Callable[[], Awaitable[None] | None]


class BridgeRuntime:
    def __init__(
        self,
        plugin_root: Path,
        raw_config: dict[str, Any] | Any,
        *,
        data_dir: Path | None = None,
        media_temp_dir: Path | None = None,
        instance_name: str = "bridge",
        message_index_max_entries: int = DurableIdMap._DEFAULT_MESSAGE_WINDOW_SIZE,
        media_publication_service: MediaPublicationService | None = None,
        disable_callback: DisableCallback | None = None,
        plugin_manager: RocketCatPluginManager | None = None,
    ):
        self.plugin_root = plugin_root
        self.raw_config = raw_config
        self.instance_name = instance_name
        if data_dir is None:
            data_dir = resolve_plugin_data_dir(plugin_root)
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if media_temp_dir is None:
            media_temp_dir = self.data_dir.parent.parent / "temp"
        self.media_temp_dir = media_temp_dir
        self.media_temp_dir.mkdir(parents=True, exist_ok=True)
        self.message_index_max_entries = DurableIdMap.normalize_message_window_size(
            message_index_max_entries
        )
        self.media_publication_service = media_publication_service
        self._disable_callback = disable_callback
        self._plugin_manager = plugin_manager

        self.config = BridgeConfig.from_mapping(raw_config)
        self.state_store = JsonStore(self.data_dir / "runtime_state.json")
        self._hot_store_bundle: RuntimeHotStoreBundle | None = None
        self._base_id_map: DurableIdMap | None = None
        self._ensure_hot_store_bundle()
        self.rocketchat: RocketChatClient | None = None
        self.inbound_translator: InboundTranslator | None = None
        self.outbound_translator: OutboundMessageTranslator | None = None
        self.action_handler: OneBotActionHandler | None = None
        self.onebot: OneBotReverseWsClient | None = None
        self.identity_registry: UserIdentityRegistry | None = None
        self.identity_database_path: Path | None = None
        self._plugin_runtime_context: PluginExecutionContext | None = None
        self._runtime_plugins: list[RuntimePluginBinding] = []
        self._failure_task: asyncio.Task | None = None
        self._restart_lock = asyncio.Lock()
        self._failure_handled = False
        self._started = False

    @classmethod
    def from_plugin_root(cls, raw_config: dict[str, Any] | Any) -> "BridgeRuntime":
        plugin_root = Path(__file__).resolve().parent.parent
        return cls(plugin_root=plugin_root, raw_config=raw_config)

    async def start(self) -> None:
        self._reload_config_snapshot()
        self._ensure_hot_store_bundle()
        errors = self.config.validate()
        if errors:
            logger.error(
                f"[RocketChatOneBotBridge][{self.instance_name}] 配置校验失败: {'; '.join(errors)}"
            )
            return

        if not self.config.enabled:
            logger.info(
                f"[RocketChatOneBotBridge][{self.instance_name}] 当前 bot enabled=false，桥接不会启动。"
            )
            return

        await self._start_clients()

        await self.state_store.write(
            {
                "status": "running",
                "server_url": self.config.server_url,
                "onebot_ws_url": self.config.onebot_ws_url,
                "onebot_self_id": self.config.onebot_self_id,
                "max_reconnect_attempts": self.config.max_reconnect_attempts,
            }
        )
        self._started = True
        logger.info(f"[RocketChatOneBotBridge][{self.instance_name}] bridge 运行时已启动。")

    async def stop(self) -> None:
        if self._failure_task is not None:
            self._failure_task.cancel()
            try:
                await self._failure_task
            except asyncio.CancelledError:
                pass
            self._failure_task = None
        await self._stop_clients()
        await self.state_store.write({"status": "stopped"})
        if self._hot_store_bundle is not None:
            hot_store_bundle = self._hot_store_bundle
            self._hot_store_bundle = None
            await asyncio.to_thread(hot_store_bundle.close)
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    def set_message_index_window_size(self, message_index_max_entries: int) -> None:
        self.message_index_max_entries = DurableIdMap.normalize_message_window_size(
            message_index_max_entries
        )
        self._ensure_hot_store_bundle()
        self.id_map.set_message_window_size(self.message_index_max_entries)
        if isinstance(self.raw_config, dict):
            self.raw_config["message_index_max_entries"] = self.message_index_max_entries

    async def rebuild_message_indexes(self, *, force_compact: bool = False) -> dict[str, Any]:
        self._ensure_hot_store_bundle()
        return await self.id_map.rebuild_message_window(force_compact=force_compact)

    async def get_basic_info_summary(self) -> dict[str, Any] | None:
        self._reload_config_snapshot()
        if not self.config.enabled:
            return None

        client_name = self.config.display_name or self.instance_name
        login_username = str(self.config.username or "").strip()
        nickname = login_username or client_name
        avatar_url = ""
        user_id = ""
        server_display_name = ""
        server_avatar_url = ""

        if self.rocketchat is not None:
            try:
                user_info = await self.rocketchat.get_current_user_info()
            except Exception:
                user_info = {}

            if user_info:
                login_username = str(
                    user_info.get("username")
                    or self.rocketchat.bot_username
                    or login_username
                ).strip()
                nickname = str(
                    user_info.get("name")
                    or user_info.get("nickname")
                    or login_username
                    or client_name
                ).strip()
                user_id = str(user_info.get("_id") or self.rocketchat.user_id or "").strip()
            else:
                login_username = str(self.rocketchat.bot_username or login_username).strip()
                user_id = str(self.rocketchat.user_id or "").strip()

            avatar_url = self.rocketchat.build_avatar_url(login_username)
            if self.rocketchat.auth_token and self.rocketchat.user_id:
                try:
                    server_branding_summary = await self.rocketchat.get_server_branding_summary()
                except Exception:
                    server_branding_summary = None
                if server_branding_summary:
                    server_display_name = str(server_branding_summary.get("display_name") or "").strip()
                    server_avatar_url = str(server_branding_summary.get("avatar_url") or "").strip()

        status_code = "offline"
        status_label = "未接入"
        if self.started:
            status_code = "online" if self.rocketchat and self.rocketchat.auth_token and self.rocketchat.user_id else "starting"
            status_label = "已连接" if status_code == "online" else "连接中"

        return {
            "bot_id": self.config.bot_id,
            "client_name": client_name,
            "login_username": login_username,
            "nickname": nickname or login_username or client_name,
            "avatar_url": avatar_url,
            "status_code": status_code,
            "status_label": status_label,
            "server_url": self.config.server_url,
            "onebot_self_id": self.config.onebot_self_id,
            "server_display_name": server_display_name,
            "server_avatar_url": server_avatar_url,
            "is_main_bot": False,
            "user_id": user_id,
        }

    def build_diagnostic_summary(self) -> dict[str, Any]:
        self._reload_config_snapshot()
        return build_runtime_diagnostic_item(
            instance_name=self.instance_name,
            config=self.config,
            rocketchat=self.rocketchat,
            started=self.started,
            data_dir=self.data_dir,
            message_index_max_entries=self.message_index_max_entries,
        )

    async def _handle_rocketchat_message(self, raw_msg: dict[str, Any]) -> None:
        if self.inbound_translator is None or self.onebot is None:
            return
        trace = maybe_trace(
            perf_enabled(self.config),
            "rocketchat_receive_to_emit",
            tags={
                "instance": self.instance_name,
                "room_id": str(raw_msg.get("rid") or ""),
                "source_message_id": str(raw_msg.get("_id") or ""),
            },
        )
        try:
            with perf_stage(trace, "translate"):
                event = await self.inbound_translator.translate(raw_msg, perf_trace=trace)
            if event is None:
                if trace is not None:
                    trace.finish(status="dropped")
                return
            suppressed_by_plugin = False
            try:
                suppressed_by_plugin = await self._dispatch_inbound_message_plugins(raw_msg, event)
            except Exception:
                logger.exception("[BridgeRuntime] inbound message plugin dispatch failed")
            if suppressed_by_plugin:
                if trace is not None:
                    trace.finish(status="intercepted_by_plugin")
                return
            with perf_stage(trace, "emit_event"):
                await self.onebot.emit_event(event)
            if trace is not None:
                trace.finish(status="ok", message_id=event.get("message_id"))
        except Exception:
            if trace is not None:
                trace.finish(status="error")
            raise

    async def _dispatch_inbound_message_plugins(
        self,
        raw_msg: dict[str, Any],
        event: dict[str, Any],
    ) -> bool:
        if not self._runtime_plugins or self._plugin_runtime_context is None:
            return False

        for binding in self._runtime_plugins:
            plugin = binding.instance
            if not plugin.enabled:
                continue
            try:
                result = await asyncio.wait_for(
                    plugin.on_inbound_message(
                        dict(event),
                        dict(raw_msg),
                        self._plugin_runtime_context,
                    ),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "[RocketCatShell] 插件 %s 处理入站消息超时。",
                    binding.descriptor.plugin_id,
                )
                continue
            except Exception as exc:
                logger.error(
                    "[RocketCatShell] 插件 %s 处理入站消息失败: %r",
                    binding.descriptor.plugin_id,
                    exc,
                )
                continue
            if result is False:
                return True
        return False

    def _reload_config_snapshot(self) -> None:
        self.config = BridgeConfig.from_mapping(self.raw_config)

    async def _start_clients(self) -> None:
        self._reload_config_snapshot()
        self._ensure_hot_store_bundle()
        self._failure_handled = False

        self.rocketchat = RocketChatClient(
            self.config,
            media_publication_service=self.media_publication_service,
            media_temp_dir=self.media_temp_dir,
            on_message=self._handle_rocketchat_message,
            on_reconnect_exhausted=self._handle_reconnect_exhausted,
        )

        try:
            # REST login must finish before realtime or OneBot starts. The
            # immutable Rocket.Chat userId determines this bot's OneBot self_id.
            await self.rocketchat.start(start_realtime=False)
            await self._initialize_user_identity()
            self.inbound_translator = InboundTranslator(
                rocketchat=self.rocketchat,
                id_map=self.id_map,
                messages=self.message_store,
                private_rooms=self.private_room_store,
                context_rooms=self.context_room_store,
                self_id=self.config.onebot_self_id,
            )
            self.outbound_translator = OutboundMessageTranslator(
                rocketchat=self.rocketchat,
                id_map=self.id_map,
                messages=self.message_store,
                private_rooms=self.private_room_store,
                context_rooms=self.context_room_store,
            )
            self._plugin_runtime_context = PluginExecutionContext(
                instance_name=self.instance_name,
                bridge_config=self.config,
                rocketchat=self.rocketchat,
                id_map=self.id_map,
                messages=self.message_store,
                private_rooms=self.private_room_store,
                context_rooms=self.context_room_store,
                inbound=self.inbound_translator,
                outbound=self.outbound_translator,
            )
            if self._plugin_manager is not None and self._plugin_runtime_context is not None:
                self._runtime_plugins = await self._plugin_manager.create_runtime_plugins(self._plugin_runtime_context)
            else:
                self._runtime_plugins = []
            self.action_handler = OneBotActionHandler(
                config=self.config,
                rocketchat=self.rocketchat,
                id_map=self.id_map,
                messages=self.message_store,
                private_rooms=self.private_room_store,
                context_rooms=self.context_room_store,
                inbound=self.inbound_translator,
                outbound=self.outbound_translator,
                plugin_action_dispatcher=self._dispatch_plugin_action,
            )
            self.onebot = OneBotReverseWsClient(
                self.config,
                action_handler=self.action_handler.handle,
                on_reconnect_exhausted=self._handle_reconnect_exhausted,
            )
            await self.rocketchat.start_realtime()
            await self.onebot.start()
        except Exception:
            await self._stop_clients()
            raise

    async def _initialize_user_identity(self) -> None:
        if self.rocketchat is None or not self.rocketchat.user_id:
            raise RuntimeError("Rocket.Chat 登录尚未提供 userId")
        if self._base_id_map is None or self._hot_store_bundle is None:
            raise RuntimeError("runtime hot store 尚未初始化")

        data_root = self.data_dir.parent.parent
        warning_path = self.data_dir / "re_waring.json"
        registry = UserIdentityRegistry.for_server(
            data_root,
            server_url=self.config.server_url,
            cloud_workspace_id=self.rocketchat.cloud_workspace_id,
            bot_id=self.config.bot_id,
            warning_path=warning_path,
        )
        bot_profile = self.rocketchat.bot_profile or {}
        self_mapping = await registry.ensure_mapping(
            self.rocketchat.user_id,
            username=str(
                bot_profile.get("username")
                or self.rocketchat.bot_username
                or self.config.username
                or ""
            ),
            nickname=str(
                bot_profile.get("name")
                or bot_profile.get("nickname")
                or self.rocketchat.bot_username
                or self.config.username
                or ""
            ),
            is_bot=True,
            bot_id=self.config.bot_id,
        )
        self.identity_registry = registry
        self.identity_database_path = registry.database_path
        self.id_map = UserIdentityIdMap(self._base_id_map, registry)
        self.config.onebot_self_id = self_mapping.onebot_id

        private_bindings = self._hot_store_bundle.state_engine.get_private_room_source_bindings()
        rebuilt_private_surrogates: dict[int, str] = {}
        for user_source_id, room_source_id in private_bindings.items():
            mapping = await registry.ensure_mapping(
                user_source_id,
                bot_id=self.config.bot_id,
            )
            rebuilt_private_surrogates[mapping.onebot_id] = room_source_id
        self._hot_store_bundle.state_engine.replace_private_room_surrogate_bindings(
            rebuilt_private_surrogates
        )

        identity_scope_path = self.data_dir / "identity_scope.json"
        await asyncio.to_thread(
            identity_scope_path.write_text,
            (
                "{\n"
                f'  "scope_key": {self._json_string(registry.scope_key)},\n'
                f'  "database_path": {self._json_string(str(registry.database_path))},\n'
                f'  "onebot_self_id": {self.config.onebot_self_id}\n'
                "}\n"
            ),
            "utf-8",
        )
        await registry.sync_warning_file(
            bot_id=self.config.bot_id,
            warning_path=warning_path,
        )
        registry.repeat_persisted_warnings()
        logger.info(
            "[RocketChatOneBotBridge] bot 用户哈希映射就绪 | "
            "userId=%s | onebot_self_id=%s | algorithm=sha256-linear-v1",
            self.rocketchat.user_id,
            self.config.onebot_self_id,
        )

    @staticmethod
    def _json_string(value: str) -> str:
        import json

        return json.dumps(str(value), ensure_ascii=False)

    async def _stop_clients(self) -> None:
        runtime_context = self._plugin_runtime_context
        runtime_plugins = list(self._runtime_plugins)
        if self.rocketchat is not None:
            await self.rocketchat.stop()
        if self.onebot is not None:
            await self.onebot.stop()
        if self._plugin_manager is not None and runtime_context is not None and runtime_plugins:
            await self._plugin_manager.shutdown_runtime_plugins(runtime_plugins, runtime_context)
        self.rocketchat = None
        self.inbound_translator = None
        self.outbound_translator = None
        self.action_handler = None
        self.onebot = None
        self._plugin_runtime_context = None
        self._runtime_plugins = []
        self.identity_registry = None
        self.identity_database_path = None
        if self._base_id_map is not None:
            self.id_map = self._base_id_map

    def _ensure_hot_store_bundle(self) -> None:
        if self._hot_store_bundle is not None:
            return
        self._hot_store_bundle = build_runtime_hot_stores(
            self.data_dir,
            message_window_size=self.message_index_max_entries,
        )
        self.message_store = self._hot_store_bundle.message_store
        self._base_id_map = self._hot_store_bundle.id_map
        self.id_map = self._base_id_map
        self.private_room_store = self._hot_store_bundle.private_room_store
        self.context_room_store = self._hot_store_bundle.context_room_store

    async def _dispatch_plugin_action(
        self,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self._plugin_manager is None or self._plugin_runtime_context is None or not self._runtime_plugins:
            return None
        return await self._plugin_manager.dispatch_onebot_action(
            self._runtime_plugins,
            action,
            params,
            self._plugin_runtime_context,
        )

    async def restart_connections(self, reason: str) -> None:
        async with self._restart_lock:
            self._reload_config_snapshot()
            errors = self.config.validate()
            if errors:
                logger.error(
                    f"[RocketChatOneBotBridge][{self.instance_name}] 配置校验失败: {'; '.join(errors)}"
                )
                await self.state_store.write({"status": "error", "error": '; '.join(errors)})
                return
            if not self.config.enabled:
                logger.info(
                    f"[RocketChatOneBotBridge][{self.instance_name}] 当前 enabled=false，跳过 bridge 重连。"
                )
                await self._stop_clients()
                await self.state_store.write({"status": "stopped", "reason": reason, "enabled": False})
                self._started = False
                return

            await self.state_store.write({"status": "restarting", "reason": reason})
            await self._stop_clients()
            await self._start_clients()
            await self.state_store.write(
                {
                    "status": "running",
                    "server_url": self.config.server_url,
                    "onebot_ws_url": self.config.onebot_ws_url,
                    "onebot_self_id": self.config.onebot_self_id,
                    "max_reconnect_attempts": self.config.max_reconnect_attempts,
                    "restart_reason": reason,
                }
            )
            logger.info(
                f"[RocketChatOneBotBridge][{self.instance_name}] bridge 连接已重启，reason={reason}"
            )

    async def _handle_reconnect_exhausted(
        self,
        client_name: str,
        attempts: int,
        error: str,
    ) -> None:
        if self._failure_handled:
            return
        self._failure_handled = True
        await self._disable_bridge_after_reconnect_failure()
        await self.state_store.write(
            {
                "status": "failed",
                "client": client_name,
                "attempts": attempts,
                "error": error,
                "enabled": False,
                "auto_disabled": True,
            }
        )
        logger.error(
            f'[RocketChatOneBotBridge][{self.instance_name}] 当前 bot 已因重连失败被自动关闭，请重新开启后再尝试连接。'
        )
        if self._failure_task is None or self._failure_task.done():
            self._failure_task = asyncio.create_task(self._enter_failed_state())

    async def _enter_failed_state(self) -> None:
        async with self._restart_lock:
            await self._stop_clients()
            self._started = False

    async def _disable_bridge_after_reconnect_failure(self) -> None:
        self.config.enabled = False
        if isinstance(self.raw_config, dict):
            self.raw_config["enabled"] = False
        elif hasattr(self.raw_config, "__setitem__"):
            self.raw_config["enabled"] = False
        if self._disable_callback is not None:
            result = self._disable_callback()
            if inspect.isawaitable(result):
                await result
            return
        if hasattr(self.raw_config, "save_config"):
            self.raw_config.save_config()

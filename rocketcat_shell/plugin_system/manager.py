from __future__ import annotations

import asyncio
import copy
import importlib.util
import inspect
import secrets
import shutil
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..layout import ProjectLayout
from ..logger import logger
from ..settings import read_json, write_json
from .base import PluginContext, PluginExecutionContext, RocketCatPlugin
from .dashboard import (
    DashboardRoute,
    DashboardSSERoute,
    normalize_dashboard_path,
)


_PLUGIN_PREFIX = "rocketcat_plugin_"
_MISSING = object()
_DASHBOARD_SESSION_TTL_SECONDS = 3600.0
_DASHBOARD_SESSION_MAX_ENTRIES = 256
_ENABLED_SCHEMA = {
    "description": "启用插件",
    "type": "bool",
    "default": True,
}


@dataclass(slots=True, frozen=True)
class DashboardPage:
    name: str
    title: str
    page_dir: Path

    def to_summary(self) -> dict[str, str]:
        return {"name": self.name, "title": self.title}


@dataclass(slots=True)
class PluginDescriptor:
    plugin_id: str
    dir_name: str
    plugin_dir: Path
    data_dir: Path
    config_path: Path
    metadata: dict[str, Any]
    schema: dict[str, Any]
    config: dict[str, Any]
    logo_path: Path | None
    main_path: Path | None
    pages: tuple[DashboardPage, ...]
    default_page: str | None
    installed_at: str | None
    activated: bool
    load_error: str | None = None

    def to_summary(self) -> dict[str, Any]:
        display_name = str(
            self.metadata.get("display_name")
            or self.metadata.get("name")
            or self.plugin_id
        ).strip()
        return {
            "id": self.plugin_id,
            "name": str(self.metadata.get("name") or self.plugin_id).strip(),
            "display_name": display_name or self.plugin_id,
            "author": str(self.metadata.get("author") or "").strip(),
            "desc": str(
                self.metadata.get("desc")
                or self.metadata.get("description")
                or ""
            ).strip(),
            "version": str(self.metadata.get("version") or "").strip(),
            "repo": str(self.metadata.get("repo") or "").strip(),
            "activated": self.activated,
            "has_logo": self.logo_path is not None,
            "has_settings": bool(self.schema),
            "runtime_available": self.main_path is not None,
            "pages": [page.to_summary() for page in self.pages],
            "has_dashboard": bool(self.pages),
            "default_page": self.default_page,
            "installed_at": self.installed_at,
            "load_error": self.load_error,
            "plugin_dir": str(self.plugin_dir),
            "config_path": str(self.config_path),
            "data_dir": str(self.data_dir),
        }

    def to_detail(self) -> dict[str, Any]:
        payload = self.to_summary()
        payload["schema"] = copy.deepcopy(self.schema)
        payload["config"] = copy.deepcopy(self.config)
        return payload


@dataclass(slots=True)
class RuntimePluginBinding:
    descriptor: PluginDescriptor
    instance: RocketCatPlugin
    handled_actions: frozenset[str]
    generation: int


@dataclass(slots=True)
class _RuntimeAttachment:
    runtime: PluginExecutionContext
    bindings: list[RuntimePluginBinding] = field(default_factory=list)


@dataclass(slots=True)
class _GlobalPluginState:
    descriptor: PluginDescriptor
    instance: RocketCatPlugin
    generation: int
    api_routes: tuple[DashboardRoute, ...]
    sse_routes: tuple[DashboardSSERoute, ...]


@dataclass(slots=True, frozen=True)
class DashboardSession:
    token: str
    plugin_id: str
    page_name: str
    generation: int
    expires_at: float


class RocketCatPluginManager:
    def __init__(self, layout: ProjectLayout):
        self.layout = layout
        self._plugins: dict[str, PluginDescriptor] = {}
        self._plugins_signature: tuple[tuple[str, int, int, bool], ...] | None = None
        self._states: dict[str, _GlobalPluginState] = {}
        self._runtime_attachments: dict[int, _RuntimeAttachment] = {}
        self._plugin_generations: dict[str, int] = {}
        self._dashboard_sessions: OrderedDict[str, DashboardSession] = OrderedDict()
        self._dashboard_sse_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.layout.ensure_directories()
        self.refresh()
        await self.reconcile_all()

    async def shutdown(self) -> None:
        async with self._lock:
            for attachment in list(self._runtime_attachments.values()):
                await self._detach_runtime_locked(attachment)
            self._runtime_attachments.clear()
            for plugin_id in list(self._states):
                await self._deactivate_state_locked(plugin_id)
            self._dashboard_sessions.clear()
            self._cancel_dashboard_sse_tasks_locked()

    def refresh(self) -> None:
        signature = self._build_plugins_signature()
        if signature == self._plugins_signature:
            return

        discovered: dict[str, PluginDescriptor] = {}
        if self.layout.plugins_dir.exists():
            for child in sorted(
                self.layout.plugins_dir.iterdir(),
                key=lambda item: item.name.lower(),
            ):
                if not child.is_dir() or not child.name.startswith(_PLUGIN_PREFIX):
                    continue
                descriptor = self._build_descriptor(child)
                discovered[descriptor.plugin_id] = descriptor
        self._plugins = discovered
        self._plugins_signature = self._build_plugins_signature()

    async def reconcile_all(self, *, force: bool = False) -> None:
        self.refresh()
        async with self._lock:
            for plugin_id in list(self._states):
                if plugin_id not in self._plugins:
                    await self._deactivate_state_locked(plugin_id)
            for plugin_id in sorted(self._plugins):
                descriptor = self._plugins[plugin_id]
                state = self._states.get(plugin_id)
                should_exist = descriptor.activated and descriptor.main_path is not None
                if not should_exist:
                    if state is not None:
                        await self._deactivate_state_locked(plugin_id)
                    continue
                if state is None or force:
                    await self._replace_plugin_locked(plugin_id, descriptor)

    def list_plugins(self) -> list[dict[str, Any]]:
        self.refresh()
        items: list[dict[str, Any]] = []
        for plugin_id in sorted(self._plugins):
            item = self._plugins[plugin_id].to_summary()
            item["global_instance_active"] = plugin_id in self._states
            item["runtime_binding_count"] = sum(
                1
                for attachment in self._runtime_attachments.values()
                for binding in attachment.bindings
                if binding.descriptor.plugin_id == plugin_id
            )
            items.append(item)
        return items

    def diagnostic_summary(self) -> dict[str, int]:
        return {
            "plugin_count": len(self._plugins),
            "global_instance_count": len(self._states),
            "runtime_attachment_count": len(self._runtime_attachments),
            "runtime_binding_count": sum(
                len(attachment.bindings)
                for attachment in self._runtime_attachments.values()
            ),
            "dashboard_session_count": len(self._dashboard_sessions),
            "dashboard_sse_count": sum(
                len(tasks) for tasks in self._dashboard_sse_tasks.values()
            ),
        }

    def get_plugin(self, plugin_id: str) -> dict[str, Any]:
        self.refresh()
        return self._require_plugin(plugin_id).to_detail()

    def get_logo_path(self, plugin_id: str) -> Path | None:
        self.refresh()
        return self._require_plugin(plugin_id).logo_path

    async def update_plugin_config(
        self,
        plugin_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not payload:
            raise ValueError("未提供可更新的插件设置")
        self.refresh()
        descriptor = self._require_plugin(plugin_id)
        merged = copy.deepcopy(descriptor.config)
        merged.update(payload)
        normalized = self._normalize_object_value(
            {"type": "object", "items": descriptor.schema},
            merged,
        )
        if not isinstance(normalized, dict):
            raise ValueError("插件配置格式无效")
        previous_config_bytes = (
            descriptor.config_path.read_bytes()
            if descriptor.config_path.exists()
            else None
        )
        write_json(descriptor.config_path, normalized)
        refreshed = self._build_descriptor(descriptor.plugin_dir)
        self._plugins[plugin_id] = refreshed
        self._invalidate_cache()
        try:
            async with self._lock:
                await self._replace_plugin_locked(plugin_id, refreshed)
        except Exception:
            if previous_config_bytes is None:
                descriptor.config_path.unlink(missing_ok=True)
            else:
                descriptor.config_path.write_bytes(previous_config_bytes)
            self._plugins[plugin_id] = descriptor
            self._invalidate_cache()
            raise
        return refreshed.to_detail()

    async def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        return await self.update_plugin_config(plugin_id, {"enabled": bool(enabled)})

    async def reload_plugin(self, plugin_id: str) -> dict[str, Any]:
        self.refresh()
        descriptor = self._require_plugin(plugin_id)
        refreshed = self._build_descriptor(descriptor.plugin_dir)
        self._plugins[plugin_id] = refreshed
        self._invalidate_cache()
        try:
            async with self._lock:
                await self._replace_plugin_locked(plugin_id, refreshed)
        except Exception:
            self._plugins[plugin_id] = descriptor
            self._invalidate_cache()
            raise
        return refreshed.to_detail()

    async def uninstall_plugin(
        self,
        plugin_id: str,
        *,
        delete_config: bool = False,
        delete_data: bool = False,
    ) -> dict[str, Any]:
        self.refresh()
        descriptor = self._require_plugin(plugin_id)
        async with self._lock:
            await self._deactivate_state_locked(plugin_id)
            self._revoke_dashboard_sessions_locked(plugin_id)

        deleted_plugin_body = False
        deleted_config = False
        deleted_data = False
        if descriptor.plugin_dir.exists():
            shutil.rmtree(descriptor.plugin_dir)
            deleted_plugin_body = True
        if delete_data and descriptor.data_dir.exists():
            shutil.rmtree(descriptor.data_dir)
            deleted_data = True
        if delete_config and descriptor.config_path.exists():
            descriptor.config_path.unlink()
            deleted_config = True
        self._plugins.pop(plugin_id, None)
        self._invalidate_cache()
        self.refresh()
        return {
            "plugin_id": plugin_id,
            "deleted_plugin_body": deleted_plugin_body,
            "deleted_config": deleted_config,
            "deleted_data": deleted_data,
        }

    async def create_runtime_plugins(
        self,
        runtime: PluginExecutionContext,
    ) -> list[RuntimePluginBinding]:
        await self.reconcile_all()
        runtime_id = id(runtime)
        async with self._lock:
            existing = self._runtime_attachments.get(runtime_id)
            if existing is not None:
                return existing.bindings

            attachment = _RuntimeAttachment(runtime=runtime)
            self._runtime_attachments[runtime_id] = attachment
            for plugin_id in sorted(self._states):
                state = self._states[plugin_id]
                try:
                    await self._call_maybe_async(state.instance.on_load(runtime))
                except Exception as exc:
                    logger.error(
                        "[RocketCatShell] 插件 %s 绑定 runtime %s 失败: %r",
                        plugin_id,
                        runtime.instance_name,
                        exc,
                    )
                    continue
                attachment.bindings.append(self._build_binding(state))
            return attachment.bindings

    async def shutdown_runtime_plugins(
        self,
        bindings: list[RuntimePluginBinding],
        runtime: PluginExecutionContext,
    ) -> None:
        del bindings
        async with self._lock:
            attachment = self._runtime_attachments.pop(id(runtime), None)
            if attachment is not None:
                await self._detach_runtime_locked(attachment)

    async def dispatch_onebot_action(
        self,
        bindings: list[RuntimePluginBinding],
        action: str,
        params: dict[str, Any],
        runtime: PluginExecutionContext,
    ) -> dict[str, Any] | None:
        normalized_action = str(action or "").strip()
        indexed_bindings: list[RuntimePluginBinding] = []
        fallback_bindings: list[RuntimePluginBinding] = []
        for binding in tuple(bindings):
            if binding.handled_actions:
                if normalized_action in binding.handled_actions:
                    indexed_bindings.append(binding)
            else:
                fallback_bindings.append(binding)

        for binding in [*indexed_bindings, *fallback_bindings]:
            try:
                result = binding.instance.handle_onebot_action(
                    normalized_action,
                    dict(params or {}),
                    runtime,
                )
                result = await self._call_maybe_async(result)
            except Exception as exc:
                logger.error(
                    "[RocketCatShell] 插件 %s 处理动作 %s 失败: %r",
                    binding.descriptor.plugin_id,
                    action,
                    exc,
                )
                continue
            if result is not None:
                return result
        return None

    async def issue_dashboard_session(
        self,
        plugin_id: str,
        page_name: str | None = None,
    ) -> DashboardSession:
        self.refresh()
        async with self._lock:
            descriptor = self._require_plugin(plugin_id)
            if not descriptor.activated:
                raise PermissionError("插件尚未启用")
            page = self._resolve_page(descriptor, page_name)
            self._prune_dashboard_sessions_locked()
            generation = self._plugin_generations.get(plugin_id, 0)
            token = secrets.token_urlsafe(32)
            session = DashboardSession(
                token=token,
                plugin_id=plugin_id,
                page_name=page.name,
                generation=generation,
                expires_at=time.monotonic() + _DASHBOARD_SESSION_TTL_SECONDS,
            )
            self._dashboard_sessions[token] = session
            while len(self._dashboard_sessions) > _DASHBOARD_SESSION_MAX_ENTRIES:
                self._dashboard_sessions.popitem(last=False)
            return session

    async def revoke_dashboard_session(self, token: str) -> None:
        async with self._lock:
            self._dashboard_sessions.pop(str(token or ""), None)

    async def register_dashboard_sse_task(
        self,
        plugin_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        async with self._lock:
            if plugin_id not in self._states:
                raise KeyError(plugin_id)
            self._dashboard_sse_tasks.setdefault(plugin_id, set()).add(task)

    async def unregister_dashboard_sse_task(
        self,
        plugin_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        async with self._lock:
            tasks = self._dashboard_sse_tasks.get(plugin_id)
            if tasks is None:
                return
            tasks.discard(task)
            if not tasks:
                self._dashboard_sse_tasks.pop(plugin_id, None)

    async def resolve_dashboard_asset(
        self,
        plugin_id: str,
        page_name: str,
        token: str,
        asset_path: str,
    ) -> tuple[PluginDescriptor, DashboardPage, Path]:
        self.refresh()
        async with self._lock:
            session = self._validate_dashboard_session_locked(
                plugin_id,
                page_name,
                token,
            )
            descriptor = self._require_plugin(session.plugin_id)
            page = self._resolve_page(descriptor, session.page_name)
            relative = normalize_dashboard_path(asset_path)
            root = page.page_dir.resolve()
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise FileNotFoundError(relative) from exc
            if not candidate.is_file():
                raise FileNotFoundError(relative)
            return descriptor, page, candidate

    async def resolve_dashboard_api(
        self,
        plugin_id: str,
        path: str,
        method: str,
    ) -> tuple[_GlobalPluginState, DashboardRoute, dict[str, str]]:
        normalized = normalize_dashboard_path(path)
        async with self._lock:
            state = self._states.get(plugin_id)
            if state is None or not state.descriptor.activated:
                raise KeyError(plugin_id)
            for route in state.api_routes:
                path_params = route.match(normalized, method)
                if path_params is not None:
                    return state, route, path_params
        raise LookupError(normalized)

    async def resolve_dashboard_sse(
        self,
        plugin_id: str,
        path: str,
    ) -> tuple[_GlobalPluginState, DashboardSSERoute, dict[str, str]]:
        normalized = normalize_dashboard_path(path)
        async with self._lock:
            state = self._states.get(plugin_id)
            if state is None or not state.descriptor.activated:
                raise KeyError(plugin_id)
            for route in state.sse_routes:
                path_params = route.match(normalized)
                if path_params is not None:
                    return state, route, path_params
        raise LookupError(normalized)

    def is_allowed_dashboard_file(self, plugin_id: str, path: Path) -> bool:
        descriptor = self._require_plugin(plugin_id)
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return False
        for root in (descriptor.plugin_dir, descriptor.data_dir):
            try:
                resolved.relative_to(root.resolve())
                return resolved.is_file()
            except (OSError, ValueError):
                continue
        return False

    async def _replace_plugin_locked(
        self,
        plugin_id: str,
        descriptor: PluginDescriptor,
    ) -> None:
        old_state = self._states.get(plugin_id)
        next_generation = self._plugin_generations.get(plugin_id, 0) + 1

        if not descriptor.activated or descriptor.main_path is None:
            if old_state is not None:
                await self._deactivate_state_locked(plugin_id)
            else:
                self._plugin_generations[plugin_id] = next_generation
                self._revoke_dashboard_sessions_locked(plugin_id)
            return

        candidate = await self._instantiate_plugin(descriptor, next_generation)
        loaded_attachments: list[_RuntimeAttachment] = []
        try:
            for attachment in self._runtime_attachments.values():
                await self._call_maybe_async(
                    candidate.instance.on_load(attachment.runtime)
                )
                loaded_attachments.append(attachment)
        except Exception:
            for attachment in reversed(loaded_attachments):
                try:
                    await self._call_maybe_async(
                        candidate.instance.on_unload(attachment.runtime)
                    )
                except Exception:
                    logger.exception(
                        "[RocketCatShell] 回滚候选插件 %s 的 runtime 绑定失败",
                        plugin_id,
                    )
            await self._safe_terminate(candidate)
            raise

        old_bindings: list[tuple[_RuntimeAttachment, RuntimePluginBinding]] = []
        for attachment in self._runtime_attachments.values():
            old_binding = next(
                (
                    binding
                    for binding in attachment.bindings
                    if binding.descriptor.plugin_id == plugin_id
                ),
                None,
            )
            if old_binding is None:
                attachment.bindings.append(self._build_binding(candidate))
            else:
                old_bindings.append((attachment, old_binding))
                old_binding.descriptor = descriptor
                old_binding.instance = candidate.instance
                old_binding.handled_actions = self._get_handled_actions(
                    candidate.instance
                )
                old_binding.generation = next_generation

        self._plugin_generations[plugin_id] = next_generation
        self._revoke_dashboard_sessions_locked(plugin_id)
        self._states[plugin_id] = candidate
        if old_state is not None:
            for attachment, _binding in reversed(old_bindings):
                try:
                    await self._call_maybe_async(
                        old_state.instance.on_unload(attachment.runtime)
                    )
                except Exception as exc:
                    logger.warning(
                        "[RocketCatShell] 卸载插件 %s 的旧 runtime binding 时异常: %r",
                        plugin_id,
                        exc,
                    )
            await self._safe_terminate(old_state)

    async def _deactivate_state_locked(self, plugin_id: str) -> None:
        state = self._states.pop(plugin_id, None)
        self._plugin_generations[plugin_id] = self._plugin_generations.get(plugin_id, 0) + 1
        self._revoke_dashboard_sessions_locked(plugin_id)
        if state is None:
            return

        for attachment in self._runtime_attachments.values():
            binding = next(
                (
                    item
                    for item in attachment.bindings
                    if item.descriptor.plugin_id == plugin_id
                ),
                None,
            )
            if binding is None:
                continue
            attachment.bindings.remove(binding)
            try:
                await self._call_maybe_async(
                    state.instance.on_unload(attachment.runtime)
                )
            except Exception as exc:
                logger.warning(
                    "[RocketCatShell] 停止插件 %s 时出现异常: %r",
                    plugin_id,
                    exc,
                )
        await self._safe_terminate(state)

    async def _detach_runtime_locked(self, attachment: _RuntimeAttachment) -> None:
        for binding in reversed(tuple(attachment.bindings)):
            try:
                await self._call_maybe_async(
                    binding.instance.on_unload(attachment.runtime)
                )
            except Exception as exc:
                logger.warning(
                    "[RocketCatShell] 插件 %s 解绑 runtime %s 时异常: %r",
                    binding.descriptor.plugin_id,
                    attachment.runtime.instance_name,
                    exc,
                )
        attachment.bindings.clear()

    async def _instantiate_plugin(
        self,
        descriptor: PluginDescriptor,
        generation: int,
    ) -> _GlobalPluginState:
        if descriptor.main_path is None:
            raise RuntimeError("插件缺少 main.py")

        module_name = (
            f"rocketcat_global_plugins_{descriptor.plugin_id}_{generation}_{time.time_ns()}"
        )
        spec = importlib.util.spec_from_file_location(module_name, descriptor.main_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载插件模块: {descriptor.main_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin_cls = self._resolve_plugin_class(module)

        api_routes: list[DashboardRoute] = []
        sse_routes: list[DashboardSSERoute] = []

        def register_api(route: DashboardRoute) -> None:
            if any(
                existing.path == route.path
                and existing.methods.intersection(route.methods)
                for existing in api_routes
            ):
                raise ValueError(f"Dashboard API 重复注册: {route.path}")
            api_routes.append(route)

        def register_sse(route: DashboardSSERoute) -> None:
            if any(existing.path == route.path for existing in sse_routes):
                raise ValueError(f"Dashboard SSE 重复注册: {route.path}")
            sse_routes.append(route)

        context = PluginContext(
            plugin_id=descriptor.plugin_id,
            plugin_dir=descriptor.plugin_dir,
            data_dir=descriptor.data_dir,
            config_path=descriptor.config_path,
            metadata=copy.deepcopy(descriptor.metadata),
            _dashboard_api_registrar=register_api,
            _dashboard_sse_registrar=register_sse,
        )
        instance = plugin_cls(context, copy.deepcopy(descriptor.config))
        if not isinstance(instance, RocketCatPlugin):
            raise TypeError(
                f"插件 {descriptor.plugin_id} 未返回 RocketCatPlugin 实例"
            )
        await self._call_maybe_async(instance.on_initialize())
        return _GlobalPluginState(
            descriptor=descriptor,
            instance=instance,
            generation=generation,
            api_routes=tuple(api_routes),
            sse_routes=tuple(sse_routes),
        )

    async def _safe_terminate(self, state: _GlobalPluginState) -> None:
        try:
            await self._call_maybe_async(state.instance.on_terminate())
        except Exception as exc:
            logger.warning(
                "[RocketCatShell] 终止插件 %s 时出现异常: %r",
                state.descriptor.plugin_id,
                exc,
            )

    def _build_binding(self, state: _GlobalPluginState) -> RuntimePluginBinding:
        return RuntimePluginBinding(
            descriptor=state.descriptor,
            instance=state.instance,
            handled_actions=self._get_handled_actions(state.instance),
            generation=state.generation,
        )

    def _resolve_plugin_class(self, module: Any) -> type[RocketCatPlugin]:
        candidate = getattr(module, "Plugin", None)
        if inspect.isclass(candidate) and issubclass(candidate, RocketCatPlugin):
            return candidate
        for value in vars(module).values():
            if (
                inspect.isclass(value)
                and issubclass(value, RocketCatPlugin)
                and value is not RocketCatPlugin
            ):
                return value
        raise RuntimeError("插件 main.py 中未找到 Plugin 类")

    def _get_handled_actions(self, instance: RocketCatPlugin) -> frozenset[str]:
        try:
            return instance.get_handled_actions()
        except Exception as exc:
            logger.warning(
                "[RocketCatShell] 读取插件 %s action 索引失败: %r",
                instance.context.plugin_id,
                exc,
            )
            return frozenset()

    def _build_descriptor(self, plugin_dir: Path) -> PluginDescriptor:
        plugin_id = plugin_dir.name
        config_path = self.layout.plugins_config_dir / f"{plugin_id}_config.json"
        data_dir = self.layout.plugin_data_dir / plugin_id
        metadata = self._read_metadata(plugin_dir / "metadata.yaml")
        schema, schema_error = self._read_schema(plugin_dir / "_conf_schema.json")
        raw_config = read_json(config_path, {})
        normalized_config = self._normalize_object_value(
            {"type": "object", "items": schema},
            raw_config,
        )
        if not isinstance(normalized_config, dict):
            normalized_config = {"enabled": True}
        if raw_config != normalized_config:
            write_json(config_path, normalized_config)
            self._plugins_signature = None

        pages = self._discover_pages(plugin_dir)
        requested_default = str(metadata.get("dashboard_page") or "").strip()
        page_names = {page.name for page in pages}
        if requested_default in page_names:
            default_page = requested_default
        elif "dashboard" in page_names:
            default_page = "dashboard"
        else:
            default_page = pages[0].name if pages else None

        logo_path = plugin_dir / "logo.png"
        main_path = plugin_dir / "main.py"
        return PluginDescriptor(
            plugin_id=plugin_id,
            dir_name=plugin_id,
            plugin_dir=plugin_dir,
            data_dir=data_dir,
            config_path=config_path,
            metadata=metadata,
            schema=schema,
            config=normalized_config,
            logo_path=logo_path if logo_path.exists() else None,
            main_path=main_path if main_path.exists() else None,
            pages=pages,
            default_page=default_page,
            installed_at=self._get_installed_at(plugin_dir),
            activated=bool(normalized_config.get("enabled", True)),
            load_error=schema_error,
        )

    def _discover_pages(self, plugin_dir: Path) -> tuple[DashboardPage, ...]:
        pages_root = plugin_dir / "pages"
        if not pages_root.is_dir():
            return ()
        pages: list[DashboardPage] = []
        for child in sorted(pages_root.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir():
                continue
            try:
                page_name = normalize_dashboard_path(child.name)
            except ValueError:
                continue
            if "/" in page_name or not (child / "index.html").is_file():
                continue
            pages.append(
                DashboardPage(
                    name=page_name,
                    title=page_name.replace("_", " ").replace("-", " ").title(),
                    page_dir=child,
                )
            )
        return tuple(pages)

    def _resolve_page(
        self,
        descriptor: PluginDescriptor,
        page_name: str | None,
    ) -> DashboardPage:
        requested = str(page_name or descriptor.default_page or "").strip()
        for page in descriptor.pages:
            if page.name == requested:
                return page
        raise KeyError(requested)

    def _require_plugin(self, plugin_id: str) -> PluginDescriptor:
        descriptor = self._plugins.get(plugin_id)
        if descriptor is None:
            raise KeyError(plugin_id)
        return descriptor

    def _invalidate_cache(self) -> None:
        self._plugins_signature = None

    def _build_plugins_signature(self) -> tuple[tuple[str, int, int, bool], ...]:
        paths: list[Path] = [self.layout.plugins_dir, self.layout.plugins_config_dir]
        if self.layout.plugins_dir.exists():
            try:
                for child in sorted(
                    self.layout.plugins_dir.iterdir(),
                    key=lambda item: item.name.lower(),
                ):
                    if not child.is_dir() or not child.name.startswith(_PLUGIN_PREFIX):
                        continue
                    paths.extend(
                        [
                            child,
                            child / "metadata.yaml",
                            child / "_conf_schema.json",
                            child / "main.py",
                            child / "logo.png",
                            child / "pages",
                            self.layout.plugins_config_dir
                            / f"{child.name}_config.json",
                        ]
                    )
                    pages_root = child / "pages"
                    if pages_root.is_dir():
                        for page_path in pages_root.rglob("*"):
                            paths.append(page_path)
            except OSError:
                pass

        signature: list[tuple[str, int, int, bool]] = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                signature.append((str(path), 0, 0, False))
                continue
            signature.append(
                (str(path), int(stat.st_mtime_ns), int(stat.st_size), path.is_dir())
            )
        return tuple(signature)

    def _read_metadata(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        payload: dict[str, Any] = {}
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                stripped = raw_line.strip()
                if not stripped or stripped.startswith("#") or ":" not in stripped:
                    continue
                key, _, value = stripped.partition(":")
                normalized = value.strip()
                if (
                    len(normalized) >= 2
                    and normalized[0] == normalized[-1]
                    and normalized[0] in {'"', "'"}
                ):
                    normalized = normalized[1:-1]
                payload[key.strip()] = normalized
        except OSError as exc:
            logger.warning(
                "[RocketCatShell] 读取插件 metadata 失败 %s: %r",
                path,
                exc,
            )
            return {}
        return payload

    def _read_schema(self, path: Path) -> tuple[dict[str, Any], str | None]:
        schema_error: str | None = None
        raw_schema = read_json(path, {}) if path.exists() else {}
        if not isinstance(raw_schema, dict):
            raw_schema = {}
            schema_error = "_conf_schema.json 不是合法对象，已回退为空配置"
        return self._with_enabled_schema(raw_schema), schema_error

    def _with_enabled_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        ordered: dict[str, Any] = {}
        if "enabled" in schema and isinstance(schema["enabled"], dict):
            ordered["enabled"] = dict(schema["enabled"])
        else:
            ordered["enabled"] = dict(_ENABLED_SCHEMA)
        for key, value in schema.items():
            if key != "enabled" and isinstance(value, dict):
                ordered[key] = value
        return ordered

    def _normalize_object_value(self, schema: dict[str, Any], value: Any) -> Any:
        schema_type = str(schema.get("type") or "").strip().lower()
        if schema_type == "object":
            items = schema.get("items") if isinstance(schema.get("items"), dict) else {}
            source = value if isinstance(value, dict) else {}
            return {
                key: self._normalize_value(item_schema, source.get(key, _MISSING), key)
                for key, item_schema in items.items()
            }
        return self._normalize_value(schema, value, None)

    def _normalize_value(
        self,
        schema: dict[str, Any],
        value: Any,
        field_name: str | None,
    ) -> Any:
        schema_type = str(schema.get("type") or "string").strip().lower()
        if value is _MISSING:
            return self._default_value(schema)
        if schema_type in {"string", "text"}:
            return str(value or "")
        if schema_type == "bool":
            return self._coerce_bool(value)
        if schema_type == "int":
            try:
                return int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field_name or '配置项'} 必须是整数") from exc
        if schema_type == "float":
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field_name or '配置项'} 必须是数字") from exc
        if schema_type in {"list", "template_list"}:
            if not isinstance(value, list):
                raise ValueError(f"{field_name or '配置项'} 必须是列表")
            return copy.deepcopy(value)
        if schema_type == "dict":
            if not isinstance(value, dict):
                raise ValueError(f"{field_name or '配置项'} 必须是对象")
            return copy.deepcopy(value)
        if schema_type == "object":
            return self._normalize_object_value(
                schema,
                value if isinstance(value, dict) else {},
            )
        return copy.deepcopy(value)

    def _default_value(self, schema: dict[str, Any]) -> Any:
        if "default" in schema:
            return copy.deepcopy(schema["default"])
        schema_type = str(schema.get("type") or "string").strip().lower()
        if schema_type in {"string", "text"}:
            return ""
        if schema_type == "bool":
            return False
        if schema_type == "int":
            return 0
        if schema_type == "float":
            return 0.0
        if schema_type in {"list", "template_list"}:
            return []
        if schema_type == "dict":
            return {}
        if schema_type == "object":
            items = schema.get("items") if isinstance(schema.get("items"), dict) else {}
            return {
                key: self._default_value(item_schema)
                for key, item_schema in items.items()
            }
        return None

    def _coerce_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
        return bool(value)

    def _get_installed_at(self, plugin_dir: Path) -> str | None:
        try:
            return datetime.fromtimestamp(
                plugin_dir.stat().st_mtime,
                timezone.utc,
            ).isoformat()
        except OSError:
            return None

    def _prune_dashboard_sessions_locked(self) -> None:
        now = time.monotonic()
        expired = [
            token
            for token, session in self._dashboard_sessions.items()
            if session.expires_at <= now
        ]
        for token in expired:
            self._dashboard_sessions.pop(token, None)

    def _revoke_dashboard_sessions_locked(self, plugin_id: str) -> None:
        for token in [
            token
            for token, session in self._dashboard_sessions.items()
            if session.plugin_id == plugin_id
        ]:
            self._dashboard_sessions.pop(token, None)
        self._cancel_dashboard_sse_tasks_locked(plugin_id)

    def _cancel_dashboard_sse_tasks_locked(self, plugin_id: str | None = None) -> None:
        plugin_ids = (
            [plugin_id]
            if plugin_id is not None
            else list(self._dashboard_sse_tasks)
        )
        current = asyncio.current_task()
        for target_plugin_id in plugin_ids:
            tasks = self._dashboard_sse_tasks.pop(target_plugin_id, set())
            for task in tasks:
                if task is not current and not task.done():
                    task.cancel()

    def _validate_dashboard_session_locked(
        self,
        plugin_id: str,
        page_name: str,
        token: str,
    ) -> DashboardSession:
        self._prune_dashboard_sessions_locked()
        session = self._dashboard_sessions.get(str(token or ""))
        if session is None:
            raise FileNotFoundError("dashboard session")
        expected_generation = self._plugin_generations.get(plugin_id, 0)
        if (
            session.plugin_id != plugin_id
            or session.page_name != page_name
            or session.generation != expected_generation
        ):
            self._dashboard_sessions.pop(session.token, None)
            raise FileNotFoundError("dashboard session")
        return session

    async def _call_maybe_async(self, result: Any) -> Any:
        if inspect.isawaitable(result):
            return await result
        return result

from __future__ import annotations

import copy
import importlib.util
import inspect
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..layout import ProjectLayout
from ..logger import logger
from ..settings import read_json, write_json
from .base import PluginContext, PluginExecutionContext, RocketCatPlugin


_PLUGIN_PREFIX = "rocketcat_plugin_"
_MISSING = object()
_ENABLED_SCHEMA = {
    "description": "启用插件",
    "type": "bool",
    "default": True,
}


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


class RocketCatPluginManager:
    def __init__(self, layout: ProjectLayout):
        self.layout = layout
        self._plugins: dict[str, PluginDescriptor] = {}

    async def initialize(self) -> None:
        self.layout.ensure_directories()
        self.refresh()

    def refresh(self) -> None:
        discovered: dict[str, PluginDescriptor] = {}
        if not self.layout.plugins_dir.exists():
            self._plugins = discovered
            return

        for child in sorted(self.layout.plugins_dir.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir() or not child.name.startswith(_PLUGIN_PREFIX):
                continue
            descriptor = self._build_descriptor(child)
            discovered[descriptor.plugin_id] = descriptor
        self._plugins = discovered

    def list_plugins(self) -> list[dict[str, Any]]:
        self.refresh()
        return [self._plugins[key].to_summary() for key in sorted(self._plugins)]

    def get_plugin(self, plugin_id: str) -> dict[str, Any]:
        self.refresh()
        return self._require_plugin(plugin_id).to_detail()

    def get_logo_path(self, plugin_id: str) -> Path | None:
        self.refresh()
        descriptor = self._require_plugin(plugin_id)
        return descriptor.logo_path

    def update_plugin_config(self, plugin_id: str, payload: dict[str, Any]) -> dict[str, Any]:
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
        write_json(descriptor.config_path, normalized)
        refreshed = self._build_descriptor(descriptor.plugin_dir)
        self._plugins[plugin_id] = refreshed
        return refreshed.to_detail()

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        return self.update_plugin_config(plugin_id, {"enabled": bool(enabled)})

    def reload_plugin(self, plugin_id: str) -> dict[str, Any]:
        self.refresh()
        descriptor = self._require_plugin(plugin_id)
        refreshed = self._build_descriptor(descriptor.plugin_dir)
        self._plugins[plugin_id] = refreshed
        return refreshed.to_detail()

    def uninstall_plugin(
        self,
        plugin_id: str,
        *,
        delete_config: bool = False,
        delete_data: bool = False,
    ) -> dict[str, Any]:
        self.refresh()
        descriptor = self._require_plugin(plugin_id)
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
        self.refresh()
        bindings: list[RuntimePluginBinding] = []
        for plugin_id in sorted(self._plugins):
            descriptor = self._plugins[plugin_id]
            if not descriptor.activated or descriptor.main_path is None:
                continue
            try:
                instance = await self._instantiate_plugin(descriptor, runtime)
            except Exception as exc:
                logger.error(
                    "[RocketCatShell] 加载插件 %s 失败: %r",
                    descriptor.plugin_id,
                    exc,
                )
                continue
            bindings.append(RuntimePluginBinding(descriptor=descriptor, instance=instance))
        return bindings

    async def shutdown_runtime_plugins(
        self,
        bindings: list[RuntimePluginBinding],
        runtime: PluginExecutionContext,
    ) -> None:
        for binding in reversed(bindings):
            try:
                await self._call_maybe_async(binding.instance.on_unload(runtime))
            except Exception as exc:
                logger.warning(
                    "[RocketCatShell] 停止插件 %s 时出现异常: %r",
                    binding.descriptor.plugin_id,
                    exc,
                )

    async def dispatch_onebot_action(
        self,
        bindings: list[RuntimePluginBinding],
        action: str,
        params: dict[str, Any],
        runtime: PluginExecutionContext,
    ) -> dict[str, Any] | None:
        for binding in bindings:
            try:
                result = binding.instance.handle_onebot_action(action, dict(params or {}), runtime)
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

    async def _instantiate_plugin(
        self,
        descriptor: PluginDescriptor,
        runtime: PluginExecutionContext,
    ) -> RocketCatPlugin:
        if descriptor.main_path is None:
            raise RuntimeError("插件缺少 main.py")

        module_name = f"rocketcat_runtime_plugins_{descriptor.plugin_id}_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(module_name, descriptor.main_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载插件模块: {descriptor.main_path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        plugin_cls = self._resolve_plugin_class(module)
        context = PluginContext(
            plugin_id=descriptor.plugin_id,
            plugin_dir=descriptor.plugin_dir,
            data_dir=descriptor.data_dir,
            config_path=descriptor.config_path,
            metadata=copy.deepcopy(descriptor.metadata),
        )
        instance = plugin_cls(context, copy.deepcopy(descriptor.config))
        if not isinstance(instance, RocketCatPlugin):
            raise TypeError(f"插件 {descriptor.plugin_id} 未返回 RocketCatPlugin 实例")
        await self._call_maybe_async(instance.on_load(runtime))
        return instance

    def _resolve_plugin_class(self, module: Any) -> type[RocketCatPlugin]:
        candidate = getattr(module, "Plugin", None)
        if inspect.isclass(candidate) and issubclass(candidate, RocketCatPlugin):
            return candidate

        for value in vars(module).values():
            if inspect.isclass(value) and issubclass(value, RocketCatPlugin) and value is not RocketCatPlugin:
                return value
        raise RuntimeError("插件 main.py 中未找到 Plugin 类")

    def _build_descriptor(self, plugin_dir: Path) -> PluginDescriptor:
        plugin_id = plugin_dir.name
        config_path = self.layout.plugins_config_dir / f"{plugin_id}_config.json"
        data_dir = self.layout.plugin_data_dir / plugin_id
        metadata = self._read_metadata(plugin_dir / "metadata.yaml")
        schema, schema_error = self._read_schema(plugin_dir / "_conf_schema.json")
        raw_config = read_json(config_path, {})
        normalized_config = self._normalize_object_value({"type": "object", "items": schema}, raw_config)
        if not isinstance(normalized_config, dict):
            normalized_config = {"enabled": True}
        if raw_config != normalized_config:
            write_json(config_path, normalized_config)

        installed_at = self._get_installed_at(plugin_dir)
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
            installed_at=installed_at,
            activated=bool(normalized_config.get("enabled", True)),
            load_error=schema_error,
        )

    def _require_plugin(self, plugin_id: str) -> PluginDescriptor:
        descriptor = self._plugins.get(plugin_id)
        if descriptor is None:
            raise KeyError(plugin_id)
        return descriptor

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
            logger.warning("[RocketCatShell] 读取插件 metadata 失败 %s: %r", path, exc)
            return {}
        return payload

    def _read_schema(self, path: Path) -> tuple[dict[str, Any], str | None]:
        schema_error: str | None = None
        raw_schema = read_json(path, {}) if path.exists() else {}
        if not isinstance(raw_schema, dict):
            raw_schema = {}
            schema_error = "_conf_schema.json 不是合法对象，已回退为空配置"
        schema = self._with_enabled_schema(raw_schema)
        return schema, schema_error

    def _with_enabled_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        ordered: dict[str, Any] = {}
        if "enabled" in schema and isinstance(schema["enabled"], dict):
            ordered["enabled"] = dict(schema["enabled"])
        else:
            ordered["enabled"] = dict(_ENABLED_SCHEMA)
        for key, value in schema.items():
            if key == "enabled":
                continue
            if isinstance(value, dict):
                ordered[key] = value
        return ordered

    def _normalize_object_value(self, schema: dict[str, Any], value: Any) -> Any:
        schema_type = str(schema.get("type") or "").strip().lower()
        if schema_type == "object":
            items = schema.get("items") if isinstance(schema.get("items"), dict) else {}
            source = value if isinstance(value, dict) else {}
            normalized: dict[str, Any] = {}
            for key, item_schema in items.items():
                normalized[key] = self._normalize_value(item_schema, source.get(key, _MISSING), key)
            return normalized
        return self._normalize_value(schema, value, None)

    def _normalize_value(self, schema: dict[str, Any], value: Any, field_name: str | None) -> Any:
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
                label = field_name or "配置项"
                raise ValueError(f"{label} 必须是整数") from exc
        if schema_type == "float":
            try:
                return float(value)
            except (TypeError, ValueError) as exc:
                label = field_name or "配置项"
                raise ValueError(f"{label} 必须是数字") from exc
        if schema_type in {"list", "template_list"}:
            if not isinstance(value, list):
                label = field_name or "配置项"
                raise ValueError(f"{label} 必须是列表")
            return copy.deepcopy(value)
        if schema_type == "dict":
            if not isinstance(value, dict):
                label = field_name or "配置项"
                raise ValueError(f"{label} 必须是对象")
            return copy.deepcopy(value)
        if schema_type == "object":
            if not isinstance(value, dict):
                value = {}
            return self._normalize_object_value(schema, value)
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
            return datetime.fromtimestamp(plugin_dir.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            return None

    async def _call_maybe_async(self, result: Any) -> Any:
        if inspect.isawaitable(result):
            return await result
        return result
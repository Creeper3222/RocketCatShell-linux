from __future__ import annotations

import ipaddress
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from urllib.parse import urlparse


TransportFactory = Callable[[Any, Any], Any]
LegacySettingsReconciler = Callable[
    [Mapping[str, Any], Mapping[str, Any], Any, bool],
    Mapping[str, Any],
]
_HOST_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


@dataclass(frozen=True, slots=True)
class TransportFieldSpec:
    key: str
    label: str
    kind: str
    default: Any
    required: bool = False
    minimum: float | int | None = None
    maximum: float | int | None = None
    step: float | int | None = None
    options: tuple[tuple[str, str], ...] = ()
    help_text: str = ""
    span: int = 1
    schemes: tuple[str, ...] = ()
    shell_default: str = ""
    invert_shell_default: bool = False
    random_token_on_create: bool = False

    def to_public_mapping(self, *, default: Any = None) -> dict[str, Any]:
        resolved_default = self.default if default is None else default
        payload: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "type": self.kind,
            "default": resolved_default,
            "required": self.required,
            "span": self.span,
        }
        if self.minimum is not None:
            payload["min"] = self.minimum
        if self.maximum is not None:
            payload["max"] = self.maximum
        if self.step is not None:
            payload["step"] = self.step
        if self.options:
            payload["options"] = [
                {"value": value, "label": label} for value, label in self.options
            ]
        if self.help_text:
            payload["help"] = self.help_text
        if self.schemes:
            payload["schemes"] = list(self.schemes)
        return payload


@dataclass(frozen=True, slots=True)
class TransportCardField:
    key: str
    label: str
    format: str = "text"

    def to_public_mapping(self) -> dict[str, str]:
        return {"key": self.key, "label": self.label, "format": self.format}


@dataclass(frozen=True, slots=True)
class TransportSpec:
    type_id: str
    label: str
    description: str
    fields: tuple[TransportFieldSpec, ...]
    card_fields: tuple[TransportCardField, ...]
    factory: TransportFactory
    server: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)
    legacy_settings_reconciler: LegacySettingsReconciler | None = None

    def defaults(
        self,
        *,
        shell_defaults: Any,
        for_create: bool,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field_spec in self.fields:
            value = field_spec.default
            if field_spec.shell_default:
                value = getattr(shell_defaults, field_spec.shell_default, value)
                if field_spec.invert_shell_default:
                    value = not bool(value)
            if for_create and field_spec.random_token_on_create:
                value = secrets.token_hex(8)
            values[field_spec.key] = value
        return values

    def to_public_mapping(self, defaults: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "type": self.type_id,
            "label": self.label,
            "description": self.description,
            "server": self.server,
            "defaults": dict(defaults),
            "fields": [
                item.to_public_mapping(default=defaults.get(item.key))
                for item in self.fields
            ],
            "card_fields": [item.to_public_mapping() for item in self.card_fields],
        }


class TransportValidationError(ValueError):
    pass


def coerce_transport_settings(
    spec: TransportSpec,
    raw_settings: Mapping[str, Any] | None,
    *,
    defaults: Mapping[str, Any],
) -> dict[str, Any]:
    if raw_settings is None:
        source: dict[str, Any] = {}
    elif isinstance(raw_settings, Mapping):
        source = dict(raw_settings)
    else:
        raise TransportValidationError(f"{spec.label} 的 settings 必须是对象")

    allowed = {item.key for item in spec.fields}
    unknown = sorted(str(key) for key in source if str(key) not in allowed)
    if unknown:
        raise TransportValidationError(
            f"{spec.label} 包含未知设置项：{', '.join(unknown)}"
        )

    normalized: dict[str, Any] = {}
    errors: list[str] = []
    for item in spec.fields:
        value = source[item.key] if item.key in source else defaults.get(item.key, item.default)
        try:
            normalized[item.key] = _coerce_field(item, value)
        except TransportValidationError as exc:
            errors.append(str(exc))

    if errors:
        raise TransportValidationError("；".join(errors))
    return normalized


def _coerce_field(field_spec: TransportFieldSpec, value: Any) -> Any:
    label = field_spec.label
    if field_spec.kind in {"text", "url", "password"}:
        normalized = str(value or "").strip()
        if field_spec.required and not normalized:
            raise TransportValidationError(f"{label}不能为空")
        if field_spec.kind == "url" and normalized:
            parsed = urlparse(normalized)
            allowed_schemes = set(field_spec.schemes or ("http", "https"))
            if parsed.scheme.lower() not in allowed_schemes or not parsed.netloc:
                expected = " 或 ".join(f"{scheme}://" for scheme in field_spec.schemes)
                raise TransportValidationError(f"{label}必须是有效的 {expected} 地址")
        if field_spec.key == "host" and normalized:
            normalized = _normalize_host(normalized, label=label)
        return normalized

    if field_spec.kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off", ""}:
                return False
        raise TransportValidationError(f"{label}必须是布尔值")

    if field_spec.kind == "number":
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise TransportValidationError(f"{label}必须是整数") from exc
        if field_spec.minimum is not None and number < field_spec.minimum:
            raise TransportValidationError(f"{label}不能小于 {field_spec.minimum:g}")
        if field_spec.maximum is not None and number > field_spec.maximum:
            raise TransportValidationError(f"{label}不能大于 {field_spec.maximum:g}")
        return number

    if field_spec.kind == "select":
        normalized = str(value or "").strip().lower()
        allowed = {option[0] for option in field_spec.options}
        if normalized not in allowed:
            raise TransportValidationError(
                f"{label}必须是 {', '.join(sorted(allowed))} 之一"
            )
        return normalized

    raise TransportValidationError(f"{label}使用了未知字段类型 {field_spec.kind}")


def _normalize_host(value: str, *, label: str) -> str:
    normalized = str(value or "").strip()
    if normalized == "*":
        return "0.0.0.0"
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1].strip()
    if not normalized or any(character.isspace() for character in normalized):
        raise TransportValidationError(f"{label}格式无效")
    try:
        ipaddress.ip_address(normalized)
        return normalized
    except ValueError:
        pass
    if ":" in normalized or "/" in normalized or "\\" in normalized:
        raise TransportValidationError(f"{label}格式无效")
    try:
        ascii_host = normalized.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise TransportValidationError(f"{label}格式无效") from exc
    canonical_host = ascii_host.rstrip(".").lower()
    if not canonical_host or len(canonical_host) > 253:
        raise TransportValidationError(f"{label}格式无效")
    labels = canonical_host.split(".")
    if not labels or any(not _HOST_LABEL_PATTERN.fullmatch(item) for item in labels):
        raise TransportValidationError(f"{label}格式无效")
    return canonical_host

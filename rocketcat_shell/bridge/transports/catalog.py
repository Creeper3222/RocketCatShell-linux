from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache
from typing import Any, Mapping

from .spec import TransportSpec, TransportValidationError, coerce_transport_settings


@lru_cache(maxsize=1)
def _specs() -> OrderedDict[str, TransportSpec]:
    from .http_client import SPEC as HTTP_CLIENT_SPEC
    from .http_server import SPEC as HTTP_SERVER_SPEC
    from .http_sse_server import SPEC as HTTP_SSE_SERVER_SPEC
    from .websocket_client import SPEC as WEBSOCKET_CLIENT_SPEC
    from .websocket_server import SPEC as WEBSOCKET_SERVER_SPEC

    ordered = (
        HTTP_SERVER_SPEC,
        HTTP_CLIENT_SPEC,
        HTTP_SSE_SERVER_SPEC,
        WEBSOCKET_SERVER_SPEC,
        WEBSOCKET_CLIENT_SPEC,
    )
    return OrderedDict((spec.type_id, spec) for spec in ordered)


def list_transport_specs() -> tuple[TransportSpec, ...]:
    return tuple(_specs().values())


def get_transport_spec(type_id: str) -> TransportSpec:
    normalized = str(type_id or "").strip().lower()
    registry = _specs()
    if normalized in registry:
        return registry[normalized]
    for spec in registry.values():
        if normalized == spec.label.lower() or normalized in spec.aliases:
            return spec
    raise TransportValidationError(f"未知 OneBot 网络类型：{type_id or '-'}")


def build_transport_defaults(
    spec: TransportSpec,
    *,
    shell_defaults: Any,
    for_create: bool,
) -> dict[str, Any]:
    return spec.defaults(
        shell_defaults=shell_defaults,
        for_create=for_create,
    )


def normalize_transport(
    payload: Mapping[str, Any] | None,
    *,
    shell_defaults: Any,
    for_create: bool = False,
    legacy_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    source = dict(payload or {})
    legacy = dict(legacy_payload or {})
    has_explicit_transport = bool(source)
    if not source:
        source = {"type": "websocket-client", "settings": {}}
    type_id = str(source.get("type") or "websocket-client").strip().lower()
    spec = get_transport_spec(type_id)
    defaults = build_transport_defaults(
        spec,
        shell_defaults=shell_defaults,
        for_create=for_create,
    )
    settings = source.get("settings")
    if (
        spec.legacy_settings_reconciler is not None
        and legacy
        and (settings is None or isinstance(settings, Mapping))
    ):
        settings = spec.legacy_settings_reconciler(
            dict(settings or {}),
            legacy,
            shell_defaults,
            has_explicit_transport,
        )
    normalized_settings = coerce_transport_settings(
        spec,
        settings if isinstance(settings, Mapping) else settings,
        defaults=defaults,
    )
    return {"type": spec.type_id, "settings": normalized_settings}


def transport_catalog(shell_defaults: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for spec in list_transport_specs():
        defaults = build_transport_defaults(
            spec,
            shell_defaults=shell_defaults,
            for_create=True,
        )
        result.append(spec.to_public_mapping(defaults))
    return result


def create_transport(config: Any, action_handler: Any) -> Any:
    spec = get_transport_spec(getattr(config, "transport_type", "websocket-client"))
    return spec.factory(config, action_handler)

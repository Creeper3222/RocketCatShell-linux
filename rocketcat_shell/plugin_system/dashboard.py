from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterable, Awaitable, Callable


DashboardHandler = Callable[["DashboardRequest"], Any | Awaitable[Any]]
DashboardSSEHandler = Callable[
    ["DashboardRequest"],
    AsyncIterable[Any] | Awaitable[AsyncIterable[Any]],
]

_ROUTE_PARAMETER = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def normalize_dashboard_path(path: str, *, allow_empty: bool = False) -> str:
    normalized = str(path or "").strip().replace("\\", "/").strip("/")
    if not normalized and allow_empty:
        return ""
    if (
        not normalized
        or normalized.startswith(".")
        or "://" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise ValueError(f"无效的 Dashboard 相对路径: {path!r}")
    return normalized


@dataclass(slots=True, frozen=True)
class DashboardUpload:
    field_name: str
    filename: str
    content_type: str
    data: bytes


@dataclass(slots=True)
class DashboardRequest:
    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    path_params: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    json_value: Any = None
    form: dict[str, list[str]] = field(default_factory=dict)
    files: dict[str, list[DashboardUpload]] = field(default_factory=dict)

    @property
    def json(self) -> Any:
        return self.json_value

    def query_value(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(str(name), [])
        return values[-1] if values else default

    def form_value(self, name: str, default: str | None = None) -> str | None:
        values = self.form.get(str(name), [])
        return values[-1] if values else default

    def uploaded_files(self, name: str) -> tuple[DashboardUpload, ...]:
        return tuple(self.files.get(str(name), ()))


@dataclass(slots=True)
class DashboardResponse:
    content: Any = None
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    media_type: str | None = None


@dataclass(slots=True)
class DashboardFileResponse:
    path: Path
    filename: str | None = None
    media_type: str | None = None
    status_code: int = 200
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class DashboardRoute:
    path: str
    methods: frozenset[str]
    handler: DashboardHandler
    parameter_names: tuple[str, ...]
    pattern: re.Pattern[str]

    @classmethod
    def build(
        cls,
        path: str,
        handler: DashboardHandler,
        methods: list[str] | tuple[str, ...] | set[str] | frozenset[str],
    ) -> "DashboardRoute":
        normalized = normalize_dashboard_path(path)
        normalized_methods = frozenset(
            str(method or "").strip().upper()
            for method in methods
            if str(method or "").strip()
        )
        if not normalized_methods:
            raise ValueError("Dashboard API 至少需要一个 HTTP method")

        parameter_names: list[str] = []
        pattern_parts: list[str] = []
        for part in normalized.split("/"):
            parameter = _ROUTE_PARAMETER.match(part)
            if parameter:
                name = parameter.group(1)
                if name in parameter_names:
                    raise ValueError(f"Dashboard API 路径参数重复: {name}")
                parameter_names.append(name)
                pattern_parts.append(f"(?P<{name}>[^/]+)")
            else:
                pattern_parts.append(re.escape(part))
        pattern = re.compile("^" + "/".join(pattern_parts) + "$")
        return cls(
            path=normalized,
            methods=normalized_methods,
            handler=handler,
            parameter_names=tuple(parameter_names),
            pattern=pattern,
        )

    def match(self, path: str, method: str) -> dict[str, str] | None:
        if str(method or "").upper() not in self.methods:
            return None
        match = self.pattern.fullmatch(normalize_dashboard_path(path))
        return match.groupdict() if match else None


@dataclass(slots=True, frozen=True)
class DashboardSSERoute:
    path: str
    handler: DashboardSSEHandler
    parameter_names: tuple[str, ...]
    pattern: re.Pattern[str]

    @classmethod
    def build(cls, path: str, handler: DashboardSSEHandler) -> "DashboardSSERoute":
        route = DashboardRoute.build(path, handler, {"GET"})
        return cls(
            path=route.path,
            handler=handler,
            parameter_names=route.parameter_names,
            pattern=route.pattern,
        )

    def match(self, path: str) -> dict[str, str] | None:
        match = self.pattern.fullmatch(normalize_dashboard_path(path))
        return match.groupdict() if match else None

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?")
_ENDPOINT_INCOMPATIBLE_STATUSES = frozenset({404, 405, 410, 501})


@dataclass(frozen=True, order=True)
class RocketChatVersion:
    major: int
    minor: int
    patch: int = 0

    @classmethod
    def parse(cls, value: Any) -> RocketChatVersion | None:
        match = _VERSION_RE.match(str(value or ""))
        if not match:
            return None
        return cls(
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3) or 0),
        )

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class RocketChatCapabilities:
    version: RocketChatVersion | None
    compatibility_status: str
    supports_modern_media: bool
    allows_legacy_upload_fallback: bool
    allows_ddp_method_fallback: bool
    strict_rest_validation: bool
    secure_upload_validation: bool

    MINIMUM_VERSION = RocketChatVersion(7, 10, 0)
    MAXIMUM_TESTED_SERIES = RocketChatVersion(8, 5, 999999)

    @classmethod
    def unknown(cls) -> RocketChatCapabilities:
        return cls(
            version=None,
            compatibility_status="unknown",
            supports_modern_media=True,
            allows_legacy_upload_fallback=True,
            allows_ddp_method_fallback=True,
            strict_rest_validation=False,
            secure_upload_validation=False,
        )

    @classmethod
    def from_version(cls, value: Any) -> RocketChatCapabilities:
        version = value if isinstance(value, RocketChatVersion) else RocketChatVersion.parse(value)
        if version is None:
            return cls.unknown()

        supported = version >= cls.MINIMUM_VERSION
        if not supported:
            compatibility_status = "unsupported"
        elif version <= cls.MAXIMUM_TESTED_SERIES:
            compatibility_status = "supported"
        else:
            compatibility_status = "untested"
        return cls(
            version=version,
            compatibility_status=compatibility_status,
            supports_modern_media=supported,
            allows_legacy_upload_fallback=supported and version.major < 8,
            allows_ddp_method_fallback=supported and version.major < 8,
            strict_rest_validation=version >= RocketChatVersion(8, 3, 0),
            secure_upload_validation=version >= RocketChatVersion(8, 5, 0),
        )

    @property
    def version_text(self) -> str:
        return str(self.version) if self.version is not None else "unknown"


class UnsupportedRocketChatVersionError(RuntimeError):
    def __init__(self, version: RocketChatVersion):
        super().__init__(
            f"Rocket.Chat {version} is unsupported; RocketCatShell requires Rocket.Chat 7.10.0 or newer"
        )
        self.version = version


class RocketChatHTTPError(RuntimeError):
    def __init__(
        self,
        *,
        method: str,
        url: str,
        status: int,
        data: Any = None,
        headers: Mapping[str, str] | None = None,
        response_text: str = "",
    ) -> None:
        self.method = str(method or "").upper()
        self.url = url
        self.status = int(status)
        self.data = data
        self.headers = dict(headers or {})
        self.response_text = response_text
        self.error_code = self._extract_error_code(data)
        self.reason = self._extract_reason(data, response_text)
        super().__init__(
            f"Rocket.Chat HTTP {self.status} for {self.method} {self.url}: "
            f"{self.error_code or self.reason or 'request failed'}"
        )

    @staticmethod
    def _extract_error_code(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        for key in ("errorType", "error", "status"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        details = data.get("details")
        if isinstance(details, dict):
            value = details.get("errorType") or details.get("error")
            if isinstance(value, str):
                return value
        return ""

    @staticmethod
    def _extract_reason(data: Any, response_text: str) -> str:
        if isinstance(data, dict):
            for key in ("reason", "message", "error"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
        return " ".join(str(response_text or "").split())[:240]

    @property
    def endpoint_incompatible(self) -> bool:
        return self.status in _ENDPOINT_INCOMPATIBLE_STATUSES

    def get_header(self, name: str, default: str = "") -> str:
        normalized_name = str(name or "").lower()
        for key, value in self.headers.items():
            if str(key).lower() == normalized_name:
                return str(value)
        return default


class RocketChatMethodError(RuntimeError):
    def __init__(self, method: str, error: Any):
        self.method = method
        self.error = error
        if isinstance(error, dict):
            reason = error.get("reason") or error.get("message") or error.get("error")
        else:
            reason = error
        super().__init__(f"Rocket.Chat method {method} failed: {reason or error!r}")

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ..json_codec import json_dumps, json_loads


_CQ_PATTERN = re.compile(r"\[CQ:(?P<type>[^,\]]+)(?P<params>(?:,[^\]]*)?)\]")


@dataclass(slots=True)
class SerializedOneBotFrame:
    """One immutable JSON representation shared by all transport consumers."""

    text: str
    message_type: Any = None
    group_id: Any = None
    user_id: Any = None
    _utf8: bytes | None = field(default=None, init=False, repr=False)
    _sse: bytes | None = field(default=None, init=False, repr=False)

    @property
    def utf8(self) -> bytes:
        encoded = self._utf8
        if encoded is None:
            encoded = self.text.encode("utf-8")
            self._utf8 = encoded
        return encoded

    @property
    def sse(self) -> bytes:
        encoded = self._sse
        if encoded is None:
            encoded = b"data: " + self.utf8 + b"\n\n"
            self._sse = encoded
        return encoded

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SerializedOneBotFrame):
            return (
                self.text == other.text
                and self.message_type == other.message_type
                and self.group_id == other.group_id
                and self.user_id == other.user_id
            )
        # v0.2.2 exposed dictionaries in the private outgoing queue.  Decode
        # only for legacy inspection/equality; the transport hot path still
        # shares the pre-serialized frame without an extra parse or copy.
        if isinstance(other, Mapping):
            try:
                return json_loads(self.text) == dict(other)
            except (TypeError, ValueError):
                return False
        return NotImplemented


class OneBotMessageCodec:
    def __init__(self, message_post_format: str = "array"):
        normalized = str(message_post_format or "array").strip().lower()
        if normalized not in {"array", "string"}:
            normalized = "array"
        self.message_post_format = normalized

    def encode_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.message_post_format != "string" or not isinstance(payload.get("message"), list):
            return payload
        encoded = dict(payload)
        encoded["message"] = self.segments_to_cq(payload["message"])
        return encoded

    def serialize_event(self, payload: dict[str, Any]) -> SerializedOneBotFrame:
        encoded = self.encode_event(payload)
        return SerializedOneBotFrame(
            text=json_dumps(encoded),
            message_type=payload.get("message_type"),
            group_id=payload.get("group_id"),
            user_id=payload.get("user_id"),
        )

    @staticmethod
    def serialize_payload(payload: Any) -> SerializedOneBotFrame:
        return SerializedOneBotFrame(text=json_dumps(payload))

    def encode_response_data(self, payload: Any) -> Any:
        encoded = deepcopy(payload)
        if self.message_post_format != "string":
            return encoded
        return self._encode_nested_messages(encoded)

    def normalize_action_params(self, params: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(params or {})
        message = normalized.get("message")
        if isinstance(message, str) and "[CQ:" in message:
            normalized["message"] = self.cq_to_segments(message)
        return normalized

    @classmethod
    def _encode_nested_messages(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    cls.segments_to_cq(item)
                    if key == "message" and isinstance(item, list)
                    else cls._encode_nested_messages(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._encode_nested_messages(item) for item in value]
        return value

    @classmethod
    def segments_to_cq(cls, segments: list[Any]) -> str:
        parts: list[str] = []
        for raw_segment in segments:
            if not isinstance(raw_segment, dict):
                parts.append(cls._escape_text(str(raw_segment)))
                continue
            segment_type = str(raw_segment.get("type") or "text")
            data = raw_segment.get("data")
            data = data if isinstance(data, dict) else {}
            if segment_type == "text":
                parts.append(cls._escape_text(str(data.get("text") or "")))
                continue
            params = ",".join(
                f"{key}={cls._escape_param(str(value))}"
                for key, value in data.items()
                if value is not None
            )
            suffix = f",{params}" if params else ""
            parts.append(f"[CQ:{segment_type}{suffix}]")
        return "".join(parts)

    @classmethod
    def cq_to_segments(cls, value: str) -> list[dict[str, Any]]:
        source = str(value or "")
        result: list[dict[str, Any]] = []
        cursor = 0
        for match in _CQ_PATTERN.finditer(source):
            if match.start() > cursor:
                result.append(
                    {
                        "type": "text",
                        "data": {"text": cls._unescape(source[cursor : match.start()])},
                    }
                )
            data: dict[str, str] = {}
            raw_params = str(match.group("params") or "").lstrip(",")
            if raw_params:
                for item in raw_params.split(","):
                    key, separator, raw_value = item.partition("=")
                    if separator and key:
                        data[key] = cls._unescape(raw_value)
            result.append({"type": match.group("type"), "data": data})
            cursor = match.end()
        if cursor < len(source):
            result.append(
                {"type": "text", "data": {"text": cls._unescape(source[cursor:])}}
            )
        if not result:
            result.append({"type": "text", "data": {"text": cls._unescape(source)}})
        return result

    @staticmethod
    def _escape_text(value: str) -> str:
        return value.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")

    @classmethod
    def _escape_param(cls, value: str) -> str:
        return cls._escape_text(value).replace(",", "&#44;")

    @staticmethod
    def _unescape(value: str) -> str:
        return (
            value.replace("&#44;", ",")
            .replace("&#91;", "[")
            .replace("&#93;", "]")
            .replace("&amp;", "&")
        )

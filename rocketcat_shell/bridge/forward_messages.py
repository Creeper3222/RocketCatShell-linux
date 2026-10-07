from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .transports.codec import OneBotMessageCodec


_SUPPORTED_SEGMENTS = {"text", "at", "reply", "image", "file", "record", "video", "markdown"}


@dataclass(slots=True)
class ForwardMessageItem:
    node_index: int
    segments: list[dict[str, Any]] | None = None
    reference_id: str = ""


def is_forward_candidate(value: Any) -> bool:
    if isinstance(value, Mapping):
        if "messages" in value or str(value.get("type") or "").lower() == "node":
            return True
        return False
    if isinstance(value, list):
        return any(
            isinstance(item, Mapping)
            and (
                str(item.get("type") or "").lower() == "node"
                or "messages" in item
            )
            for item in value
        )
    return False


def parse_forward_nodes(payload: Any) -> list[ForwardMessageItem]:
    nodes = _unwrap_node_list(payload)
    if not nodes:
        raise ValueError("合并转发消息列表不能为空")
    if any(not _is_node(item) for item in nodes):
        if any(_contains_node(item) for item in nodes):
            raise ValueError("合并转发顶层不能混合普通消息与 node")
        raise ValueError("合并转发 messages 必须是 node 列表")

    result: list[ForwardMessageItem] = []
    for node_index, node in enumerate(nodes, start=1):
        _expand_node(node, node_index, result)
    if not result:
        raise ValueError("合并转发消息列表不能为空")
    return result


def validate_forward_segments(segments: Any, node_index: int) -> list[dict[str, Any]]:
    if not isinstance(segments, list) or not segments:
        raise ValueError(f"第 {node_index} 个 node 内容不能为空")
    return [_validate_segment(segment, node_index) for segment in segments]


def _unwrap_node_list(payload: Any) -> list[Any]:
    if isinstance(payload, Mapping) and "messages" in payload:
        return _unwrap_node_list(payload.get("messages"))
    if _is_node(payload):
        return [payload]
    if isinstance(payload, list):
        return payload
    raise ValueError("合并转发 messages 必须是 node 列表或包含 messages 的对象")


def _is_node(value: Any) -> bool:
    return isinstance(value, Mapping) and str(value.get("type") or "").lower() == "node"


def _contains_node(value: Any) -> bool:
    if _is_node(value):
        return True
    if isinstance(value, Mapping):
        nested = value.get("messages")
        return _contains_node(nested) if nested is not None else False
    if isinstance(value, list):
        return any(_contains_node(item) for item in value)
    return False


def _expand_node(node: Mapping[str, Any], node_index: int, result: list[ForwardMessageItem]) -> None:
    data = node.get("data")
    if not isinstance(data, Mapping):
        raise ValueError(f"第 {node_index} 个 node 缺少有效 data")

    reference_id = str(data.get("id") or "").strip()
    if reference_id:
        result.append(ForwardMessageItem(node_index=node_index, reference_id=reference_id))
        return

    content = data.get("content", data.get("messages"))
    if content is None:
        raise ValueError(f"第 {node_index} 个 node 缺少 content 或 id")
    before = len(result)
    _expand_content(content, node_index, result)
    if len(result) == before:
        raise ValueError(f"第 {node_index} 个 node 内容不能为空")


def _expand_content(content: Any, node_index: int, result: list[ForwardMessageItem]) -> None:
    if isinstance(content, Mapping) and "messages" in content:
        nested = _unwrap_node_list(content)
        if not nested:
            raise ValueError(f"第 {node_index} 个 node 的嵌套 messages 不能为空")
        if any(not _is_node(item) for item in nested):
            raise ValueError(f"第 {node_index} 个 node 的嵌套 messages 必须全部是 node")
        for nested_node in nested:
            _expand_node(nested_node, node_index, result)
        return

    if _is_node(content):
        _expand_node(content, node_index, result)
        return

    if isinstance(content, str):
        segments = OneBotMessageCodec.cq_to_segments(content)
        _append_segments(segments, node_index, result)
        return

    if isinstance(content, Mapping):
        _append_segments([dict(content)], node_index, result)
        return

    if not isinstance(content, list) or not content:
        raise ValueError(f"第 {node_index} 个 node 的 content 必须是非空消息或消息列表")

    if all(_is_node(item) for item in content):
        for nested_node in content:
            _expand_node(nested_node, node_index, result)
        return

    if any(_contains_node(item) for item in content):
        pending_segments: list[dict[str, Any]] = []
        for item in content:
            if _is_node(item) or (
                isinstance(item, Mapping) and "messages" in item
            ):
                if pending_segments:
                    _append_segments(pending_segments, node_index, result)
                    pending_segments = []
                if _is_node(item):
                    _expand_node(item, node_index, result)
                else:
                    nested = _unwrap_node_list(item)
                    if not nested or any(not _is_node(nested_item) for nested_item in nested):
                        raise ValueError(
                            f"第 {node_index} 个 node 的嵌套 messages 必须全部是 node"
                        )
                    for nested_node in nested:
                        _expand_node(nested_node, node_index, result)
                continue
            segment = _validate_segment(item, node_index)
            pending_segments.append(segment)
        if pending_segments:
            _append_segments(pending_segments, node_index, result)
        return

    _append_segments(content, node_index, result)


def _append_segments(
    segments: list[Any], node_index: int, result: list[ForwardMessageItem]
) -> None:
    validated = [_validate_segment(segment, node_index) for segment in segments]
    if not validated:
        raise ValueError(f"第 {node_index} 个 node 内容不能为空")
    result.append(ForwardMessageItem(node_index=node_index, segments=validated))


def _validate_segment(segment: Any, node_index: int) -> dict[str, Any]:
    if not isinstance(segment, Mapping):
        raise ValueError(f"第 {node_index} 个 node 包含无效消息段")
    segment_type = str(segment.get("type") or "").strip().lower()
    if segment_type not in _SUPPORTED_SEGMENTS:
        raise ValueError(f"第 {node_index} 个 node 包含不支持的消息段: {segment_type or '-'}")
    data = segment.get("data", {})
    if not isinstance(data, Mapping):
        raise ValueError(f"第 {node_index} 个 node 的 {segment_type} 消息段 data 无效")
    normalized = {"type": segment_type, "data": dict(data)}
    if segment_type == "reply" and not str(data.get("id") or "").strip():
        raise ValueError(f"第 {node_index} 个 node 的 reply 消息段缺少 id")
    if segment_type in {"image", "file", "record", "video"} and not str(
        data.get("file") or data.get("url") or ""
    ).strip():
        raise ValueError(f"第 {node_index} 个 node 的 {segment_type} 消息段缺少 file")
    return normalized

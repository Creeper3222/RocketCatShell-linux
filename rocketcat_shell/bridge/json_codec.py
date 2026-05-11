from __future__ import annotations

import json
from typing import Any

try:  # orjson is optional at runtime; requirements installs it for the fast path.
    import orjson
except Exception:  # pragma: no cover - exercised when optional dependency is absent.
    orjson = None


def json_loads(data: str | bytes | bytearray) -> Any:
    if orjson is not None:
        return orjson.loads(data)
    return json.loads(data)


def json_dumps(
    data: Any,
    *,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
    default: Any = str,
) -> str:
    if orjson is not None and not ensure_ascii:
        option = 0
        if sort_keys:
            option |= orjson.OPT_SORT_KEYS
        return orjson.dumps(data, option=option, default=default).decode("utf-8")
    return json.dumps(
        data,
        ensure_ascii=ensure_ascii,
        sort_keys=sort_keys,
        separators=separators,
        default=default,
    )


def json_dumps_compact(data: Any, *, sort_keys: bool = False, default: Any = str) -> str:
    return json_dumps(
        data,
        ensure_ascii=False,
        sort_keys=sort_keys,
        separators=(",", ":"),
        default=default,
    )

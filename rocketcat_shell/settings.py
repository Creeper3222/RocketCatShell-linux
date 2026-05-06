from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import ShellSettings


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
        handle.write("\n")
    temp_path.replace(path)


def load_or_create_shell_settings(path: Path) -> ShellSettings:
    payload = read_json(path, None)
    settings = ShellSettings.from_mapping(payload)
    if payload is None:
        write_json(path, settings.to_mapping())
    return settings
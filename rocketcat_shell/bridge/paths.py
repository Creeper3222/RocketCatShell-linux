from __future__ import annotations

from pathlib import Path


def resolve_project_root(package_root: Path) -> Path:
    return package_root.resolve().parent


def resolve_data_root(package_root: Path) -> Path:
    target_dir = resolve_project_root(package_root) / "data"
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir


def resolve_plugin_data_dir(package_root: Path) -> Path:
    target_dir = resolve_data_root(package_root) / "bots" / "default"
    target_dir.mkdir(parents=True, exist_ok=True)
    return target_dir
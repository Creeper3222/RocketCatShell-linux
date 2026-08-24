from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from . import __version__
from .bridge.hot_storage import JournalPersistenceWorker
from .bridge.user_identity import IDENTITY_SCHEMA_VERSION


PRODUCT_NAME = "RocketCatShell"
PACKAGE_ROOT_DIRECTORY = "RocketCatShell-linux"
PLATFORM_NAME = "linux"
TAG_NAME = __version__
VERSION = __version__
MIN_UPDATE_TAG = "v0.2.2"
SOURCE_COMPATIBILITY_MAX_EXCLUSIVE = "v0.3.0"
MANIFEST_NAME = "update-manifest.json"
MANIFEST_FORMAT_VERSION = 1
CONTAINER_RUNTIME_GENERATION = 1
CONFIG_SCHEMA_VERSION = 1
SNAPSHOT_SCHEMA_VERSION = JournalPersistenceWorker._SNAPSHOT_VERSION
MAX_LINUX_RELEASE_BYTES = 512 * 1024 * 1024
MAX_RELEASE_FILES = 10_000

# Only these paths may be replaced by an in-container update transaction.
MANAGED_DIRECTORIES = (
    "rocketcat_shell",
    "data/plugins/rocketcat_plugin_adapt_iamthinking",
    "data/plugins/rocketcat_plugin_built_in_command",
)
MANAGED_FILES = (
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "requirements.txt",
    "tools/check_requirements.py",
    "tools/migrate_user_identity.py",
    "tools/update_helper.py",
)

# These files ship in the Linux release ZIP for image deployment, but a hot
# update must never replace them in a running container.
IMAGE_DEPLOYMENT_FILES = (
    ".dockerignore",
    ".env.example",
    "Dockerfile",
    "docker-compose.yml",
    "launcher.sh",
    "docker/entrypoint.sh",
    "docker/examples/shell.json.example",
)

SOURCE_ONLY_DIRECTORIES = frozenset({"assets", "specs", "tests"})
SOURCE_ONLY_TOP_LEVEL_FILES = frozenset({".gitignore", ".gitattributes"})
SOURCE_ONLY_TOOL_FILES = frozenset(
    {
        "tools/benchmark_inbound_translate.py",
        "tools/build_linux_release.py",
        "tools/benchmark_v023_hotpaths.py",
        "tools/build_v022_acceptance_report.py",
        "tools/smoke_v022_readonly.py",
        "tools/stress_v020.py",
        "tools/stress_v022_full_stack.py",
    }
)
IGNORED_RELEASE_SOURCE_NAMES = frozenset(
    {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)
IGNORED_RELEASE_SOURCE_SUFFIXES = frozenset({".pyc", ".pyo"})
PROTECTED_TOP_LEVEL = frozenset(
    {
        ".git",
        ".venv",
        "backups",
        "config",
        "data",
        "logs",
        "specs",
        "tests",
    }
)
TAG_PATTERN = re.compile(
    r"^v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class UpdatePackageError(ValueError):
    pass


def parse_tag(tag: str) -> tuple[int, int, int, tuple[tuple[int, object], ...]]:
    match = TAG_PATTERN.fullmatch(str(tag or ""))
    if not match:
        raise UpdatePackageError("invalid semantic version tag")
    prerelease = match.group("pre")
    if prerelease is None:
        prerelease_key: tuple[tuple[int, object], ...] = ((1, ""),)
    else:
        parts: list[tuple[int, object]] = []
        for part in prerelease.split("."):
            if part.isdigit() and len(part) > 1 and part.startswith("0"):
                raise UpdatePackageError("invalid semantic version tag")
            parts.append((0, int(part)) if part.isdigit() else (1, part.lower()))
        prerelease_key = ((0, ""), *parts)
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        prerelease_key,
    )


def compare_tags(left: str, right: str) -> int:
    left_key = parse_tag(left)
    right_key = parse_tag(right)
    return (left_key > right_key) - (left_key < right_key)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_managed_path(relative: str) -> bool:
    return relative in MANAGED_FILES or any(
        relative == directory or relative.startswith(directory + "/")
        for directory in MANAGED_DIRECTORIES
    )


def _is_packaged_path(relative: str) -> bool:
    return _is_managed_path(relative) or relative in IMAGE_DEPLOYMENT_FILES


def audit_release_source_contract(root: Path) -> tuple[str, ...]:
    root = root.resolve()
    tracked_result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if tracked_result.returncode != 0:
        raise UpdatePackageError("release source must be a readable Git repository")
    dirty_result = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if dirty_result.returncode != 0:
        raise UpdatePackageError("release source Git status is unavailable")
    if dirty_result.stdout.strip():
        raise UpdatePackageError("release source contains uncommitted or untracked changes")

    tracked = tuple(
        item.decode("utf-8", "strict").replace("\\", "/")
        for item in tracked_result.stdout.split(b"\0")
        if item
    )
    unclassified: list[str] = []
    for relative in tracked:
        pure = PurePosixPath(relative)
        if not pure.parts or pure.is_absolute() or ".." in pure.parts:
            unclassified.append(relative)
        elif _is_packaged_path(relative):
            continue
        elif pure.parts[0] in SOURCE_ONLY_DIRECTORIES:
            continue
        elif relative in SOURCE_ONLY_TOP_LEVEL_FILES or relative in SOURCE_ONLY_TOOL_FILES:
            continue
        else:
            unclassified.append(relative)
    if unclassified:
        sample = ", ".join(sorted(unclassified)[:5])
        raise UpdatePackageError(
            "tracked files are not classified by the frozen Linux release contract: " + sample
        )

    tracked_set = set(tracked)
    missing_or_untracked: list[str] = []
    for path in _relative_files(root):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or relative not in tracked_set:
            missing_or_untracked.append(relative)
    if missing_or_untracked:
        sample = ", ".join(sorted(missing_or_untracked)[:5])
        raise UpdatePackageError("release source contains unsafe runtime files: " + sample)
    return tracked


def _relative_files(root: Path) -> Iterable[Path]:
    for directory in MANAGED_DIRECTORIES:
        base = root / Path(directory)
        if not base.is_dir() or base.is_symlink():
            raise UpdatePackageError(f"missing or unsafe managed directory: {directory}")
        for path in base.rglob("*"):
            if path.is_symlink():
                raise UpdatePackageError(
                    f"release source contains a symbolic link: {path.relative_to(root).as_posix()}"
                )
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if (
                any(part in IGNORED_RELEASE_SOURCE_NAMES for part in relative.parts)
                or path.suffix in IGNORED_RELEASE_SOURCE_SUFFIXES
            ):
                continue
            yield path
    for relative in (*MANAGED_FILES, *IMAGE_DEPLOYMENT_FILES):
        path = root / Path(relative)
        if not path.is_file() or path.is_symlink():
            raise UpdatePackageError(f"missing or unsafe release file: {relative}")
        yield path


def build_manifest(
    root: Path,
    *,
    version: str = VERSION,
    tag_name: str = TAG_NAME,
) -> dict[str, Any]:
    if version != tag_name:
        raise UpdatePackageError("version and tag do not match")
    if compare_tags(tag_name, MIN_UPDATE_TAG) < 0:
        raise UpdatePackageError("update manifests are supported from v0.2.2")
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(_relative_files(root), key=lambda item: item.as_posix().casefold()):
        relative = path.relative_to(root).as_posix()
        folded = relative.casefold()
        if folded in seen:
            raise UpdatePackageError(f"release paths collide by case: {relative}")
        seen.add(folded)
        files.append(
            {"path": relative, "size": path.stat().st_size, "sha256": sha256_file(path)}
        )
    return {
        "manifest_format": MANIFEST_FORMAT_VERSION,
        "product": PRODUCT_NAME,
        "platform": PLATFORM_NAME,
        "version": version,
        "tag_name": tag_name,
        "root_directory": PACKAGE_ROOT_DIRECTORY,
        "container_runtime_generation": CONTAINER_RUNTIME_GENERATION,
        "managed_directories": list(MANAGED_DIRECTORIES),
        "managed_files": list(MANAGED_FILES),
        "image_deployment_files": list(IMAGE_DEPLOYMENT_FILES),
        "python": {"implementation": "cpython", "minimum": "3.11"},
        "source_compatibility": {
            "minimum": MIN_UPDATE_TAG,
            "maximum_exclusive": SOURCE_COMPATIBILITY_MAX_EXCLUSIVE,
        },
        "persistent_compatibility": {
            "config_schema": {"minimum": CONFIG_SCHEMA_VERSION, "maximum": CONFIG_SCHEMA_VERSION},
            "runtime_snapshot": {"minimum": SNAPSHOT_SCHEMA_VERSION, "maximum": SNAPSHOT_SCHEMA_VERSION},
            "identity_registry": {"minimum": IDENTITY_SCHEMA_VERSION, "maximum": IDENTITY_SCHEMA_VERSION},
        },
        "files": files,
    }


def write_manifest(
    root: Path,
    *,
    version: str = VERSION,
    tag_name: str = TAG_NAME,
) -> Path:
    target = root / MANIFEST_NAME
    target.write_text(
        json.dumps(build_manifest(root, version=version, tag_name=tag_name), ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return target


def _validate_component(component: str) -> None:
    if not component or component in {".", ".."}:
        raise UpdatePackageError("release contains an unsafe path")
    if component[-1:] in {" ", "."} or any(ord(char) < 32 for char in component):
        raise UpdatePackageError("release contains an unsafe path component")


def _validated_relative_path(value: object) -> str:
    text = str(value or "")
    if "\\" in text or ":" in text:
        raise UpdatePackageError("release contains an unsafe path")
    pure = PurePosixPath(text)
    if not text or text.startswith("//") or pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise UpdatePackageError("release contains an unsafe path")
    for component in pure.parts:
        _validate_component(component)
    normalized = pure.as_posix()
    if _is_packaged_path(normalized):
        return normalized
    if pure.parts[0].casefold() in PROTECTED_TOP_LEVEL:
        raise UpdatePackageError("release attempts to manage a protected path")
    raise UpdatePackageError(f"release contains an unknown packaged path: {normalized}")


def _validate_persistent_compatibility(contract: dict[str, Any]) -> None:
    current = {
        "config_schema": CONFIG_SCHEMA_VERSION,
        "runtime_snapshot": SNAPSHOT_SCHEMA_VERSION,
        "identity_registry": IDENTITY_SCHEMA_VERSION,
    }
    for key, value in current.items():
        bounds = contract.get(key) or {}
        try:
            minimum = int(bounds["minimum"])
            maximum = int(bounds["maximum"])
        except (KeyError, TypeError, ValueError) as exc:
            raise UpdatePackageError(f"missing persistent compatibility range: {key}") from exc
        if not minimum <= value <= maximum:
            raise UpdatePackageError(f"target release is incompatible with {key}={value}")


def _validate_source_compatibility(contract: dict[str, Any], *, current_tag: str) -> None:
    minimum = str(contract.get("minimum") or "")
    maximum_exclusive = str(contract.get("maximum_exclusive") or "")
    if minimum != MIN_UPDATE_TAG or maximum_exclusive != SOURCE_COMPATIBILITY_MAX_EXCLUSIVE:
        raise UpdatePackageError("target source compatibility range does not match the v0.2 contract")
    if compare_tags(current_tag, minimum) < 0 or compare_tags(current_tag, maximum_exclusive) >= 0:
        raise UpdatePackageError(f"target release cannot switch from the current version {current_tag}")


def validate_manifest(
    manifest: dict[str, Any],
    *,
    expected_tag: str,
    candidate_root: Path | None = None,
    current_tag: str = TAG_NAME,
) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise UpdatePackageError("release manifest is not an object")
    if compare_tags(expected_tag, MIN_UPDATE_TAG) < 0:
        raise UpdatePackageError("versions below v0.2.2 are not update-compatible")
    if manifest.get("manifest_format") != MANIFEST_FORMAT_VERSION:
        raise UpdatePackageError("unsupported update manifest format")
    if manifest.get("product") != PRODUCT_NAME or manifest.get("platform") != PLATFORM_NAME:
        raise UpdatePackageError("release product or platform does not match")
    if manifest.get("tag_name") != expected_tag or manifest.get("version") != expected_tag:
        raise UpdatePackageError("release tag and internal version do not match")
    if manifest.get("root_directory") != PACKAGE_ROOT_DIRECTORY:
        raise UpdatePackageError("release root directory is invalid")
    if manifest.get("container_runtime_generation") != CONTAINER_RUNTIME_GENERATION:
        raise UpdatePackageError("target requires a different container runtime generation; update the image")
    if tuple(manifest.get("managed_directories") or ()) != MANAGED_DIRECTORIES:
        raise UpdatePackageError("managed directory contract does not match")
    if tuple(manifest.get("managed_files") or ()) != MANAGED_FILES:
        raise UpdatePackageError("managed file contract does not match")
    if tuple(manifest.get("image_deployment_files") or ()) != IMAGE_DEPLOYMENT_FILES:
        raise UpdatePackageError("image deployment contract does not match")
    python_contract = manifest.get("python") or {}
    if python_contract.get("implementation") != "cpython" or python_contract.get("minimum") != "3.11":
        raise UpdatePackageError("release Python contract is incompatible")
    if sys.implementation.name != "cpython" or sys.version_info[:2] < (3, 11):
        raise UpdatePackageError("current Python cannot run the target release")
    _validate_source_compatibility(manifest.get("source_compatibility") or {}, current_tag=current_tag)
    _validate_persistent_compatibility(manifest.get("persistent_compatibility") or {})

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_RELEASE_FILES:
        raise UpdatePackageError("release manifest file count is invalid")
    declared: set[str] = set()
    folded_paths: set[str] = set()
    total_size = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise UpdatePackageError("release manifest contains an invalid file entry")
        relative = _validated_relative_path(entry.get("path"))
        folded = relative.casefold()
        if relative in declared or folded in folded_paths:
            raise UpdatePackageError("release manifest contains duplicate paths")
        declared.add(relative)
        folded_paths.add(folded)
        raw_size = entry.get("size")
        digest = str(entry.get("sha256") or "").lower()
        if isinstance(raw_size, bool) or not isinstance(raw_size, int) or raw_size < 0:
            raise UpdatePackageError(f"release file size is invalid: {relative}")
        if not SHA256_PATTERN.fullmatch(digest):
            raise UpdatePackageError(f"release file digest is invalid: {relative}")
        total_size += raw_size
        if total_size > MAX_LINUX_RELEASE_BYTES:
            raise UpdatePackageError("release manifest expands beyond the size limit")
        if candidate_root is not None:
            path = candidate_root / Path(relative)
            if not path.is_file() or path.is_symlink():
                raise UpdatePackageError(f"release file is missing or unsafe: {relative}")
            if path.stat().st_size != raw_size or sha256_file(path) != digest:
                raise UpdatePackageError(f"release file verification failed: {relative}")

    required = set((*MANAGED_FILES, *IMAGE_DEPLOYMENT_FILES))
    if not required.issubset(declared):
        raise UpdatePackageError("release is missing required files")
    if candidate_root is not None:
        for relative in MANAGED_DIRECTORIES:
            if not (candidate_root / Path(relative)).is_dir():
                raise UpdatePackageError(f"release directory is missing: {relative}")
        actual = {
            path.relative_to(candidate_root).as_posix()
            for path in candidate_root.rglob("*")
            if path.is_file() and path.relative_to(candidate_root).as_posix() != MANIFEST_NAME
        }
        if actual != declared:
            raise UpdatePackageError(
                f"release file list mismatch: extra={sorted(actual - declared)[:3]} "
                f"missing={sorted(declared - actual)[:3]}"
            )
        version_source = (candidate_root / "rocketcat_shell" / "__init__.py").read_text(encoding="utf-8")
        version_match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', version_source, re.MULTILINE)
        if not version_match or version_match.group(1) != expected_tag:
            raise UpdatePackageError("release internal runtime version does not match the tag")
    return manifest


def inspect_and_extract_zip(
    zip_path: Path,
    destination: Path,
    *,
    expected_tag: str,
    current_tag: str = TAG_NAME,
) -> tuple[Path, dict[str, Any]]:
    if not zip_path.is_file() or zip_path.stat().st_size > MAX_LINUX_RELEASE_BYTES:
        raise UpdatePackageError("release asset exceeds the size limit")
    if destination.exists():
        raise UpdatePackageError("release extraction destination already exists")
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if not infos or len(infos) > MAX_RELEASE_FILES:
            raise UpdatePackageError("release archive file count is invalid")
        normalized: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        roots: set[str] = set()
        seen: set[str] = set()
        total_size = 0
        for info in infos:
            name = info.filename
            if "\\" in name or ":" in name:
                raise UpdatePackageError("release archive contains an unsafe path")
            pure = PurePosixPath(name)
            if not name or name.startswith("//") or pure.is_absolute() or ".." in pure.parts or not pure.parts:
                raise UpdatePackageError("release archive contains an unsafe path")
            for component in pure.parts:
                _validate_component(component)
            roots.add(pure.parts[0])
            folded = pure.as_posix().rstrip("/").casefold()
            if folded in seen:
                raise UpdatePackageError("release archive contains duplicate paths")
            seen.add(folded)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise UpdatePackageError("release archive contains a symbolic link")
            total_size += int(info.file_size)
            if total_size > MAX_LINUX_RELEASE_BYTES:
                raise UpdatePackageError("release archive expands beyond the size limit")
            normalized.append((info, pure))
        if roots != {PACKAGE_ROOT_DIRECTORY}:
            raise UpdatePackageError("release archive must contain one RocketCatShell-linux root directory")

        destination.mkdir(parents=True, exist_ok=False)
        for info, pure in normalized:
            relative_parts = pure.parts[1:]
            if not relative_parts:
                continue
            relative = PurePosixPath(*relative_parts).as_posix()
            if relative != MANIFEST_NAME:
                _validated_relative_path(relative)
            if info.is_dir():
                continue
            target = destination.joinpath(*pure.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            mode = info.external_attr >> 16
            if mode & stat.S_IXUSR:
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    root = destination / PACKAGE_ROOT_DIRECTORY
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise UpdatePackageError("release manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdatePackageError("release manifest is invalid") from exc
    return root, validate_manifest(
        manifest,
        expected_tag=expected_tag,
        candidate_root=root,
        current_tag=current_tag,
    )


def current_release_contract() -> dict[str, Any]:
    return {
        "product": PRODUCT_NAME,
        "platform": PLATFORM_NAME,
        "version": VERSION,
        "tag_name": TAG_NAME,
        "minimum_update_tag": MIN_UPDATE_TAG,
        "container_runtime_generation": CONTAINER_RUNTIME_GENERATION,
        "python": f"{sys.implementation.name} {sys.version_info.major}.{sys.version_info.minor}",
        "persistent_compatibility": {
            "config_schema": CONFIG_SCHEMA_VERSION,
            "runtime_snapshot": SNAPSHOT_SCHEMA_VERSION,
            "identity_registry": IDENTITY_SCHEMA_VERSION,
        },
    }

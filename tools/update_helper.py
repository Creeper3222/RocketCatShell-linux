from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any


TRANSACTION_ID_PATTERN = re.compile(r"^[0-9a-f]{24}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_STATUSES = frozenset({"completed", "failed", "rolled_back"})
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
MANAGED_PATHS = (*MANAGED_DIRECTORIES, *MANAGED_FILES)
IMAGE_DEPLOYMENT_FILES = (
    ".dockerignore",
    ".env.example",
    "Dockerfile",
    "docker-compose.yml",
    "launcher.sh",
    "docker/entrypoint.sh",
    "docker/examples/shell.json.example",
)
PACKAGE_ROOT_DIRECTORY = "RocketCatShell-linux"
PRODUCT_NAME = "RocketCatShell"
CONTAINER_RUNTIME_GENERATION = 1
HEALTH_TIMEOUT_SECONDS = 120.0
ROLLBACK_HEALTH_TIMEOUT_SECONDS = 120.0


class UpdateHelperError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateHelperError(f"invalid JSON state: {path.name}") from exc
    if not isinstance(payload, dict):
        raise UpdateHelperError(f"JSON state is not an object: {path.name}")
    return payload


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _update_transaction(path: Path, payload: dict[str, Any], **changes: Any) -> dict[str, Any]:
    payload = dict(payload)
    payload.update(changes)
    payload["updated_at"] = time.time()
    _atomic_write_json(path, payload)
    return payload


def _safe_relative(value: object) -> str:
    text = str(value or "")
    pure = PurePosixPath(text)
    if (
        not text
        or "\\" in text
        or ":" in text
        or pure.is_absolute()
        or ".." in pure.parts
        or not pure.parts
    ):
        raise UpdateHelperError("unsafe transaction path")
    return pure.as_posix()


def _is_managed(relative: str) -> bool:
    return relative in MANAGED_FILES or any(
        relative == directory or relative.startswith(directory + "/")
        for directory in MANAGED_DIRECTORIES
    )


def _assert_no_symlink_path(root: Path, path: Path) -> None:
    root = root.resolve()
    try:
        relative = path.absolute().relative_to(root)
    except ValueError as exc:
        raise UpdateHelperError("transaction path escaped the installation") from exc
    cursor = root
    for component in relative.parts:
        cursor /= component
        if cursor.is_symlink():
            raise UpdateHelperError("transaction path contains a symbolic link")


def _context(transaction_file: Path) -> tuple[dict[str, Any], Path, Path, Path, Path, Path]:
    transaction_file = transaction_file.absolute()
    if transaction_file.name != "transaction.json" or transaction_file.is_symlink():
        raise UpdateHelperError("transaction file path is invalid")
    payload = _read_json(transaction_file)
    transaction_id = str(payload.get("transaction_id") or "")
    if not TRANSACTION_ID_PATTERN.fullmatch(transaction_id):
        raise UpdateHelperError("transaction id is invalid")
    source_root = Path(str(payload.get("source_root") or "")).resolve()
    state_root = Path(str(payload.get("state_root") or "")).resolve()
    if source_root != state_root:
        raise UpdateHelperError("Linux update source and state roots must match")
    update_root = source_root / "data" / "update"
    transaction_root = (update_root / "transactions" / transaction_id).resolve()
    if transaction_file.resolve() != transaction_root / "transaction.json":
        raise UpdateHelperError("transaction file is outside the update directory")
    _assert_no_symlink_path(source_root, transaction_file)
    candidate_root = Path(str(payload.get("candidate_root") or "")).resolve()
    expected_candidate = (transaction_root / "candidate" / PACKAGE_ROOT_DIRECTORY).resolve()
    if candidate_root != expected_candidate:
        raise UpdateHelperError("candidate root is invalid")
    backup_root = transaction_root / "backup"
    runtime_path = update_root / "runtime.json"
    return payload, source_root, transaction_root, candidate_root, backup_root, runtime_path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_candidate(candidate_root: Path, payload: dict[str, Any]) -> None:
    entries = payload.get("candidate_files")
    if not isinstance(entries, list) or not entries:
        raise UpdateHelperError("transaction candidate contract is missing")
    declared: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise UpdateHelperError("transaction candidate entry is invalid")
        relative = _safe_relative(entry.get("path"))
        if not (_is_managed(relative) or relative in IMAGE_DEPLOYMENT_FILES):
            raise UpdateHelperError("transaction candidate contains an unknown path")
        if relative in declared:
            raise UpdateHelperError("transaction candidate contains a duplicate path")
        declared.add(relative)
        size = entry.get("size")
        digest = str(entry.get("sha256") or "").lower()
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise UpdateHelperError("transaction candidate size is invalid")
        if not SHA256_PATTERN.fullmatch(digest):
            raise UpdateHelperError("transaction candidate digest is invalid")
        path = candidate_root / Path(relative)
        if not path.is_file() or path.is_symlink():
            raise UpdateHelperError(f"transaction candidate file is missing: {relative}")
        if path.stat().st_size != size or _sha256(path) != digest:
            raise UpdateHelperError(f"transaction candidate verification failed: {relative}")
    required = set((*MANAGED_FILES, *IMAGE_DEPLOYMENT_FILES))
    if not required.issubset(declared):
        raise UpdateHelperError("transaction candidate is missing required files")
    for directory in MANAGED_DIRECTORIES:
        path = candidate_root / directory
        if not path.is_dir() or path.is_symlink():
            raise UpdateHelperError(f"transaction candidate directory is missing: {directory}")
    actual = {
        path.relative_to(candidate_root).as_posix()
        for path in candidate_root.rglob("*")
        if path.is_file() and path.name != "update-manifest.json"
    }
    if actual != declared:
        raise UpdateHelperError("transaction candidate file set changed after validation")


def _validate_tree_without_symlinks(path: Path) -> None:
    if path.is_symlink():
        raise UpdateHelperError(f"managed path is a symbolic link: {path.name}")
    if path.is_dir():
        for item in path.rglob("*"):
            if item.is_symlink():
                raise UpdateHelperError(f"managed tree contains a symbolic link: {item.name}")


def _copy_exact(source: Path, target: Path) -> None:
    _validate_tree_without_symlinks(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, symlinks=False)
    else:
        shutil.copy2(source, target)


def _remove_exact(source_root: Path, relative: str) -> None:
    target = source_root / Path(relative)
    _assert_no_symlink_path(source_root, target.parent)
    if target.is_symlink():
        raise UpdateHelperError(f"managed target is a symbolic link: {relative}")
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists():
        target.unlink()


def _backup(source_root: Path, backup_root: Path, runtime_path: Path) -> None:
    if backup_root.exists():
        raise UpdateHelperError("transaction backup already exists")
    files_root = backup_root / "files"
    files_root.mkdir(parents=True, exist_ok=False)
    presence: dict[str, str] = {}
    for relative in MANAGED_PATHS:
        source = source_root / Path(relative)
        if source.is_symlink():
            raise UpdateHelperError(f"managed source is a symbolic link: {relative}")
        if source.is_dir():
            presence[relative] = "directory"
            _copy_exact(source, files_root / Path(relative))
        elif source.is_file():
            presence[relative] = "file"
            _copy_exact(source, files_root / Path(relative))
        elif source.exists():
            raise UpdateHelperError(f"managed source has an unsupported type: {relative}")
        else:
            presence[relative] = "missing"
    _atomic_write_json(backup_root / "presence.json", presence)
    if runtime_path.is_file() and not runtime_path.is_symlink():
        shutil.copy2(runtime_path, backup_root / "runtime.json")
        runtime_presence = True
    else:
        runtime_presence = False
    _atomic_write_json(
        backup_root / "complete.json",
        {"complete": True, "runtime_present": runtime_presence, "created_at": time.time()},
    )


def _backup_complete(backup_root: Path) -> bool:
    try:
        payload = _read_json(backup_root / "complete.json")
        presence = _read_json(backup_root / "presence.json")
    except UpdateHelperError:
        return False
    return payload.get("complete") is True and set(presence) == set(MANAGED_PATHS)


def _install(source_root: Path, candidate_root: Path) -> None:
    for relative in MANAGED_PATHS:
        candidate = candidate_root / Path(relative)
        if not candidate.exists() or candidate.is_symlink():
            raise UpdateHelperError(f"candidate managed path is missing: {relative}")
    for relative in MANAGED_PATHS:
        _remove_exact(source_root, relative)
        _copy_exact(candidate_root / Path(relative), source_root / Path(relative))


def _restore(source_root: Path, backup_root: Path, runtime_path: Path) -> None:
    if not _backup_complete(backup_root):
        raise UpdateHelperError("transaction backup is incomplete")
    presence = _read_json(backup_root / "presence.json")
    files_root = backup_root / "files"
    for relative in MANAGED_PATHS:
        kind = presence.get(relative)
        if kind not in {"directory", "file", "missing"}:
            raise UpdateHelperError("transaction backup presence record is invalid")
        _remove_exact(source_root, relative)
        if kind != "missing":
            backup = files_root / Path(relative)
            if not backup.exists():
                raise UpdateHelperError(f"transaction backup is missing: {relative}")
            _copy_exact(backup, source_root / Path(relative))
    complete = _read_json(backup_root / "complete.json")
    if complete.get("runtime_present") is True:
        backup_runtime = backup_root / "runtime.json"
        if not backup_runtime.is_file() or backup_runtime.is_symlink():
            raise UpdateHelperError("transaction runtime backup is missing")
        shutil.copy2(backup_runtime, runtime_path)
    elif runtime_path.exists():
        if runtime_path.is_symlink() or not runtime_path.is_file():
            raise UpdateHelperError("runtime marker has an unsafe type")
        runtime_path.unlink()


def _requirements_digest(path: Path) -> str:
    return _sha256(path)


def _prepare_python(
    source_root: Path,
    transaction_root: Path,
    candidate_root: Path,
    payload: dict[str, Any],
) -> str:
    old_python = Path(str(payload.get("old_python") or sys.executable)).resolve()
    if not old_python.is_file():
        raise UpdateHelperError("current Python interpreter is unavailable")
    current_requirements = source_root / "requirements.txt"
    candidate_requirements = candidate_root / "requirements.txt"
    if (
        current_requirements.is_file()
        and _requirements_digest(current_requirements) == _requirements_digest(candidate_requirements)
    ):
        return str(old_python)

    digest = _requirements_digest(candidate_requirements)[:20]
    venv_root = source_root / "data" / "update" / "venvs"
    venv_root.mkdir(parents=True, exist_ok=True)
    target = venv_root / digest
    python_path = target / "bin" / "python"
    if python_path.is_file():
        check = subprocess.run(
            [str(python_path), str(candidate_root / "tools" / "check_requirements.py"), str(candidate_requirements)],
            cwd=str(candidate_root),
            check=False,
        )
        if check.returncode == 0:
            return str(python_path)

    staging = transaction_root / "runtime-venv"
    if staging.exists():
        shutil.rmtree(staging)
    result = subprocess.run(
        [str(old_python), "-m", "venv", "--system-site-packages", str(staging)],
        cwd=str(source_root),
        check=False,
    )
    if result.returncode != 0:
        raise UpdateHelperError("failed to create the isolated dependency environment")
    staging_python = staging / "bin" / "python"
    install = subprocess.run(
        [
            str(staging_python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "-r",
            str(candidate_requirements),
        ],
        cwd=str(candidate_root),
        check=False,
    )
    if install.returncode != 0:
        raise UpdateHelperError("failed to prepare target release dependencies")
    check = subprocess.run(
        [str(staging_python), str(candidate_root / "tools" / "check_requirements.py"), str(candidate_requirements)],
        cwd=str(candidate_root),
        check=False,
    )
    if check.returncode != 0:
        raise UpdateHelperError("target dependency verification failed")
    if target.exists():
        shutil.rmtree(target)
    os.replace(staging, target)
    return str(target / "bin" / "python")


def _runtime_payload(
    *,
    version: str,
    python: str,
    image_version: str,
    transaction_id: str = "",
    mode: str = "active",
) -> dict[str, Any]:
    return {
        "runtime_version": version,
        "python": python,
        "image_version": image_version,
        "container_runtime_generation": CONTAINER_RUNTIME_GENERATION,
        "active_transaction": transaction_id,
        "mode": mode,
        "updated_at": time.time(),
    }


def _pending_path(source_root: Path) -> Path:
    return source_root / "data" / "update" / "pending-handoff.json"


def _clear_pending(source_root: Path) -> None:
    pending = _pending_path(source_root)
    if pending.is_symlink():
        raise UpdateHelperError("pending handoff marker is a symbolic link")
    if pending.is_file():
        pending.unlink()


def _spawn_watchdog(python: str, helper: Path, transaction_file: Path, mode: str) -> None:
    subprocess.Popen(
        [python, str(helper), "watch", str(transaction_file), "--mode", mode],
        cwd=str(transaction_file.parents[4]),
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def _exec_runtime(
    source_root: Path,
    python: str,
    transaction_id: str,
    image_version: str,
) -> None:
    environment = dict(os.environ)
    environment["ROCKETCATSHELL_UPDATE_TRANSACTION"] = transaction_id
    environment["ROCKETCAT_IMAGE_VERSION"] = image_version
    environment["ROCKETCAT_CONTAINER_RUNTIME_GENERATION"] = str(CONTAINER_RUNTIME_GENERATION)
    os.chdir(source_root)
    os.execve(
        python,
        [python, "-m", "rocketcat_shell", "--no-browser"],
        environment,
    )


def apply_transaction(transaction_file: Path) -> int:
    payload: dict[str, Any] | None = None
    try:
        payload, source_root, transaction_root, candidate_root, backup_root, runtime_path = _context(transaction_file)
        if os.name != "posix" or sys.platform != "linux" or os.getpid() != 1:
            raise UpdateHelperError("Linux update helper must replace container PID 1")
        if payload.get("status") != "prepared" or payload.get("stage") != "waiting_for_shutdown":
            raise UpdateHelperError("transaction is not ready to apply")
        _validate_candidate(candidate_root, payload)
        payload = _update_transaction(transaction_file, payload, status="applying", stage="preparing_dependencies")
        target_python = _prepare_python(source_root, transaction_root, candidate_root, payload)
        payload = _update_transaction(
            transaction_file,
            payload,
            stage="backing_up",
            target_python=target_python,
        )
        _backup(source_root, backup_root, runtime_path)
        payload = _update_transaction(transaction_file, payload, stage="backup_complete")
        payload = _update_transaction(transaction_file, payload, stage="replacing")
        _install(source_root, candidate_root)
        transaction_id = str(payload["transaction_id"])
        image_version = str(payload.get("image_version") or payload.get("current_version") or "")
        _atomic_write_json(
            runtime_path,
            _runtime_payload(
                version=str(payload["target_version"]),
                python=target_python,
                image_version=image_version,
                transaction_id=transaction_id,
                mode="target_monitoring",
            ),
        )
        _clear_pending(source_root)
        payload = _update_transaction(transaction_file, payload, status="monitoring", stage="starting_target")
        helper = transaction_root / "update_helper.py"
        _spawn_watchdog(target_python, helper, transaction_file, "target")
        _update_transaction(transaction_file, payload, status="monitoring", stage="monitoring_target")
        _exec_runtime(source_root, target_python, transaction_id, image_version)
        return 0
    except Exception as exc:
        if payload is not None:
            try:
                stage = str(payload.get("stage") or "")
                if stage in {"preparing_dependencies", "backing_up"}:
                    _clear_pending(Path(str(payload.get("source_root") or ".")).resolve())
                    _update_transaction(
                        transaction_file,
                        payload,
                        status="failed",
                        stage="apply_failed_before_replacement",
                        error=str(exc),
                        completed_at=time.time(),
                    )
                else:
                    _update_transaction(
                        transaction_file,
                        payload,
                        status="recovery_required",
                        stage="apply_interrupted",
                        error=str(exc),
                    )
            except Exception:
                pass
        print(f"[update-helper] apply failed: {exc}", file=sys.stderr, flush=True)
        return 1


def _health_matches(urls: list[str], expected_version: str, transaction_id: str, timeout: float) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for base_url in urls:
            try:
                request = urllib.request.Request(
                    str(base_url).rstrip("/") + "/api/health",
                    headers={"User-Agent": "RocketCatShell-linux-update-helper/1"},
                )
                with opener.open(request, timeout=2.0) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if (
                    isinstance(payload, dict)
                    and payload.get("status") == "ok"
                    and payload.get("product") == PRODUCT_NAME
                    and payload.get("version") == expected_version
                    and payload.get("update_transaction") == transaction_id
                ):
                    return True
            except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
                continue
        time.sleep(0.5)
    return False


def watch_transaction(transaction_file: Path, mode: str) -> int:
    try:
        payload, source_root, _, _, _, runtime_path = _context(transaction_file)
        transaction_id = str(payload["transaction_id"])
        urls = [str(item) for item in payload.get("health_urls") or []]
        if mode == "target":
            expected_version = str(payload["target_version"])
            timeout = HEALTH_TIMEOUT_SECONDS
        elif mode == "rollback":
            expected_version = str(payload["current_version"])
            timeout = ROLLBACK_HEALTH_TIMEOUT_SECONDS
        else:
            raise UpdateHelperError("unknown health watch mode")
        if _health_matches(urls, expected_version, transaction_id, timeout):
            current = _read_json(transaction_file)
            runtime = _read_json(runtime_path)
            runtime["active_transaction"] = ""
            runtime["mode"] = "active"
            runtime["updated_at"] = time.time()
            _atomic_write_json(runtime_path, runtime)
            if mode == "target":
                _update_transaction(
                    transaction_file,
                    current,
                    status="completed",
                    stage="completed",
                    error="",
                    completed_at=time.time(),
                )
            else:
                _update_transaction(
                    transaction_file,
                    current,
                    status="rolled_back",
                    stage="rolled_back",
                    rollback_error="",
                    completed_at=time.time(),
                )
            return 0

        current = _read_json(transaction_file)
        if mode == "target":
            _update_transaction(
                transaction_file,
                current,
                status="rollback_pending",
                stage="target_health_failed",
                error="target service failed the transaction health check",
            )
            # Docker's restart policy restarts the container. The image-frozen
            # helper restores the backup before the application starts again.
            os.kill(1, getattr(signal, "SIGTERM", 15))
            time.sleep(10.0)
            os.kill(1, getattr(signal, "SIGKILL", 9))
        else:
            _update_transaction(
                transaction_file,
                current,
                status="recovery_required",
                stage="rollback_health_failed",
                rollback_error="restored service failed the health check",
            )
        return 1
    except Exception as exc:
        print(f"[update-helper] health watcher failed: {exc}", file=sys.stderr, flush=True)
        return 1


def _restore_for_recovery(transaction_file: Path) -> bool:
    payload, source_root, transaction_root, _, backup_root, runtime_path = _context(transaction_file)
    if not _backup_complete(backup_root):
        _update_transaction(
            transaction_file,
            payload,
            status="recovery_required",
            stage="backup_incomplete",
            rollback_error="transaction backup is incomplete",
        )
        return False
    payload = _update_transaction(transaction_file, payload, status="rolling_back", stage="restoring_backup")
    _restore(source_root, backup_root, runtime_path)
    old_python = str(payload.get("old_python") or sys.executable)
    image_version = str(payload.get("image_version") or payload.get("current_version") or "")
    if runtime_path.is_file():
        runtime = _read_json(runtime_path)
        old_python = str(runtime.get("python") or old_python)
        image_version = str(runtime.get("image_version") or image_version)
    runtime = _runtime_payload(
        version=str(payload["current_version"]),
        python=old_python,
        image_version=image_version,
        transaction_id=str(payload["transaction_id"]),
        mode="rollback_monitoring",
    )
    _atomic_write_json(runtime_path, runtime)
    _clear_pending(source_root)
    _update_transaction(transaction_file, payload, status="rolling_back", stage="monitoring_rollback")
    helper = transaction_root / "update_helper.py"
    _spawn_watchdog(old_python, helper, transaction_file, "rollback")
    return True


def recover_transactions(source_root: Path) -> int:
    source_root = source_root.resolve()
    update_root = source_root / "data" / "update"
    transactions_root = update_root / "transactions"
    if transactions_root.is_symlink():
        print("[update-helper] unsafe transactions directory", file=sys.stderr)
        return 1
    if not transactions_root.is_dir():
        return 0
    candidates: list[tuple[float, Path, dict[str, Any]]] = []
    for transaction_file in transactions_root.glob("*/transaction.json"):
        try:
            payload = _read_json(transaction_file)
            if payload.get("status") in TERMINAL_STATUSES:
                continue
            candidates.append((float(payload.get("created_at") or 0), transaction_file, payload))
        except Exception:
            continue
    if not candidates:
        return 0
    _, transaction_file, payload = max(candidates, key=lambda item: item[0])
    stage = str(payload.get("stage") or "")
    try:
        if stage in {
            "prepared",
            "waiting_for_shutdown",
            "preparing_dependencies",
            "backing_up",
            "apply_failed_before_replacement",
        }:
            source = Path(str(payload.get("source_root") or source_root)).resolve()
            _clear_pending(source)
            _update_transaction(
                transaction_file,
                payload,
                status="failed",
                stage="interrupted_before_replacement",
                error="container restarted before code replacement",
                completed_at=time.time(),
            )
            return 0
        if _restore_for_recovery(transaction_file):
            return 0
        return 1
    except Exception as exc:
        try:
            _update_transaction(
                transaction_file,
                payload,
                status="recovery_required",
                stage="restore_failed",
                rollback_error=str(exc),
            )
        except Exception:
            pass
        print(f"[update-helper] recovery failed: {exc}", file=sys.stderr, flush=True)
        return 1


def runtime_python(source_root: Path) -> int:
    source_root = source_root.resolve()
    marker = source_root / "data" / "update" / "runtime.json"
    value = sys.executable
    if marker.is_file() and not marker.is_symlink():
        try:
            payload = _read_json(marker)
            candidate = Path(str(payload.get("python") or "")).resolve()
            if candidate.is_file() and str(payload.get("container_runtime_generation")) == str(
                CONTAINER_RUNTIME_GENERATION
            ):
                value = str(candidate)
        except Exception:
            pass
    print(value)
    return 0


def run_runtime(source_root: Path) -> int:
    source_root = source_root.resolve()
    marker = source_root / "data" / "update" / "runtime.json"
    python = sys.executable
    transaction_id = ""
    image_version = str(os.environ.get("ROCKETCAT_IMAGE_VERSION") or "").strip()
    if marker.is_file() and not marker.is_symlink():
        payload = _read_json(marker)
        candidate = Path(str(payload.get("python") or "")).resolve()
        if str(payload.get("container_runtime_generation")) != str(CONTAINER_RUNTIME_GENERATION):
            raise UpdateHelperError("runtime marker generation does not match the image")
        if not candidate.is_file():
            raise UpdateHelperError("runtime marker Python interpreter is unavailable")
        python = str(candidate)
        transaction_id = str(payload.get("active_transaction") or "")
        if transaction_id and not TRANSACTION_ID_PATTERN.fullmatch(transaction_id):
            raise UpdateHelperError("runtime marker transaction id is invalid")
        image_version = str(payload.get("image_version") or image_version)
    environment = dict(os.environ)
    environment["ROCKETCAT_CONTAINER_RUNTIME_GENERATION"] = str(CONTAINER_RUNTIME_GENERATION)
    if image_version:
        environment["ROCKETCAT_IMAGE_VERSION"] = image_version
    if transaction_id:
        environment["ROCKETCATSHELL_UPDATE_TRANSACTION"] = transaction_id
    else:
        environment.pop("ROCKETCATSHELL_UPDATE_TRANSACTION", None)
    os.chdir(source_root)
    os.execve(
        python,
        [python, "-m", "rocketcat_shell", "--no-browser"],
        environment,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RocketCatShell Linux update helper")
    subparsers = parser.add_subparsers(dest="command", required=True)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("transaction_file")
    watch_parser = subparsers.add_parser("watch")
    watch_parser.add_argument("transaction_file")
    watch_parser.add_argument("--mode", choices=("target", "rollback"), required=True)
    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("source_root")
    runtime_parser = subparsers.add_parser("runtime-python")
    runtime_parser.add_argument("source_root")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("source_root")
    args = parser.parse_args(argv)
    if args.command == "apply":
        return apply_transaction(Path(args.transaction_file))
    if args.command == "watch":
        return watch_transaction(Path(args.transaction_file), args.mode)
    if args.command == "recover":
        return recover_transactions(Path(args.source_root))
    if args.command == "run":
        return run_runtime(Path(args.source_root))
    return runtime_python(Path(args.source_root))


if __name__ == "__main__":
    raise SystemExit(main())

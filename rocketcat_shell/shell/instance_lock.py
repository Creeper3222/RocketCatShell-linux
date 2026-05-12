from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class SingleInstanceError(RuntimeError):
    def __init__(self, details: dict[str, Any] | None = None):
        self.details = details or {}
        pid = self.details.get("pid")
        suffix = f" (pid={pid})" if pid else ""
        super().__init__(f"RocketCat Shell 已在当前项目中运行{suffix}")


class ShellInstanceLock:
    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._handle: BinaryIO | None = None

    def acquire(self, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._handle is not None:
            return self._read_details(self._handle)

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+b")
        self._ensure_lock_byte(handle)
        try:
            self._lock_handle(handle)
        except OSError as exc:
            details = self._read_details(handle)
            handle.close()
            raise SingleInstanceError(details) from exc

        payload = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            payload.update(metadata)
        self._write_details(handle, payload)
        self._handle = handle
        return payload

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return

        try:
            self._write_details(handle, {})
            self._unlock_handle(handle)
        finally:
            handle.close()
            self._handle = None

    def _ensure_lock_byte(self, handle: BinaryIO) -> None:
        handle.seek(0, os.SEEK_END)
        if handle.tell() <= 0:
            handle.write(b"\0")
            handle.flush()

    def _lock_handle(self, handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_handle(self, handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_details(self, handle: BinaryIO) -> dict[str, Any]:
        handle.seek(1)
        raw = handle.read()
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_details(self, handle: BinaryIO, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else b""
        handle.seek(1)
        handle.truncate()
        if encoded:
            handle.write(encoded)
        handle.flush()
from __future__ import annotations

import asyncio
import codecs
import contextlib
import io
import json
import logging
import mimetypes
import os
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
import uuid
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect, status
from fastapi import File as FastAPIFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..__init__ import __version__
from ..bridge.user_identity import (
    UserIdentityConflictError,
    UserIdentityError,
    UserIdentityRevisionError,
)
from ..logger import logger

try:
    import fcntl
    import pty
    import select
    import termios
except Exception:  # pragma: no cover - Linux PTY modules are not available on Windows hosts.
    fcntl = None
    pty = None
    select = None
    termios = None


_FILE_PREVIEW_LIMIT_BYTES = 1024 * 1024
_FILE_UPLOAD_CHUNK_BYTES = 1024 * 1024
_FILE_UPLOAD_LIMIT_BYTES = 100 * 1024 * 1024
_FILE_UPLOAD_MAX_FILES = 20
_FILE_EDIT_LIMIT_BYTES = 1024 * 1024
_TERMINAL_BUFFER_LIMIT_CHARS = 200_000
_TERMINAL_DEFAULT_COLS = 80
_TERMINAL_DEFAULT_ROWS = 24
_PROTECTED_FILE_EXACT_PATHS = {
    (".dockerignore",),
    (".env.example",),
    ("dockerfile",),
    ("docker-compose.yml",),
    ("launcher.sh",),
    ("requirements.txt",),
}
_PROTECTED_FILE_ROOTS = {
    ("docker",),
    ("data", "plugins", "rocketcat_plugin_adapt_iamthinking"),
    ("data", "plugins", "rocketcat_plugin_built_in_command"),
    ("rocketcat_shell",),
    ("tools",),
}
_IMAGE_PREVIEW_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".webp",
}
_IMAGE_PREVIEW_MEDIA_TYPES = {
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_BINARY_PREVIEW_EXTENSIONS = {
    ".7z",
    ".bin",
    ".bmp",
    ".dll",
    ".exe",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mp3",
    ".mp4",
    ".png",
    ".pyc",
    ".rar",
    ".so",
    ".webm",
    ".wav",
    ".zip",
}


class BridgeLogBuffer:
    PERF_PREFIX = "[RocketCatPerf]"
    PREFIXES = ("[RocketChatOneBotBridge]", "[RocketCatShell]", PERF_PREFIX)

    def __init__(self, max_entries: int = 5000):
        self.max_entries = int(max_entries)
        self._entries: deque[dict[str, Any]] = deque(maxlen=self.max_entries)
        self._lock = threading.Lock()
        self._next_id = 1
        self._version = 0
        self._changed = asyncio.Event()
        self._event_loop: asyncio.AbstractEventLoop | None = None

    def append_record(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if not any(prefix in message for prefix in self.PREFIXES):
            return

        is_perf = self.PERF_PREFIX in message

        level = record.levelname.upper()
        if level == "WARNING":
            level = "WARN"

        entry = {
            "id": self._next_id,
            "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
            + f".{int(record.msecs):03d}",
            "level": level,
            "is_perf": is_perf,
            "message": message,
            "line": f"[{datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S')}.{int(record.msecs):03d}] [{level}] {message}",
        }

        with self._lock:
            self._entries.append(entry)
            self._next_id += 1
            self._version += 1
        self._notify_changed()

    def get_entries(self, *, after_id: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(entry) for entry in self._entries if int(entry["id"]) > int(after_id)]

    def latest_id(self) -> int:
        with self._lock:
            if self._entries:
                return int(self._entries[-1]["id"])
            return max(0, self._next_id - 1)

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    async def wait_for_change(self, version: int, *, timeout: float) -> None:
        self._event_loop = asyncio.get_running_loop()
        if self.version != version:
            return
        try:
            await asyncio.wait_for(self._changed.wait(), timeout=max(0.0, float(timeout)))
        except asyncio.TimeoutError:
            return
        finally:
            self._changed.clear()

    def clear(self) -> int:
        with self._lock:
            cleared = len(self._entries)
            self._entries.clear()
            self._next_id = 1
            self._version += 1
        self._notify_changed()
        return cleared

    def _notify_changed(self) -> None:
        loop = self._event_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._changed.set)


class BridgeLogHandler(logging.Handler):
    def __init__(self, buffer: BridgeLogBuffer):
        super().__init__(level=logging.DEBUG)
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append_record(record)
        except Exception:
            self.handleError(record)


class ShellWebUI:
    def __init__(self, manager: Any, *, host: str, port: int, access_password: str = ""):
        self.manager = manager
        self.host = host
        self.requested_port = int(port)
        self.port = int(port)
        self._access_password = str(access_password or "").strip()
        self._auth_required = bool(self._access_password)
        self._session_timeout = 3600
        self._session_max_lifetime = 86400
        self._session_cookie_name = "rocketcat_webui_token"
        self._sessions: dict[str, dict[str, float]] = {}
        self._session_lock = asyncio.Lock()
        self._failed_attempts: dict[str, list[float]] = {}
        self._attempt_lock = asyncio.Lock()
        self._server: uvicorn.Server | None = None
        self._server_task: asyncio.Task | None = None
        self._bound_socket: socket.socket | None = None
        self._log_buffer = BridgeLogBuffer(max_entries=5000)
        self._log_handler = BridgeLogHandler(self._log_buffer)
        self._file_root = Path(self.manager.layout.project_root).resolve()
        self._terminal_lock = asyncio.Lock()
        self._terminal_sessions: dict[str, dict[str, Any]] = {}
        self._terminal_order: list[str] = []
        self._app = FastAPI(title="RocketCat Shell", version=__version__)
        self._static_dir = Path(__file__).resolve().parent / "static"
        self._login_file = self._static_dir / "login.html"
        self._setup_routes()

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    async def _apply_access_password(self, password: str) -> None:
        next_password = str(password or "").strip()
        next_auth_required = bool(next_password)
        changed = (
            next_password != self._access_password
            or next_auth_required != self._auth_required
        )
        self._access_password = next_password
        self._auth_required = next_auth_required
        if not changed:
            return

        async with self._session_lock:
            self._sessions.clear()
        async with self._attempt_lock:
            self._failed_attempts.clear()

    def _setup_routes(self) -> None:
        @self._app.middleware("http")
        async def _disable_cache(request: Request, call_next):
            path = request.url.path or "/"
            if self._auth_required and path.startswith("/api/") and path not in {"/api/login"}:
                if not await self._is_request_authenticated(request):
                    return JSONResponse(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        content={"detail": "请先登录管理 WebUI"},
                    )

            response = await call_next(request)
            if path == "/" or path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

        self._app.mount(
            "/static",
            StaticFiles(directory=str(self._static_dir)),
            name="static",
        )
        self._app.add_api_route(
            "/_rocketcat/media/{bot_id}/{token}/{filename}",
            self._handle_published_media,
            methods=["GET"],
        )
        self._app.add_api_route("/", self._handle_index, methods=["GET"])
        self._app.add_api_route("/api/status", self._handle_status, methods=["GET"])
        self._app.add_api_route("/api/diagnostics", self._handle_diagnostics, methods=["GET"])
        self._app.add_api_route("/api/login", self._handle_login, methods=["POST"])
        self._app.add_api_route("/api/logout", self._handle_logout, methods=["POST"])
        self._app.add_api_route("/api/basic-info", self._handle_basic_info, methods=["GET"])
        self._app.add_api_route("/api/basic-info/avatar", self._handle_basic_info_avatar, methods=["GET"])
        self._app.add_api_route(
            "/api/basic-info/server-avatar",
            self._handle_basic_info_server_avatar,
            methods=["GET"],
        )
        self._app.add_api_route("/api/settings", self._handle_settings, methods=["GET"])
        self._app.add_api_route("/api/settings", self._handle_update_settings, methods=["PUT"])
        self._app.add_api_route(
            "/api/settings/export-config",
            self._handle_export_configuration,
            methods=["GET"],
        )
        self._app.add_api_route(
            "/api/settings/import-config",
            self._handle_import_configuration,
            methods=["POST"],
        )
        self._app.add_api_route(
            "/api/settings/rebuild-message-indexes",
            self._handle_rebuild_message_indexes,
            methods=["POST"],
        )
        self._app.add_api_route("/api/logs", self._handle_logs, methods=["GET"])
        self._app.add_api_route("/api/logs/clear", self._handle_clear_logs, methods=["POST"])
        self._app.add_api_route("/api/terminal/list", self._handle_list_terminals, methods=["GET"])
        self._app.add_api_route("/api/terminal/create", self._handle_create_terminal, methods=["POST"])
        self._app.add_api_route("/api/terminal/{terminal_id}/close", self._handle_close_terminal, methods=["POST"])
        self._app.add_api_route("/api/terminal/order", self._handle_update_terminal_order, methods=["PUT"])
        self._app.add_api_websocket_route("/api/ws/terminal/{terminal_id}", self._handle_terminal_websocket)
        self._app.add_api_route("/api/files", self._handle_list_files, methods=["GET"])
        self._app.add_api_route("/api/files/read", self._handle_read_file, methods=["POST"])
        self._app.add_api_route("/api/files/write", self._handle_write_file, methods=["POST"])
        self._app.add_api_route("/api/files/create", self._handle_create_file_item, methods=["POST"])
        self._app.add_api_route("/api/files/upload", self._handle_upload_files, methods=["POST"])
        self._app.add_api_route("/api/files/delete", self._handle_delete_file_items, methods=["POST"])
        self._app.add_api_route("/api/files/move", self._handle_move_file_items, methods=["POST"])
        self._app.add_api_route("/api/files/rename", self._handle_rename_file_item, methods=["POST"])
        self._app.add_api_route("/api/files/preview", self._handle_preview_file_item, methods=["GET"])
        self._app.add_api_route("/api/files/download", self._handle_download_file_item, methods=["GET"])
        self._app.add_api_route("/api/files/download", self._handle_download_file_items, methods=["POST"])
        self._app.add_api_route("/api/bots", self._handle_list_bots, methods=["GET"])
        self._app.add_api_route("/api/bots", self._handle_create_bot, methods=["POST"])
        self._app.add_api_route(
            "/api/bots/{bot_id}",
            self._handle_update_bot,
            methods=["PUT"],
        )
        self._app.add_api_route(
            "/api/bots/{bot_id}",
            self._handle_delete_bot,
            methods=["DELETE"],
        )
        self._app.add_api_route(
            "/api/bots/{bot_id}/user-mappings",
            self._handle_list_user_mappings,
            methods=["GET"],
        )
        self._app.add_api_route(
            "/api/bots/{bot_id}/user-mappings/{user_id}",
            self._handle_update_user_mapping,
            methods=["PUT"],
        )
        self._app.add_api_route(
            "/api/bots/{bot_id}/user-mappings/{user_id}",
            self._handle_delete_user_mapping,
            methods=["DELETE"],
        )
        self._app.add_api_route("/api/plugins", self._handle_list_plugins, methods=["GET"])
        self._app.add_api_route(
            "/api/plugins/{plugin_id}",
            self._handle_get_plugin,
            methods=["GET"],
        )
        self._app.add_api_route(
            "/api/plugins/{plugin_id}/config",
            self._handle_update_plugin_config,
            methods=["PUT"],
        )
        self._app.add_api_route(
            "/api/plugins/{plugin_id}/enabled",
            self._handle_set_plugin_enabled,
            methods=["PUT"],
        )
        self._app.add_api_route(
            "/api/plugins/{plugin_id}/reload",
            self._handle_reload_plugin,
            methods=["POST"],
        )
        self._app.add_api_route(
            "/api/plugins/{plugin_id}/logo",
            self._handle_plugin_logo,
            methods=["GET"],
        )
        self._app.add_api_route(
            "/api/plugins/{plugin_id}",
            self._handle_uninstall_plugin,
            methods=["DELETE"],
        )

    async def start(self) -> None:
        if self._server_task is not None and not self._server_task.done():
            return

        self._attach_log_handler()
        try:
            bound_socket, selected_port, fallback_reason = self._acquire_start_socket(
                self.host,
                self.requested_port,
            )
            self._bound_socket = bound_socket
            self.port = selected_port
            config = uvicorn.Config(
                app=self._app,
                host=self.host,
                port=self.port,
                log_level="warning",
                loop="asyncio",
                lifespan="on",
            )
            self._server = uvicorn.Server(config)
            self._server_task = asyncio.create_task(
                self._server.serve(sockets=[bound_socket])
            )

            for _ in range(50):
                if getattr(self._server, "started", False):
                    if hasattr(self.manager, "set_webui_runtime"):
                        self.manager.set_webui_runtime(
                            host=self.host,
                            requested_port=self.requested_port,
                            actual_port=self.port,
                        )
                    if fallback_reason:
                        logger.warning(
                            "[RocketChatOneBotBridge] 独立WebUI请求端口 %s 不可用，已自动回退到 %s。原因: %s",
                            self.requested_port,
                            self.port,
                            fallback_reason,
                        )
                    logger.info(
                        f"[RocketChatOneBotBridge] 独立WebUI已启动: http://{self.host}:{self.port}/"
                    )
                    return
                if self._server_task.done():
                    error = self._server_task.exception()
                    await self._cleanup_failed_start(reset_logs=True)
                    if error is None:
                        raise RuntimeError("独立WebUI启动失败: 未知错误")
                    raise RuntimeError(f"独立WebUI启动失败: {error}") from error
                await asyncio.sleep(0.1)

            logger.warning(
                f"[RocketChatOneBotBridge] 独立WebUI启动耗时较长，仍在后台启动中: http://{self.host}:{self.port}/"
            )
            if hasattr(self.manager, "set_webui_runtime"):
                self.manager.set_webui_runtime(
                    host=self.host,
                    requested_port=self.requested_port,
                    actual_port=self.port,
                )
        except Exception:
            await self._cleanup_failed_start(reset_logs=True)
            raise

    async def stop(self) -> None:
        if self._server is None and self._server_task is None and self._bound_socket is None:
            return

        if self._server is not None:
            self._server.should_exit = True
        if self._server_task is not None:
            try:
                await self._server_task
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                logger.warning(f"[RocketChatOneBotBridge] 独立WebUI停止时出现异常: {exc!r}")
        self._server = None
        self._server_task = None
        if self._bound_socket is not None:
            try:
                self._bound_socket.close()
            finally:
                self._bound_socket = None
        await self._close_all_terminal_sessions()
        self._detach_log_handler()
        self._log_buffer.clear()
        if hasattr(self.manager, "clear_webui_runtime"):
            self.manager.clear_webui_runtime()
        logger.info("[RocketChatOneBotBridge] 独立WebUI已停止。")

    def _acquire_start_socket(
        self,
        host: str,
        preferred_port: int,
    ) -> tuple[socket.socket, int, str | None]:
        candidates = [preferred_port, 5751, 0]
        seen: set[int] = set()
        preferred_error: OSError | None = None

        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                sock = self._bind_socket(host, candidate)
                actual_port = int(sock.getsockname()[1])
                if candidate == preferred_port:
                    return sock, actual_port, None
                reason = str(preferred_error) if preferred_error is not None else "请求端口不可用"
                return sock, actual_port, reason
            except OSError as exc:
                if candidate == preferred_port:
                    preferred_error = exc

        raise RuntimeError("独立WebUI无法绑定任何候选端口")

    def _bind_socket(self, host: str, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, int(port)))
            sock.listen(128)
            sock.setblocking(False)
            return sock
        except Exception:
            sock.close()
            raise

    async def _cleanup_failed_start(self, *, reset_logs: bool = False) -> None:
        self._server = None
        self._server_task = None
        if self._bound_socket is not None:
            try:
                self._bound_socket.close()
            finally:
                self._bound_socket = None
        if reset_logs:
            self._detach_log_handler()
            self._log_buffer.clear()

    async def _cleanup_sessions_locked(self) -> None:
        now = time.time()
        expired_tokens = []
        for token, session in self._sessions.items():
            created_at = float(session.get("created_at") or 0.0)
            last_active = float(session.get("last_active") or 0.0)
            if now - created_at > self._session_max_lifetime:
                expired_tokens.append(token)
                continue
            if now - last_active > self._session_timeout:
                expired_tokens.append(token)

        for token in expired_tokens:
            self._sessions.pop(token, None)

    async def _cleanup_failed_attempts_locked(self) -> None:
        now = time.time()
        expired_clients = []
        for client_ip, attempts in self._failed_attempts.items():
            recent_attempts = [attempt for attempt in attempts if now - attempt < 300]
            if recent_attempts:
                self._failed_attempts[client_ip] = recent_attempts
            else:
                expired_clients.append(client_ip)

        for client_ip in expired_clients:
            self._failed_attempts.pop(client_ip, None)

    async def _check_rate_limit(self, client_ip: str) -> bool:
        async with self._attempt_lock:
            await self._cleanup_failed_attempts_locked()
            attempts = self._failed_attempts.get(client_ip, [])
            return len(attempts) < 5

    async def _record_failed_attempt(self, client_ip: str) -> None:
        async with self._attempt_lock:
            attempts = self._failed_attempts.setdefault(client_ip, [])
            attempts.append(time.time())

    def _extract_session_token(self, request: Request) -> str:
        return str(request.cookies.get(self._session_cookie_name, "") or "").strip()

    async def _is_request_authenticated(self, request: Request) -> bool:
        if not self._auth_required:
            return True

        token = self._extract_session_token(request)
        if not token:
            return False

        async with self._session_lock:
            await self._cleanup_sessions_locked()
            session = self._sessions.get(token)
            if not session:
                return False
            session["last_active"] = time.time()
            return True

    def _get_client_ip(self, request: Request) -> str:
        if request.client and request.client.host:
            return str(request.client.host)
        return "unknown"

    def _extract_websocket_session_token(self, websocket: WebSocket) -> str:
        return str(websocket.cookies.get(self._session_cookie_name, "") or "").strip()

    async def _is_websocket_authenticated(self, websocket: WebSocket) -> bool:
        if not self._auth_required:
            return True

        token = self._extract_websocket_session_token(websocket)
        if not token:
            return False

        async with self._session_lock:
            await self._cleanup_sessions_locked()
            session = self._sessions.get(token)
            if not session:
                return False
            session["last_active"] = time.time()
            return True

    def _get_terminal_command(self) -> list[str]:
        shell = str(os.environ.get("SHELL") or "").strip()
        if shell:
            shell_path = Path(shell)
            if shell_path.exists() or shutil.which(shell):
                return [shell]

        for candidate in ("/bin/bash", "/bin/sh", "sh"):
            if Path(candidate).exists() or shutil.which(candidate):
                return [candidate]
        return ["/bin/sh"]

    def _build_terminal_environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("LANG", "C.UTF-8")
        env.setdefault("LC_ALL", env.get("LANG", "C.UTF-8"))
        return env

    def _set_terminal_pty_size(self, master_fd: int, *, cols: int, rows: int) -> None:
        if fcntl is None or termios is None:
            return
        size = struct.pack("HHHH", int(rows), int(cols), 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, size)

    def _spawn_terminal_pty(self, *, cols: int, rows: int) -> tuple[subprocess.Popen[bytes], int]:
        if pty is None:
            raise RuntimeError("Linux PTY support is unavailable")

        master_fd, slave_fd = pty.openpty()
        try:
            self._set_terminal_pty_size(master_fd, cols=cols, rows=rows)
            process = subprocess.Popen(
                self._get_terminal_command(),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=str(self._file_root),
                env=self._build_terminal_environment(),
                close_fds=True,
                start_new_session=True,
            )
        finally:
            with contextlib.suppress(OSError):
                os.close(slave_fd)

        with contextlib.suppress(Exception):
            os.set_blocking(master_fd, False)
        return process, master_fd

    def _schedule_terminal_coroutine(self, loop: asyncio.AbstractEventLoop, coro: Any) -> None:
        if loop.is_closed():
            with contextlib.suppress(Exception):
                coro.close()
            return
        loop.call_soon_threadsafe(asyncio.create_task, coro)

    def _trim_terminal_buffer(self, value: str) -> str:
        if len(value) <= _TERMINAL_BUFFER_LIMIT_CHARS:
            return value
        return value[-_TERMINAL_BUFFER_LIMIT_CHARS:]

    def _serialize_terminal_session(self, session: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": session["id"],
            "title": session.get("title") or session["id"],
            "created_at": session.get("created_at", 0),
            "last_access": session.get("last_access", 0),
            "cwd": session.get("cwd", str(self._file_root)),
        }

    async def _create_terminal_session(self, *, cols: int, rows: int) -> dict[str, Any]:
        terminal_id = str(uuid.uuid4())
        now = time.time()
        loop = asyncio.get_running_loop()
        process, master_fd = self._spawn_terminal_pty(cols=cols, rows=rows)
        stop_event = threading.Event()

        session: dict[str, Any] = {
            "id": terminal_id,
            "title": terminal_id,
            "backend": "linux-pty",
            "process": process,
            "pty_fd": master_fd,
            "pty_stop": stop_event,
            "created_at": now,
            "last_access": now,
            "cwd": str(self._file_root),
            "cols": cols,
            "rows": rows,
            "buffer": "",
            "sockets": set(),
            "closing": False,
            "seen_output": False,
            "loop": loop,
        }

        async with self._terminal_lock:
            self._terminal_sessions[terminal_id] = session
            self._terminal_order.append(terminal_id)

        reader_thread = threading.Thread(
            target=self._read_terminal_pty_output,
            args=(terminal_id, process, master_fd, stop_event, loop),
            name=f"rocketcat-webui-pty-{terminal_id[:8]}",
            daemon=True,
        )
        session["reader_thread"] = reader_thread
        reader_thread.start()
        return session

    def _read_terminal_pty_output(
        self,
        terminal_id: str,
        process: subprocess.Popen[bytes],
        master_fd: int,
        stop_event: threading.Event,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        exit_code: int | None = None
        try:
            while not stop_event.is_set():
                ready = []
                if select is not None:
                    try:
                        ready, _, _ = select.select([master_fd], [], [], 0.1)
                    except (OSError, ValueError):
                        break
                if ready:
                    try:
                        payload = os.read(master_fd, 4096)
                    except BlockingIOError:
                        continue
                    except OSError:
                        break
                    if not payload:
                        break
                    data = payload.decode("utf-8", errors="replace")
                    self._schedule_terminal_coroutine(
                        loop,
                        self._broadcast_terminal_output(terminal_id, data),
                    )
                    continue
                if process.poll() is not None:
                    break
            exit_code = process.poll()
        except Exception as exc:
            logger.warning("[RocketCatShell] WebUI PTY reader failed: id=%s err=%r", terminal_id, exc)
        finally:
            if exit_code is None:
                with contextlib.suppress(Exception):
                    exit_code = process.wait(timeout=0)
            self._schedule_terminal_coroutine(
                loop,
                self._finish_terminal_session(terminal_id, exit_code),
            )

    async def _broadcast_terminal_output(self, terminal_id: str, data: str) -> None:
        if not data:
            return

        async with self._terminal_lock:
            session = self._terminal_sessions.get(terminal_id)
            if not session:
                return
            if not session.get("seen_output"):
                data = data.lstrip("\r\n")
                session["seen_output"] = True
            if not data:
                return
            session["buffer"] = self._trim_terminal_buffer(str(session.get("buffer") or "") + data)
            session["last_access"] = time.time()
            sockets = list(session.get("sockets") or [])

        failed_sockets = []
        message = {"type": "output", "data": data}
        for websocket in sockets:
            try:
                await websocket.send_json(message)
            except Exception:
                failed_sockets.append(websocket)

        if failed_sockets:
            async with self._terminal_lock:
                session = self._terminal_sessions.get(terminal_id)
                if session:
                    for websocket in failed_sockets:
                        session["sockets"].discard(websocket)

    async def _write_terminal_input(self, terminal_id: str, data: str) -> None:
        async with self._terminal_lock:
            session = self._terminal_sessions.get(terminal_id)
            if not session:
                return
            process: subprocess.Popen[bytes] | None = session.get("process")
            master_fd: int | None = session.get("pty_fd")
            session["last_access"] = time.time()

        if process is None or master_fd is None or process.poll() is not None:
            return
        try:
            await asyncio.to_thread(os.write, master_fd, str(data).encode("utf-8", errors="replace"))
        except OSError:
            await self._finish_terminal_session(terminal_id, process.poll())

    async def _resize_terminal(self, terminal_id: str, *, cols: int, rows: int) -> None:
        cols = max(20, min(int(cols or _TERMINAL_DEFAULT_COLS), 300))
        rows = max(5, min(int(rows or _TERMINAL_DEFAULT_ROWS), 120))
        async with self._terminal_lock:
            session = self._terminal_sessions.get(terminal_id)
            if not session:
                return
            session["cols"] = cols
            session["rows"] = rows
            master_fd: int | None = session.get("pty_fd")

        if master_fd is not None:
            with contextlib.suppress(Exception):
                self._set_terminal_pty_size(master_fd, cols=cols, rows=rows)

    async def _finish_terminal_session(self, terminal_id: str, exit_code: int | None) -> None:
        async with self._terminal_lock:
            session = self._terminal_sessions.pop(terminal_id, None)
            self._terminal_order = [item for item in self._terminal_order if item != terminal_id]
            if not session:
                return
            sockets = list(session.get("sockets") or [])
            session["sockets"].clear()
            master_fd: int | None = session.get("pty_fd")
            stop_event: threading.Event | None = session.get("pty_stop")

        if stop_event:
            stop_event.set()
        if master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(master_fd)

        message = {
            "type": "exit",
            "data": f"\r\n[terminal exited with code {exit_code}]\r\n",
            "exit_code": exit_code,
            "id": terminal_id,
        }
        for websocket in sockets:
            with contextlib.suppress(Exception):
                await websocket.send_json(message)
            with contextlib.suppress(Exception):
                await websocket.close()

    async def _close_terminal_session(self, terminal_id: str) -> bool:
        async with self._terminal_lock:
            session = self._terminal_sessions.pop(terminal_id, None)
            self._terminal_order = [item for item in self._terminal_order if item != terminal_id]
            if not session:
                return False
            session["closing"] = True
            process: subprocess.Popen[bytes] | None = session.get("process")
            master_fd: int | None = session.get("pty_fd")
            stop_event: threading.Event | None = session.get("pty_stop")
            sockets = list(session.get("sockets") or [])
            session["sockets"].clear()
            reader_thread: threading.Thread | None = session.get("reader_thread")

        if stop_event:
            stop_event.set()
        if master_fd is not None:
            with contextlib.suppress(OSError):
                os.close(master_fd)

        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError, RuntimeError, OSError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.to_thread(process.wait, timeout=2.0)
            except Exception:
                with contextlib.suppress(ProcessLookupError, RuntimeError, OSError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(process.wait, timeout=1.0)

        if reader_thread and reader_thread.is_alive() and reader_thread is not threading.current_thread():
            with contextlib.suppress(Exception):
                await asyncio.to_thread(reader_thread.join, 1.0)

        for websocket in sockets:
            with contextlib.suppress(Exception):
                await websocket.close()
        return True

    async def _close_all_terminal_sessions(self) -> None:
        async with self._terminal_lock:
            terminal_ids = list(self._terminal_sessions)
        for terminal_id in terminal_ids:
            await self._close_terminal_session(terminal_id)

    async def _handle_list_terminals(self) -> dict[str, Any]:
        async with self._terminal_lock:
            ordered_ids = [item for item in self._terminal_order if item in self._terminal_sessions]
            missing_ids = [item for item in self._terminal_sessions if item not in ordered_ids]
            self._terminal_order = ordered_ids + missing_ids
            items = [
                self._serialize_terminal_session(self._terminal_sessions[terminal_id])
                for terminal_id in self._terminal_order
            ]
        return {"items": items}

    async def _handle_create_terminal(
        self,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        payload = payload or {}
        cols = max(20, min(int(payload.get("cols") or _TERMINAL_DEFAULT_COLS), 240))
        rows = max(5, min(int(payload.get("rows") or _TERMINAL_DEFAULT_ROWS), 80))
        try:
            session = await self._create_terminal_session(cols=cols, rows=rows)
        except Exception as exc:
            logger.error("[RocketCatShell] WebUI 终端创建失败: %r", exc)
            raise HTTPException(status_code=500, detail="创建终端失败") from exc
        return self._serialize_terminal_session(session)

    async def _handle_close_terminal(self, terminal_id: str) -> dict[str, Any]:
        await self._close_terminal_session(terminal_id)
        return {"ok": True}

    async def _handle_update_terminal_order(
        self,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        payload = payload or {}
        raw_order = payload.get("order") or []
        if not isinstance(raw_order, list):
            raise HTTPException(status_code=400, detail="终端顺序必须是列表")

        async with self._terminal_lock:
            known_ids = set(self._terminal_sessions)
            next_order: list[str] = []
            for raw_id in raw_order:
                terminal_id = str(raw_id or "").strip()
                if terminal_id in known_ids and terminal_id not in next_order:
                    next_order.append(terminal_id)
            next_order.extend(
                terminal_id
                for terminal_id in self._terminal_order
                if terminal_id in known_ids and terminal_id not in next_order
            )
            next_order.extend(
                terminal_id
                for terminal_id in self._terminal_sessions
                if terminal_id not in next_order
            )
            self._terminal_order = next_order
            items = [
                self._serialize_terminal_session(self._terminal_sessions[terminal_id])
                for terminal_id in self._terminal_order
            ]
        return {"items": items}

    async def _handle_terminal_websocket(self, websocket: WebSocket, terminal_id: str) -> None:
        if not await self._is_websocket_authenticated(websocket):
            await websocket.close(code=1008)
            return

        async with self._terminal_lock:
            session = self._terminal_sessions.get(terminal_id)
            if not session:
                await websocket.close(code=1008)
                return

        await websocket.accept()
        buffered_output = ""
        async with self._terminal_lock:
            session = self._terminal_sessions.get(terminal_id)
            if not session:
                await websocket.close(code=1008)
                return
            session["sockets"].add(websocket)
            session["last_access"] = time.time()
            buffered_output = str(session.get("buffer") or "")

        if buffered_output:
            await websocket.send_json({"type": "output", "data": buffered_output})

        try:
            while True:
                raw_message = await websocket.receive_text()
                try:
                    message = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                message_type = message.get("type")
                if message_type == "input":
                    await self._write_terminal_input(terminal_id, str(message.get("data") or ""))
                    continue
                if message_type == "resize":
                    try:
                        cols = int(message.get("cols") or _TERMINAL_DEFAULT_COLS)
                        rows = int(message.get("rows") or _TERMINAL_DEFAULT_ROWS)
                    except (TypeError, ValueError):
                        continue
                    await self._resize_terminal(terminal_id, cols=cols, rows=rows)
        except WebSocketDisconnect:
            pass
        finally:
            async with self._terminal_lock:
                session = self._terminal_sessions.get(terminal_id)
                if session:
                    session["sockets"].discard(websocket)

    def _attach_log_handler(self) -> None:
        if self._log_handler not in logger.handlers:
            logger.addHandler(self._log_handler)

    def _detach_log_handler(self) -> None:
        if self._log_handler in logger.handlers:
            logger.removeHandler(self._log_handler)

    async def _handle_index(self, request: Request) -> FileResponse:
        if self._auth_required and not await self._is_request_authenticated(request):
            return FileResponse(self._login_file)
        return FileResponse(self._static_dir / "index.html")

    async def _handle_published_media(
        self,
        bot_id: str,
        token: str,
        filename: str,
    ) -> FileResponse:
        entry = self.manager.media_publication.resolve(
            bot_id=bot_id,
            token=token,
            filename=filename,
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="媒体不存在或令牌已失效")
        return FileResponse(
            entry.file_path,
            media_type=entry.content_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def _handle_login(self, request: Request, payload: dict[str, Any]) -> JSONResponse:
        if not self._auth_required:
            return JSONResponse({"ok": True, "auth_required": False})

        password = str(payload.get("password", "")).strip()
        if not password:
            raise HTTPException(status_code=400, detail="访问密码不能为空")

        client_ip = self._get_client_ip(request)
        if not await self._check_rate_limit(client_ip):
            raise HTTPException(status_code=429, detail="尝试次数过多，请 5 分钟后再试")

        if not secrets.compare_digest(password, self._access_password):
            await self._record_failed_attempt(client_ip)
            await asyncio.sleep(0.8)
            raise HTTPException(status_code=401, detail="访问密码错误")

        token = secrets.token_urlsafe(32)
        now = time.time()
        async with self._session_lock:
            await self._cleanup_sessions_locked()
            self._sessions[token] = {
                "created_at": now,
                "last_active": now,
            }

        response = JSONResponse({"ok": True, "auth_required": True})
        response.set_cookie(
            key=self._session_cookie_name,
            value=token,
            max_age=self._session_max_lifetime,
            expires=self._session_max_lifetime,
            path="/",
            httponly=True,
            samesite="lax",
            secure=False,
        )
        return response

    async def _handle_logout(self, request: Request) -> JSONResponse:
        token = self._extract_session_token(request)
        if token:
            async with self._session_lock:
                self._sessions.pop(token, None)

        response = JSONResponse({"ok": True, "detail": "已退出登录"})
        response.delete_cookie(key=self._session_cookie_name, path="/")
        return response

    async def _handle_status(self) -> dict[str, Any]:
        return await self.manager.get_webui_state()

    async def _handle_diagnostics(self) -> dict[str, Any]:
        return await self.manager.get_diagnostics_state()

    async def _handle_basic_info(self) -> dict[str, Any]:
        return await self.manager.get_basic_info_state()

    async def _handle_basic_info_avatar(self, bot_id: str = Query(default="")) -> Response:
        avatar = await self.manager.get_basic_info_avatar_content(bot_id)
        if avatar is None:
            raise HTTPException(status_code=404, detail="基础信息头像不存在")
        content, content_type = avatar
        return Response(content=content, media_type=content_type)

    async def _handle_basic_info_server_avatar(self, bot_id: str = Query(default="")) -> Response:
        avatar = await self.manager.get_basic_info_server_avatar_content(bot_id)
        if avatar is None:
            raise HTTPException(status_code=404, detail="基础信息服务器头像不存在")
        content, content_type = avatar
        return Response(content=content, media_type=content_type)

    async def _handle_settings(self) -> dict[str, Any]:
        return await self.manager.get_settings_state()

    async def _handle_update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            updated = await self.manager.update_settings(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(f"[RocketCatShell] 更新 shell 设置失败: {exc!r}")
            raise HTTPException(status_code=500, detail="更新 shell 设置失败") from exc

        if hasattr(self.manager, "settings") and getattr(self.manager, "settings") is not None:
            await self._apply_access_password(self.manager.settings.webui_access_password)
        return updated

    async def _handle_export_configuration(self) -> dict[str, Any]:
        try:
            return await self.manager.export_configuration()
        except Exception as exc:
            logger.error(f"[RocketCatShell] 导出配置失败: {exc!r}")
            raise HTTPException(status_code=500, detail="导出配置失败") from exc

    async def _handle_import_configuration(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await self.manager.import_configuration(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(f"[RocketCatShell] 导入配置失败: {exc!r}")
            raise HTTPException(status_code=500, detail="导入配置失败") from exc

        if hasattr(self.manager, "settings") and getattr(self.manager, "settings") is not None:
            await self._apply_access_password(self.manager.settings.webui_access_password)
        return {"ok": True, "result": result}

    async def _handle_rebuild_message_indexes(self) -> dict[str, Any]:
        try:
            result = await self.manager.rebuild_message_indexes()
        except Exception as exc:
            logger.error(f"[RocketCatShell] 手动整理消息映射窗口失败: {exc!r}")
            raise HTTPException(status_code=500, detail="手动整理消息映射窗口失败") from exc
        return {"ok": True, "result": result}

    async def _handle_logs(
        self,
        after_id: int = Query(default=0, ge=0),
        wait: float = Query(default=0.0, ge=0.0, le=30.0),
    ) -> dict[str, Any]:
        latest_id = self._log_buffer.latest_id()
        reset_cursor = after_id > latest_id
        effective_after_id = 0 if reset_cursor else after_id
        initial_version = self._log_buffer.version
        if wait > 0 and not reset_cursor and effective_after_id >= latest_id:
            await self._log_buffer.wait_for_change(initial_version, timeout=wait)
            latest_id = self._log_buffer.latest_id()
            reset_cursor = after_id > latest_id
            effective_after_id = 0 if reset_cursor else after_id
        return {
            "items": self._log_buffer.get_entries(after_id=effective_after_id),
            "max_entries": self._log_buffer.max_entries,
            "latest_id": latest_id,
            "reset": reset_cursor,
        }

    async def _handle_clear_logs(self) -> dict[str, Any]:
        return {
            "ok": True,
            "cleared": self._log_buffer.clear(),
            "max_entries": self._log_buffer.max_entries,
        }

    async def _handle_list_files(self, path: str = Query(default="")) -> dict[str, Any]:
        target_path = self._resolve_file_manager_path(path)
        if not target_path.exists():
            raise HTTPException(status_code=404, detail="文件或目录不存在")
        if not target_path.is_dir():
            raise HTTPException(status_code=400, detail="目标路径不是目录")

        items: list[dict[str, Any]] = []
        try:
            children = sorted(
                target_path.iterdir(),
                key=lambda item: (not item.is_dir(), item.name.lower()),
            )
        except OSError as exc:
            logger.warning("[RocketCatShell] 文件管理目录读取失败: path=%s err=%r", target_path, exc)
            raise HTTPException(status_code=500, detail="读取目录失败") from exc

        for child in children:
            try:
                resolved_child = child.resolve()
                if resolved_child != self._file_root and not resolved_child.is_relative_to(self._file_root):
                    continue
                stat_result = child.stat()
            except OSError:
                continue
            items.append(self._serialize_file_item(child, stat_result=stat_result))

        relative_path = self._file_manager_relative_path(target_path)
        parent_path = ""
        can_go_up = target_path != self._file_root
        if can_go_up:
            parent_path = self._file_manager_relative_path(target_path.parent)

        return {
            "path": relative_path,
            "parent_path": parent_path,
            "can_go_up": can_go_up,
            "root_path": str(self._file_root),
            "items": items,
        }

    async def _handle_read_file(
        self,
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        payload = payload or {}
        target_path = self._resolve_file_manager_path(str(payload.get("path") or ""))
        if not target_path.exists():
            raise HTTPException(status_code=404, detail="文件不存在")
        if not target_path.is_file():
            raise HTTPException(status_code=400, detail="目标路径不是文件")

        stat_result = target_path.stat()
        if self._is_sensitive_file_manager_path(target_path):
            await self._verify_file_manager_password(
                request,
                str(payload.get("password") or ""),
            )

        if target_path.suffix.lower() in _BINARY_PREVIEW_EXTENSIONS:
            raise HTTPException(status_code=415, detail="当前阶段仅支持文本文件预览")

        try:
            with target_path.open("rb") as handle:
                preview_bytes = handle.read(_FILE_PREVIEW_LIMIT_BYTES)
                has_more = bool(handle.read(1))
        except OSError as exc:
            logger.warning("[RocketCatShell] 文件管理读取文件失败: path=%s err=%r", target_path, exc)
            raise HTTPException(status_code=500, detail="读取文件失败") from exc

        if self._looks_like_binary(preview_bytes):
            raise HTTPException(status_code=415, detail="当前阶段仅支持文本文件预览")

        try:
            decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
            content = decoder.decode(preview_bytes, final=not has_more)
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=415, detail="当前阶段仅支持 UTF-8 文本文件预览") from exc

        return {
            "path": self._file_manager_relative_path(target_path),
            "name": target_path.name,
            "size": stat_result.st_size,
            "mtime": datetime.fromtimestamp(stat_result.st_mtime).isoformat(timespec="seconds"),
            "content": content,
            "encoding": "utf-8",
            "truncated": has_more,
            "requires_password": self._is_sensitive_file_manager_path(target_path),
            "is_protected": self._is_protected_file_manager_path(target_path),
            "can_edit": self._can_edit_file_manager_path(target_path, stat_result=stat_result, truncated=has_more),
        }

    async def _handle_write_file(
        self,
        request: Request,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        payload = payload or {}
        target_path = self._resolve_file_manager_path(str(payload.get("path") or ""))
        if not target_path.exists():
            raise HTTPException(status_code=404, detail="文件不存在")
        if not target_path.is_file():
            raise HTTPException(status_code=400, detail="目标路径不是文件")
        if self._is_protected_file_manager_path(target_path):
            raise HTTPException(status_code=403, detail="RocketCatShell 核心源码和内置插件源码只允许查看，不能修改")

        stat_result = target_path.stat()
        if not self._can_edit_file_manager_path(target_path, stat_result=stat_result, truncated=False):
            raise HTTPException(status_code=415, detail="当前文件不支持在线编辑")

        try:
            existing_bytes = target_path.read_bytes()
        except OSError as exc:
            logger.warning("[RocketCatShell] 文件管理读取待保存文件失败: path=%s err=%r", target_path, exc)
            raise HTTPException(status_code=500, detail="读取文件失败") from exc
        if self._looks_like_binary(existing_bytes):
            raise HTTPException(status_code=415, detail="当前文件不支持在线编辑")
        try:
            existing_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=415, detail="当前阶段仅支持 UTF-8 文本文件编辑") from exc

        if self._is_sensitive_file_manager_path(target_path):
            await self._verify_file_manager_password(
                request,
                str(payload.get("password") or ""),
            )

        content = payload.get("content")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="保存内容必须是文本")

        encoded_content = content.encode("utf-8")
        if len(encoded_content) > _FILE_EDIT_LIMIT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"在线编辑内容不能超过 {_FILE_EDIT_LIMIT_BYTES // (1024 * 1024)} MiB",
            )

        temp_path = target_path.with_name(f".{target_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_bytes(encoded_content)
            temp_path.replace(target_path)
            stat_result = target_path.stat()
        except OSError as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            logger.warning("[RocketCatShell] 文件管理保存文件失败: path=%s err=%r", target_path, exc)
            raise HTTPException(status_code=500, detail="保存文件失败") from exc

        return {
            "ok": True,
            "path": self._file_manager_relative_path(target_path),
            "name": target_path.name,
            "size": stat_result.st_size,
            "mtime": datetime.fromtimestamp(stat_result.st_mtime).isoformat(timespec="seconds"),
            "item": self._serialize_file_item(target_path, stat_result=stat_result),
        }

    async def _handle_create_file_item(
        self,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        payload = payload or {}
        item_type = str(payload.get("type") or payload.get("kind") or "file").strip().lower()
        if item_type not in {"file", "directory"}:
            raise HTTPException(status_code=400, detail="新建类型必须是 file 或 directory")

        raw_path = str(payload.get("path") or "").strip()
        if not raw_path:
            raise HTTPException(status_code=400, detail="新建路径不能为空")

        target_path = self._resolve_file_manager_path(raw_path)
        if target_path == self._file_root:
            raise HTTPException(status_code=400, detail="不能覆盖 RocketCatShell 根目录")
        if self._is_protected_mutation_path(target_path):
            raise HTTPException(status_code=403, detail="RocketCatShell 核心源码和内置插件源码不允许修改")

        parent_path = target_path.parent.resolve()
        if parent_path != self._file_root and not parent_path.is_relative_to(self._file_root):
            raise HTTPException(status_code=403, detail="文件路径不能越过 RocketCatShell 根目录")
        if parent_path.exists() and not parent_path.is_dir():
            raise HTTPException(status_code=400, detail="父级路径不是目录")
        if item_type == "file" and not parent_path.exists():
            raise HTTPException(status_code=400, detail="父目录不存在")
        if target_path.exists():
            raise HTTPException(status_code=409, detail="同名文件或目录已存在")

        try:
            if item_type == "directory":
                target_path.mkdir(parents=True)
            else:
                target_path.write_text("", encoding="utf-8")
            stat_result = target_path.stat()
        except OSError as exc:
            logger.warning("[RocketCatShell] 文件管理新建失败: path=%s err=%r", target_path, exc)
            raise HTTPException(status_code=500, detail="新建失败") from exc

        return {
            "ok": True,
            "item": self._serialize_file_item(target_path, stat_result=stat_result),
        }

    async def _handle_upload_files(
        self,
        path: str = Query(default=""),
        files: list[UploadFile] = FastAPIFile(default=[]),
    ) -> dict[str, Any]:
        target_directory = self._resolve_file_manager_path(path)
        if not target_directory.exists():
            raise HTTPException(status_code=404, detail="上传目录不存在")
        if not target_directory.is_dir():
            raise HTTPException(status_code=400, detail="上传目标不是目录")

        if not files:
            raise HTTPException(status_code=400, detail="请选择要上传的文件")
        if len(files) > _FILE_UPLOAD_MAX_FILES:
            raise HTTPException(
                status_code=413,
                detail=f"单次最多上传 {_FILE_UPLOAD_MAX_FILES} 个文件",
            )

        base_relative_path = self._file_manager_relative_path(target_directory)
        uploaded_items: list[dict[str, Any]] = []
        for upload in files:
            uploaded_relative_path = self._normalize_uploaded_file_name(upload.filename or "")
            combined_relative_path = self._join_file_manager_relative_path(
                base_relative_path,
                uploaded_relative_path,
            )
            requested_path = self._resolve_file_manager_path(combined_relative_path)
            if self._is_protected_mutation_path(requested_path):
                raise HTTPException(status_code=403, detail="RocketCatShell 核心源码和内置插件源码不允许修改")
            parent_path = requested_path.parent.resolve()
            if parent_path != self._file_root and not parent_path.is_relative_to(self._file_root):
                raise HTTPException(status_code=403, detail="上传路径不能越过 RocketCatShell 根目录")
            if parent_path.exists() and not parent_path.is_dir():
                raise HTTPException(status_code=400, detail="上传路径父级不是目录")

            try:
                parent_path.mkdir(parents=True, exist_ok=True)
                resolved_parent_path = parent_path.resolve()
                if (
                    resolved_parent_path != self._file_root
                    and not resolved_parent_path.is_relative_to(self._file_root)
                ):
                    raise HTTPException(status_code=403, detail="上传路径不能越过 RocketCatShell 根目录")
                destination_path = self._deduplicate_upload_path(requested_path)
                await self._write_uploaded_file(upload, destination_path)
                stat_result = destination_path.stat()
            except HTTPException:
                raise
            except OSError as exc:
                logger.warning("[RocketCatShell] 文件管理上传失败: path=%s err=%r", requested_path, exc)
                raise HTTPException(status_code=500, detail="上传文件失败") from exc
            finally:
                await upload.close()

            uploaded_items.append(self._serialize_file_item(destination_path, stat_result=stat_result))

        return {
            "ok": True,
            "uploaded": len(uploaded_items),
            "items": uploaded_items,
        }

    async def _handle_delete_file_items(
        self,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        payload = payload or {}
        raw_paths = payload.get("paths")
        if raw_paths is None:
            raw_paths = [payload.get("path")]
        if not isinstance(raw_paths, list):
            raise HTTPException(status_code=400, detail="删除路径必须是列表")

        target_paths = self._resolve_file_manager_targets(raw_paths, operation="delete")
        if not target_paths:
            raise HTTPException(status_code=400, detail="请选择要删除的项目")

        deleted_items: list[dict[str, Any]] = []
        try:
            for target_path in target_paths:
                if self._is_protected_mutation_path(target_path):
                    raise HTTPException(status_code=403, detail="RocketCatShell 核心源码和内置插件源码不允许删除")
                item_payload = {
                    "path": self._file_manager_relative_path(target_path),
                    "name": target_path.name,
                    "is_directory": target_path.is_dir(),
                }
                if target_path.is_dir() and not target_path.is_symlink():
                    shutil.rmtree(target_path)
                else:
                    target_path.unlink()
                deleted_items.append(item_payload)
        except OSError as exc:
            logger.warning("[RocketCatShell] 文件管理删除失败: err=%r", exc)
            raise HTTPException(status_code=500, detail="删除失败") from exc

        return {
            "ok": True,
            "deleted": len(deleted_items),
            "items": deleted_items,
        }

    async def _handle_move_file_items(
        self,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        payload = payload or {}
        raw_paths = payload.get("paths")
        if raw_paths is None:
            raw_paths = [payload.get("path")]
        if not isinstance(raw_paths, list):
            raise HTTPException(status_code=400, detail="移动路径必须是列表")

        target_paths = self._resolve_file_manager_targets(raw_paths, operation="move")
        if not target_paths:
            raise HTTPException(status_code=400, detail="请选择要移动的项目")

        target_directory = self._resolve_file_manager_path(str(payload.get("target_path") or ""))
        if not target_directory.exists():
            raise HTTPException(status_code=404, detail="目标目录不存在")
        if not target_directory.is_dir():
            raise HTTPException(status_code=400, detail="目标路径不是目录")

        move_plan: list[tuple[Path, Path]] = []
        seen_destinations: set[Path] = set()
        for source_path in target_paths:
            if self._is_protected_mutation_path(source_path):
                raise HTTPException(status_code=403, detail="RocketCatShell 核心源码和内置插件源码不允许移动")
            destination_path = (target_directory / source_path.name).resolve()
            if destination_path != self._file_root and not destination_path.is_relative_to(self._file_root):
                raise HTTPException(status_code=403, detail="移动目标不能越过 RocketCatShell 根目录")
            if self._is_protected_mutation_path(destination_path):
                raise HTTPException(status_code=403, detail="RocketCatShell 核心源码和内置插件源码不允许移动")
            if source_path == target_directory:
                raise HTTPException(status_code=400, detail="不能移动目录到自身")
            if source_path.is_dir() and destination_path.is_relative_to(source_path):
                raise HTTPException(status_code=400, detail="不能移动目录到自身内部")
            if destination_path.exists():
                raise HTTPException(status_code=409, detail=f"目标目录已存在同名项目: {source_path.name}")
            if destination_path in seen_destinations:
                raise HTTPException(status_code=409, detail=f"移动目标存在冲突: {source_path.name}")
            seen_destinations.add(destination_path)
            move_plan.append((source_path, destination_path))

        moved_items: list[dict[str, Any]] = []
        try:
            for source_path, destination_path in move_plan:
                shutil.move(str(source_path), str(destination_path))
                stat_result = destination_path.stat()
                moved_items.append(self._serialize_file_item(destination_path, stat_result=stat_result))
        except OSError as exc:
            logger.warning("[RocketCatShell] 文件管理移动失败: err=%r", exc)
            raise HTTPException(status_code=500, detail="移动失败") from exc

        return {
            "ok": True,
            "moved": len(moved_items),
            "items": moved_items,
            "target_path": self._file_manager_relative_path(target_directory),
        }

    async def _handle_rename_file_item(
        self,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        payload = payload or {}
        source_path = self._resolve_file_manager_path(str(payload.get("path") or payload.get("old_path") or ""))
        if source_path == self._file_root:
            raise HTTPException(status_code=400, detail="不能重命名 RocketCatShell 根目录")
        if not source_path.exists():
            raise HTTPException(status_code=404, detail="文件或目录不存在")
        if self._is_protected_mutation_path(source_path):
            raise HTTPException(status_code=403, detail="RocketCatShell 核心源码和内置插件源码不允许重命名")

        new_name = self._normalize_file_manager_name(
            str(payload.get("name") or payload.get("new_name") or "")
        )
        destination_path = (source_path.parent / new_name).resolve()
        if destination_path != self._file_root and not destination_path.is_relative_to(self._file_root):
            raise HTTPException(status_code=403, detail="重命名目标不能越过 RocketCatShell 根目录")
        if self._is_protected_mutation_path(destination_path):
            raise HTTPException(status_code=403, detail="RocketCatShell 核心源码和内置插件源码不允许重命名")
        if destination_path.exists():
            raise HTTPException(status_code=409, detail="同名文件或目录已存在")

        try:
            source_path.rename(destination_path)
            stat_result = destination_path.stat()
        except OSError as exc:
            logger.warning("[RocketCatShell] 文件管理重命名失败: path=%s err=%r", source_path, exc)
            raise HTTPException(status_code=500, detail="重命名失败") from exc

        return {
            "ok": True,
            "item": self._serialize_file_item(destination_path, stat_result=stat_result),
        }

    async def _handle_preview_file_item(self, path: str = Query(default="")) -> FileResponse:
        target_path = self._resolve_file_manager_path(path)
        if not target_path.exists():
            raise HTTPException(status_code=404, detail="文件不存在")
        if not target_path.is_file():
            raise HTTPException(status_code=400, detail="目标路径不是文件")

        extension = target_path.suffix.lower()
        if extension not in _IMAGE_PREVIEW_EXTENSIONS:
            raise HTTPException(status_code=415, detail="当前仅支持图片文件预览")

        media_type = _IMAGE_PREVIEW_MEDIA_TYPES.get(extension)
        if not media_type:
            media_type = mimetypes.guess_type(target_path.name)[0] or "application/octet-stream"

        return FileResponse(
            target_path,
            media_type=media_type,
            headers={"Cache-Control": "no-store"},
        )

    async def _handle_download_file_item(self, path: str = Query(default="")) -> Response:
        target_path = self._resolve_file_manager_path(path)
        if target_path == self._file_root:
            raise HTTPException(status_code=400, detail="不能直接下载 RocketCatShell 根目录")
        if not target_path.exists():
            raise HTTPException(status_code=404, detail="文件或目录不存在")

        if target_path.is_file() or target_path.is_symlink():
            return FileResponse(
                target_path,
                filename=target_path.name,
                media_type="application/octet-stream",
            )

        archive_buffer = io.BytesIO()
        try:
            with zipfile.ZipFile(
                archive_buffer,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as zip_handle:
                self._write_file_manager_zip_entry(
                    zip_handle,
                    target_path=target_path,
                    archive_name=target_path.name,
                    written_names=set(),
                )
        except OSError as exc:
            logger.warning("[RocketCatShell] 文件管理目录下载打包失败: path=%s err=%r", target_path, exc)
            raise HTTPException(status_code=500, detail="下载打包失败") from exc

        content = archive_buffer.getvalue()
        return Response(
            content=content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{target_path.name}.zip"',
                "Content-Length": str(len(content)),
            },
        )

    async def _handle_download_file_items(
        self,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> Response:
        payload = payload or {}
        raw_paths = payload.get("paths")
        if raw_paths is None:
            raw_paths = [payload.get("path")]
        if not isinstance(raw_paths, list):
            raise HTTPException(status_code=400, detail="下载路径必须是列表")

        target_paths = self._resolve_file_manager_targets(raw_paths, operation="download")
        if not target_paths:
            raise HTTPException(status_code=400, detail="请选择要下载的项目")

        archive_buffer = io.BytesIO()
        try:
            with zipfile.ZipFile(
                archive_buffer,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as zip_handle:
                written_names: set[str] = set()
                for target_path in target_paths:
                    self._write_file_manager_zip_entry(
                        zip_handle,
                        target_path=target_path,
                        archive_name=target_path.name,
                        written_names=written_names,
                    )
        except OSError as exc:
            logger.warning("[RocketCatShell] 文件管理下载打包失败: err=%r", exc)
            raise HTTPException(status_code=500, detail="下载打包失败") from exc

        content = archive_buffer.getvalue()
        return Response(
            content=content,
            media_type="application/zip",
            headers={
                "Content-Disposition": 'attachment; filename="files.zip"',
                "Content-Length": str(len(content)),
            },
        )

    def _resolve_file_manager_path(self, raw_path: str) -> Path:
        cleaned_path = str(raw_path or "").strip().replace("\\", "/")
        if "\x00" in cleaned_path:
            raise HTTPException(status_code=400, detail="文件路径无效")
        if (
            cleaned_path.startswith(("/", "\\"))
            or Path(cleaned_path).drive
            or (len(cleaned_path) >= 2 and cleaned_path[1] == ":")
        ):
            raise HTTPException(status_code=400, detail="文件路径必须是项目根目录内的相对路径")

        candidate = (self._file_root / cleaned_path).resolve()
        if candidate != self._file_root and not candidate.is_relative_to(self._file_root):
            raise HTTPException(status_code=403, detail="文件路径不能越过 RocketCatShell 根目录")
        return candidate

    def _resolve_file_manager_targets(self, raw_paths: list[Any], *, operation: str) -> list[Path]:
        target_paths: list[Path] = []
        seen_paths: set[Path] = set()
        for raw_path in raw_paths:
            if raw_path is None:
                continue
            target_path = self._resolve_file_manager_path(str(raw_path))
            if target_path == self._file_root:
                raise HTTPException(status_code=400, detail="不能操作 RocketCatShell 根目录")
            if not target_path.exists():
                raise HTTPException(status_code=404, detail=f"文件或目录不存在: {raw_path}")
            if target_path in seen_paths:
                continue
            seen_paths.add(target_path)
            target_paths.append(target_path)
        target_paths.sort(key=lambda item: len(item.parts), reverse=operation == "delete")
        return target_paths

    def _normalize_file_manager_name(self, raw_name: str) -> str:
        name = str(raw_name or "").strip()
        if not name or "\x00" in name:
            raise HTTPException(status_code=400, detail="名称不能为空")
        if name in {".", ".."} or "/" in name or "\\" in name:
            raise HTTPException(status_code=400, detail="名称不能包含目录层级")
        if Path(name).drive or (len(name) >= 2 and name[1] == ":"):
            raise HTTPException(status_code=400, detail="名称不能包含盘符")
        if any(ch in name for ch in '<>:"|?*'):
            raise HTTPException(status_code=400, detail="名称包含非法字符")
        return name

    def _write_file_manager_zip_entry(
        self,
        zip_handle: zipfile.ZipFile,
        *,
        target_path: Path,
        archive_name: str,
        written_names: set[str],
    ) -> None:
        safe_archive_name = str(archive_name or target_path.name).replace("\\", "/").strip("/")
        if not safe_archive_name or safe_archive_name.startswith("../") or "/../" in safe_archive_name:
            raise HTTPException(status_code=400, detail="压缩包路径无效")

        if target_path.is_dir() and not target_path.is_symlink():
            directory_entry = f"{safe_archive_name}/"
            if directory_entry not in written_names:
                zip_handle.writestr(directory_entry, b"")
                written_names.add(directory_entry)
            for child in sorted(target_path.rglob("*"), key=lambda item: item.as_posix().lower()):
                try:
                    resolved_child = child.resolve()
                    if resolved_child != self._file_root and not resolved_child.is_relative_to(self._file_root):
                        continue
                    child_relative = child.relative_to(target_path).as_posix()
                except OSError:
                    continue
                self._write_file_manager_zip_entry(
                    zip_handle,
                    target_path=child,
                    archive_name=f"{safe_archive_name}/{child_relative}",
                    written_names=written_names,
                )
            return

        if safe_archive_name in written_names:
            return
        zip_handle.write(target_path, safe_archive_name)
        written_names.add(safe_archive_name)

    def _join_file_manager_relative_path(self, base_path: str, child_path: str) -> str:
        normalized_base = str(base_path or "").strip().strip("/").replace("\\", "/")
        normalized_child = str(child_path or "").strip().strip("/").replace("\\", "/")
        if normalized_base and normalized_child:
            return f"{normalized_base}/{normalized_child}"
        return normalized_child or normalized_base

    def _normalize_uploaded_file_name(self, file_name: str) -> str:
        cleaned_name = str(file_name or "").strip().replace("\\", "/")
        if not cleaned_name or "\x00" in cleaned_name:
            raise HTTPException(status_code=400, detail="上传文件名无效")
        if (
            cleaned_name.startswith("/")
            or Path(cleaned_name).drive
            or (len(cleaned_name) >= 2 and cleaned_name[1] == ":")
        ):
            raise HTTPException(status_code=400, detail="上传文件名必须是相对路径")

        parts = [part for part in cleaned_name.split("/") if part and part != "."]
        if not parts or any(part == ".." for part in parts):
            raise HTTPException(status_code=400, detail="上传文件名不能包含路径穿越")
        if any(any(ch in part for ch in '<>:"|?*') for part in parts):
            raise HTTPException(status_code=400, detail="上传文件名包含非法字符")
        return "/".join(parts)

    def _deduplicate_upload_path(self, target_path: Path) -> Path:
        if not target_path.exists():
            return target_path

        suffix = target_path.suffix
        stem = target_path.stem if suffix else target_path.name
        for _ in range(20):
            candidate = target_path.with_name(f"{stem}-{uuid.uuid4().hex[:8]}{suffix}")
            if not candidate.exists():
                return candidate
        raise HTTPException(status_code=409, detail="无法生成不冲突的上传文件名")

    async def _write_uploaded_file(self, upload: UploadFile, destination_path: Path) -> None:
        bytes_written = 0
        try:
            with destination_path.open("wb") as handle:
                while True:
                    chunk = await upload.read(_FILE_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    if bytes_written > _FILE_UPLOAD_LIMIT_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"单个文件不能超过 {_FILE_UPLOAD_LIMIT_BYTES // (1024 * 1024)} MiB",
                        )
                    handle.write(chunk)
        except HTTPException:
            try:
                destination_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _file_manager_relative_path(self, target_path: Path) -> str:
        if target_path == self._file_root:
            return ""
        return target_path.relative_to(self._file_root).as_posix()

    def _file_manager_parts(self, target_path: Path) -> tuple[str, ...]:
        relative_path = self._file_manager_relative_path(target_path)
        return tuple(part.lower() for part in relative_path.split("/") if part)

    def _is_protected_file_manager_path(self, target_path: Path) -> bool:
        parts = self._file_manager_parts(target_path)
        if parts in _PROTECTED_FILE_EXACT_PATHS:
            return True
        return any(
            len(parts) >= len(root_parts) and parts[: len(root_parts)] == root_parts
            for root_parts in _PROTECTED_FILE_ROOTS
        )

    def _contains_protected_file_manager_path(self, target_path: Path) -> bool:
        parts = self._file_manager_parts(target_path)
        if not parts:
            return True
        return any(
            len(parts) < len(root_parts) and root_parts[: len(parts)] == parts
            for root_parts in _PROTECTED_FILE_ROOTS
        )

    def _is_protected_mutation_path(self, target_path: Path) -> bool:
        return self._is_protected_file_manager_path(target_path) or (
            target_path.exists()
            and target_path.is_dir()
            and self._contains_protected_file_manager_path(target_path)
        )

    def _can_edit_file_manager_path(
        self,
        target_path: Path,
        *,
        stat_result: Any,
        truncated: bool = False,
    ) -> bool:
        if target_path.is_dir() or self._is_protected_file_manager_path(target_path):
            return False
        if truncated or int(getattr(stat_result, "st_size", 0)) > _FILE_EDIT_LIMIT_BYTES:
            return False
        extension = target_path.suffix.lower()
        return extension not in _BINARY_PREVIEW_EXTENSIONS and extension not in _IMAGE_PREVIEW_EXTENSIONS

    def _serialize_file_item(self, target_path: Path, *, stat_result: Any) -> dict[str, Any]:
        is_directory = target_path.is_dir()
        extension = "" if is_directory else target_path.suffix.lower()
        preview_type = "directory" if is_directory else "text"
        if extension in _IMAGE_PREVIEW_EXTENSIONS:
            preview_type = "image"
        elif extension in _BINARY_PREVIEW_EXTENSIONS:
            preview_type = "binary"
        is_protected = self._is_protected_file_manager_path(target_path)
        can_edit = preview_type == "text" and self._can_edit_file_manager_path(
            target_path,
            stat_result=stat_result,
        )
        return {
            "name": target_path.name,
            "path": self._file_manager_relative_path(target_path),
            "is_directory": is_directory,
            "size": 0 if is_directory else stat_result.st_size,
            "mtime": datetime.fromtimestamp(stat_result.st_mtime).isoformat(timespec="seconds"),
            "extension": extension,
            "preview_type": preview_type,
            "requires_password": self._is_sensitive_file_manager_path(target_path),
            "is_protected": is_protected,
            "can_edit": can_edit,
        }

    def _is_sensitive_file_manager_path(self, target_path: Path) -> bool:
        parts = self._file_manager_parts(target_path)
        if parts in {("config", "shell.json"), ("config", "bots.json")}:
            return True
        if len(parts) == 3 and parts[0] == "config" and parts[1] == "plugins_config":
            return parts[2].endswith(".json")
        if len(parts) >= 4 and parts[0] == "data" and parts[1] == "bots":
            return parts[-1] == "runtime_state.json"
        return False

    async def _verify_file_manager_password(self, request: Request, password: str) -> None:
        if not self._auth_required or not self._access_password:
            raise HTTPException(
                status_code=403,
                detail="请先在基础设置中设置 WebUI 登录认证 / 文件管理鉴权密码",
            )

        client_ip = self._get_client_ip(request)
        if not await self._check_rate_limit(client_ip):
            raise HTTPException(status_code=429, detail="尝试次数过多，请 5 分钟后再试")

        if not password or not secrets.compare_digest(str(password).strip(), self._access_password):
            await self._record_failed_attempt(client_ip)
            await asyncio.sleep(0.8)
            raise HTTPException(status_code=401, detail="文件管理鉴权密码错误")

    def _looks_like_binary(self, payload: bytes) -> bool:
        if not payload:
            return False
        if b"\x00" in payload:
            return True
        allowed_controls = {7, 8, 9, 10, 12, 13, 27}
        control_count = sum(1 for byte in payload if byte < 32 and byte not in allowed_controls)
        return control_count / max(1, len(payload)) > 0.08

    async def _handle_list_bots(self) -> dict[str, Any]:
        return {"items": await self.manager.list_bots()}

    async def _handle_create_bot(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            created = await self.manager.create_bot(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(f"[RocketCatShell] 创建 bot 失败: {exc!r}")
            raise HTTPException(status_code=500, detail="创建 bot 失败") from exc
        return {"item": created}

    async def _handle_update_bot(self, bot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            updated = await self.manager.update_bot(bot_id, payload)
        except KeyError:
            raise HTTPException(status_code=404, detail="找不到目标 bot")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(f"[RocketCatShell] 更新 bot 失败: {exc!r}")
            raise HTTPException(status_code=500, detail="更新 bot 失败") from exc
        return {"item": updated}

    async def _handle_list_user_mappings(
        self,
        bot_id: str,
        search: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        try:
            return await self.manager.list_user_mappings(
                bot_id,
                search=search,
                offset=offset,
                limit=limit,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="找不到目标 bot")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error("[RocketCatShell] 读取用户映射失败: bot_id=%s error=%r", bot_id, exc)
            raise HTTPException(status_code=500, detail="读取用户映射失败") from exc

    async def _handle_update_user_mapping(
        self,
        bot_id: str,
        user_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return await self.manager.update_user_mapping(
                bot_id,
                user_id,
                onebot_id=payload.get("onebot_id"),
                revision=int(payload.get("revision") or 0),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="找不到目标 bot")
        except UserIdentityConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": str(exc),
                    "occupant": exc.occupant,
                },
            ) from exc
        except UserIdentityRevisionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UserIdentityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(
                "[RocketCatShell] 更新用户映射失败: bot_id=%s user_id=%s error=%r",
                bot_id,
                user_id,
                exc,
            )
            raise HTTPException(status_code=500, detail="更新用户映射失败") from exc

    async def _handle_delete_user_mapping(
        self,
        bot_id: str,
        user_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return await self.manager.delete_user_mapping(
                bot_id,
                user_id,
                revision=int(payload.get("revision") or 0),
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="找不到目标 bot")
        except UserIdentityRevisionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except UserIdentityError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(
                "[RocketCatShell] 删除用户映射失败: bot_id=%s user_id=%s error=%r",
                bot_id,
                user_id,
                exc,
            )
            raise HTTPException(status_code=500, detail="删除用户映射失败") from exc

    async def _handle_delete_bot(self, bot_id: str) -> dict[str, bool]:
        try:
            await self.manager.delete_bot(bot_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="找不到目标 bot")
        except Exception as exc:
            logger.error(f"[RocketCatShell] 删除 bot 失败: {exc!r}")
            raise HTTPException(status_code=500, detail="删除 bot 失败") from exc
        return {"ok": True}

    async def _handle_list_plugins(self) -> dict[str, Any]:
        return {"items": await self.manager.list_plugins()}

    async def _handle_get_plugin(self, plugin_id: str) -> dict[str, Any]:
        try:
            item = await self.manager.get_plugin(plugin_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="找不到目标插件")
        except Exception as exc:
            logger.error(f"[RocketCatShell] 读取插件详情失败: {exc!r}")
            raise HTTPException(status_code=500, detail="读取插件详情失败") from exc
        return {"item": item}

    async def _handle_update_plugin_config(
        self,
        plugin_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            item = await self.manager.update_plugin_config(plugin_id, payload)
        except KeyError:
            raise HTTPException(status_code=404, detail="找不到目标插件")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(f"[RocketCatShell] 更新插件设置失败: {exc!r}")
            raise HTTPException(status_code=500, detail="更新插件设置失败") from exc
        return {"item": item}

    async def _handle_set_plugin_enabled(
        self,
        plugin_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if "enabled" not in payload:
            raise HTTPException(status_code=400, detail="缺少 enabled 字段")
        try:
            item = await self.manager.set_plugin_enabled(plugin_id, bool(payload.get("enabled")))
        except KeyError:
            raise HTTPException(status_code=404, detail="找不到目标插件")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.error(f"[RocketCatShell] 更新插件启用状态失败: {exc!r}")
            raise HTTPException(status_code=500, detail="更新插件启用状态失败") from exc
        return {"item": item}

    async def _handle_reload_plugin(self, plugin_id: str) -> dict[str, Any]:
        try:
            item = await self.manager.reload_plugin(plugin_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="找不到目标插件")
        except Exception as exc:
            logger.error(f"[RocketCatShell] 重载插件失败: {exc!r}")
            raise HTTPException(status_code=500, detail="重载插件失败") from exc
        return {"item": item, "ok": True}

    async def _handle_plugin_logo(self, plugin_id: str) -> FileResponse:
        try:
            logo_path = await self.manager.get_plugin_logo_path(plugin_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="找不到目标插件")
        if logo_path is None or not logo_path.exists():
            raise HTTPException(status_code=404, detail="插件 Logo 不存在")
        return FileResponse(logo_path)

    async def _handle_uninstall_plugin(
        self,
        plugin_id: str,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        payload = payload or {}
        try:
            result = await self.manager.uninstall_plugin(
                plugin_id,
                delete_config=payload.get("delete_config") is True,
                delete_data=payload.get("delete_data") is True,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="找不到目标插件")
        except Exception as exc:
            logger.error(f"[RocketCatShell] 卸载插件失败: {exc!r}")
            raise HTTPException(status_code=500, detail="卸载插件失败") from exc
        return {"ok": True, "result": result}

from __future__ import annotations

import mimetypes
import os
import re
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse


_MEDIA_ROUTE_PREFIX = "/_rocketcat/media"
_HOST_UPSTREAM_NAMES = {
    "",
    "0.0.0.0",
    "127.0.0.1",
    "::1",
    "host.containers.internal",
    "host.docker.internal",
    "localhost",
}


@dataclass(frozen=True, slots=True)
class PublishedMedia:
    bot_id: str
    token: str
    filename: str
    file_path: Path
    content_type: str
    size: int
    mtime_ns: int


class MediaPublicationService:
    route_prefix = _MEDIA_ROUTE_PREFIX

    def __init__(self, *, max_entries: int = 4096) -> None:
        self._max_entries = max(128, int(max_entries))
        self._webui_port: int | None = None
        self._entries: OrderedDict[str, PublishedMedia] = OrderedDict()
        self._tokens_by_path: dict[tuple[str, str], str] = {}

    def configure_webui(self, *, port: int) -> None:
        self._webui_port = int(port)

    def clear_webui(self) -> None:
        self._webui_port = None

    def invalidate_bot(self, bot_id: str) -> None:
        normalized_bot_id = str(bot_id or "").strip()
        for token, entry in tuple(self._entries.items()):
            if entry.bot_id == normalized_bot_id:
                self._remove_token(token)

    def publish(
        self,
        *,
        bot_id: str,
        onebot_ws_url: str,
        file_path: str,
        name: str = "",
        content_type: str = "",
    ) -> str | None:
        if self._webui_port is None:
            return None
        try:
            resolved = Path(file_path).resolve(strict=True)
            if not resolved.is_file():
                return None
            stat_result = resolved.stat()
        except (OSError, RuntimeError, ValueError):
            return None

        normalized_bot_id = str(bot_id or "default").strip() or "default"
        normalized_path = os.path.normcase(str(resolved))
        path_key = (normalized_bot_id, normalized_path)
        token = self._tokens_by_path.get(path_key)
        entry = self._entries.get(token or "")
        if (
            entry is None
            or entry.size != stat_result.st_size
            or entry.mtime_ns != stat_result.st_mtime_ns
        ):
            if token:
                self._remove_token(token)
            token = secrets.token_urlsafe(32)
            filename = self._safe_filename(name or resolved.name)
            resolved_content_type = (
                str(content_type or "").split(";", 1)[0].strip()
                or mimetypes.guess_type(filename or resolved.name)[0]
                or "application/octet-stream"
            )
            entry = PublishedMedia(
                bot_id=normalized_bot_id,
                token=token,
                filename=filename,
                file_path=resolved,
                content_type=resolved_content_type,
                size=stat_result.st_size,
                mtime_ns=stat_result.st_mtime_ns,
            )
            self._entries[token] = entry
            self._tokens_by_path[path_key] = token
            self._evict_old_entries()
        else:
            self._entries.move_to_end(token)

        base_url = self.upstream_base_url(onebot_ws_url)
        if not base_url:
            return None
        return (
            f"{base_url}{self.route_prefix}/"
            f"{quote(entry.bot_id, safe='')}/{entry.token}/{quote(entry.filename, safe='')}"
        )

    def upstream_base_url(self, onebot_ws_url: str) -> str:
        if self._webui_port is None:
            return ""
        return self._resolve_upstream_base_url(onebot_ws_url)

    def resolve(
        self,
        *,
        bot_id: str,
        token: str,
        filename: str,
    ) -> PublishedMedia | None:
        entry = self._entries.get(str(token or ""))
        if entry is None:
            return None
        if entry.bot_id != str(bot_id or ""):
            return None
        if entry.filename != self._safe_filename(unquote(str(filename or ""))):
            return None
        try:
            stat_result = entry.file_path.stat()
        except OSError:
            self._remove_token(entry.token)
            return None
        if (
            not entry.file_path.is_file()
            or stat_result.st_size != entry.size
            or stat_result.st_mtime_ns != entry.mtime_ns
        ):
            self._remove_token(entry.token)
            return None
        self._entries.move_to_end(entry.token)
        return entry

    def is_current_url(self, value: str) -> bool:
        parsed = urlparse(str(value or ""))
        match = re.fullmatch(
            rf"{re.escape(self.route_prefix)}/([^/]+)/([^/]+)/([^/]+)",
            parsed.path,
        )
        if match is None:
            return False
        return (
            self.resolve(
                bot_id=unquote(match.group(1)),
                token=match.group(2),
                filename=unquote(match.group(3)),
            )
            is not None
        )

    @classmethod
    def is_media_url(cls, value: str) -> bool:
        return urlparse(str(value or "")).path.startswith(f"{cls.route_prefix}/")

    def _resolve_upstream_base_url(self, onebot_ws_url: str) -> str:
        override = str(os.environ.get("ROCKETCAT_UPSTREAM_MEDIA_BASE_URL") or "").strip()
        if override:
            return override.rstrip("/")

        parsed = urlparse(str(onebot_ws_url or ""))
        upstream_host = str(parsed.hostname or "").lower()
        if upstream_host in _HOST_UPSTREAM_NAMES or not self._is_running_in_container():
            return f"http://127.0.0.1:{self._webui_port}"

        service_name = str(
            os.environ.get("ROCKETCAT_DOCKER_SERVICE_NAME") or "rocketcatshell"
        ).strip()
        service_name = service_name or "rocketcatshell"
        return f"http://{service_name}:{self._webui_port}"

    @staticmethod
    def _is_running_in_container() -> bool:
        return Path("/.dockerenv").exists() or bool(
            str(os.environ.get("ROCKETCAT_CONTAINER_NAME") or "").strip()
        )

    @staticmethod
    def _safe_filename(value: str) -> str:
        filename = os.path.basename(str(value or "")) or "media"
        filename = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", filename).strip(" .")
        return filename or "media"

    def _remove_token(self, token: str) -> None:
        entry = self._entries.pop(token, None)
        if entry is None:
            return
        path_key = (entry.bot_id, os.path.normcase(str(entry.file_path)))
        if self._tokens_by_path.get(path_key) == token:
            self._tokens_by_path.pop(path_key, None)

    def _evict_old_entries(self) -> None:
        while len(self._entries) > self._max_entries:
            token, _ = self._entries.popitem(last=False)
            for path_key, registered_token in tuple(self._tokens_by_path.items()):
                if registered_token == token:
                    self._tokens_by_path.pop(path_key, None)
                    break

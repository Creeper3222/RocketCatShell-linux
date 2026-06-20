from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

import aiohttp

from rocketcat_shell.logger import logger

from .json_codec import json_dumps_compact, json_loads
from .media_publication import MediaPublicationService
from .rocketchat_compat import RocketChatHTTPError


class RocketChatMediaBridge:
    _PLAIN_UPLOAD_LEGACY_ENDPOINT = "rooms.upload"
    _PLAIN_UPLOAD_MODERN_ENDPOINT = "rooms.media"
    _ENDPOINT_COMPATIBILITY_FAILURE_STATUSES = {404, 405, 410, 501}
    _UPLOAD_MESSAGE_ECHO_TIMEOUT = 5.0
    _LOCAL_MEDIA_MAX_BASE64_BYTES = 20 * 1024 * 1024

    def __init__(
        self,
        client: Any,
        *,
        cache_dir: str | os.PathLike[str] | None = None,
        media_publication_service: MediaPublicationService | None = None,
    ) -> None:
        self.client = client
        self._plain_upload_endpoint_preference: str | None = None
        self._local_media_cache_dir = self._resolve_local_media_cache_dir(cache_dir)
        self._media_publication_service = media_publication_service

    def _resolve_local_media_cache_dir(
        self,
        cache_dir: str | os.PathLike[str] | None,
    ) -> Path:
        if cache_dir is not None:
            return Path(cache_dir).resolve()
        bot_id = str(getattr(self.client.config, "bot_id", "") or "default").strip()
        return (Path(tempfile.gettempdir()) / "rocketcat_shell_media" / bot_id).resolve()

    def _create_media_temp_file(self, suffix: str):
        self._local_media_cache_dir.mkdir(parents=True, exist_ok=True)
        return tempfile.NamedTemporaryFile(
            suffix=suffix,
            delete=False,
            dir=self._local_media_cache_dir,
        )

    @property
    def local_media_base_url(self) -> str:
        if self._media_publication_service is None:
            return ""
        return self._media_publication_service.upstream_base_url(
            str(getattr(self.client.config, "onebot_ws_url", "") or "")
        )

    async def start(self) -> None:
        self._local_media_cache_dir.mkdir(parents=True, exist_ok=True)

    async def stop(self) -> None:
        if self._media_publication_service is not None:
            self._media_publication_service.invalidate_bot(
                str(getattr(self.client.config, "bot_id", "") or "")
            )

    @staticmethod
    def _safe_media_suffix(value: str, default: str = ".bin") -> str:
        parsed = urlparse(str(value or ""))
        suffix = os.path.splitext(parsed.path or str(value or ""))[1].lower()
        if not suffix or not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            return default
        return suffix

    @staticmethod
    def _detect_media_suffix(raw: bytes, default: str = ".bin") -> str:
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if raw.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if raw.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            return ".webp"
        return default

    def _write_cached_media_file(self, raw: bytes, suffix: str) -> str:
        self._local_media_cache_dir.mkdir(parents=True, exist_ok=True)
        safe_suffix = self._safe_media_suffix(suffix)
        safe_suffix = self._detect_media_suffix(raw, safe_suffix)
        digest = hashlib.sha256(raw).hexdigest()
        target = self._local_media_cache_dir / f"e2ee_{digest}{safe_suffix}"
        if not target.exists():
            try:
                with target.open("xb") as handle:
                    handle.write(raw)
            except FileExistsError:
                pass
        return str(target)

    def _is_allowed_local_media_path(self, file_path: str) -> bool:
        try:
            resolved = Path(file_path).resolve()
        except (OSError, RuntimeError, ValueError):
            return False
        allowed_roots = (
            self._local_media_cache_dir,
            Path(tempfile.gettempdir()).resolve(),
        )
        for root in allowed_roots:
            try:
                normalized_root = os.path.normcase(str(root))
                normalized_resolved = os.path.normcase(str(resolved))
                if (
                    os.path.commonpath([normalized_root, normalized_resolved])
                    == normalized_root
                ):
                    return True
            except ValueError:
                continue
        return False

    def _copy_allowed_media_into_cache(self, file_path: str) -> str | None:
        candidate = str(file_path or "").strip()
        if not candidate or not self._is_allowed_local_media_path(candidate):
            return None
        try:
            limit = self._remote_media_size_limit()
            if limit and os.path.getsize(candidate) > limit:
                return None
            raw = Path(candidate).read_bytes()
        except OSError:
            return None
        suffix = self._safe_media_suffix(candidate)
        return self._write_cached_media_file(raw, suffix)

    def publish_local_media_file(
        self,
        file_path: str,
        *,
        name: str = "",
        content_type: str = "",
    ) -> str | None:
        cached_path = self._copy_allowed_media_into_cache(file_path)
        if not cached_path or self._media_publication_service is None:
            return None
        published_name = os.path.basename(str(name or "").strip()) or Path(cached_path).name
        detected_suffix = Path(cached_path).suffix
        if detected_suffix and Path(published_name).suffix.lower() != detected_suffix.lower():
            published_name = f"{Path(published_name).stem or 'media'}{detected_suffix}"
        return self._media_publication_service.publish(
            bot_id=str(getattr(self.client.config, "bot_id", "") or "default"),
            onebot_ws_url=str(getattr(self.client.config, "onebot_ws_url", "") or ""),
            file_path=cached_path,
            name=published_name,
            content_type=content_type,
        )

    def _publish_base64_media_ref(self, file_ref: str, *, kind: str) -> str | None:
        payload = str(file_ref or "").removeprefix("base64://")
        if not payload:
            return None
        try:
            raw = base64.b64decode(payload, validate=True)
        except Exception:
            return None
        limit = min(
            self._LOCAL_MEDIA_MAX_BASE64_BYTES,
            max(0, self._remote_media_size_limit()),
        )
        if not raw or (limit and len(raw) > limit):
            return None
        default_suffix = ".jpg" if kind == "image" else ".bin"
        suffix = self._detect_media_suffix(raw, default_suffix)
        local_path = self._write_cached_media_file(raw, suffix)
        return self.publish_local_media_file(local_path, name=f"media{suffix}")

    def is_current_local_media_url(self, value: str) -> bool:
        return bool(
            self._media_publication_service
            and self._media_publication_service.is_current_url(value)
        )

    @classmethod
    def is_rocketcat_local_media_url(cls, value: str) -> bool:
        return MediaPublicationService.is_media_url(value)

    def prepare_cached_onebot_event_media(self, event: dict[str, Any]) -> bool:
        segments = event.get("message")
        if not isinstance(segments, list):
            return True

        all_ready = True
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            kind = str(segment.get("type") or "")
            if kind not in {"image", "record", "video"}:
                continue
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            file_ref = str(data.get("file") or data.get("url") or "").strip()
            if not file_ref:
                continue
            if file_ref.startswith(("http://", "https://")):
                if self.is_rocketcat_local_media_url(file_ref):
                    all_ready = all_ready and self.is_current_local_media_url(file_ref)
                continue

            published_url: str | None = None
            if file_ref.startswith("base64://"):
                published_url = self._publish_base64_media_ref(file_ref, kind=kind)
            else:
                parsed = urlparse(file_ref)
                local_path = unquote(parsed.path) if parsed.scheme == "file" else file_ref
                if (
                    os.name == "nt"
                    and parsed.scheme == "file"
                    and local_path.startswith("/")
                    and len(local_path) > 3
                    and local_path[2] == ":"
                ):
                    local_path = local_path[1:]
                published_url = self.publish_local_media_file(local_path)
            if published_url:
                data["file"] = published_url
                data.pop("url", None)
            else:
                all_ready = False
        return all_ready

    @property
    def active_plain_upload_endpoint(self) -> str:
        capabilities = getattr(self.client, "capabilities", None)
        if capabilities is not None and not capabilities.allows_legacy_upload_fallback:
            return self._PLAIN_UPLOAD_MODERN_ENDPOINT
        return self._normalize_plain_upload_endpoint(self._plain_upload_endpoint_preference)

    def _remote_media_size_limit(self) -> int:
        return max(0, int(getattr(self.client.config, "remote_media_max_size", 0) or 0))

    def _log_media_size_limit_error(
        self,
        action: str,
        *,
        actual_size: int,
        limit: int,
        room_id: str | None = None,
        file_name: str | None = None,
        source: str | None = None,
    ) -> None:
        config = self.client.config
        logger.error(
            "[RocketChatOneBotBridge] %s，超过 bot 远程媒体大小上限: "
            "bot_id=%s bot_name=%s room_id=%s file=%s size=%s limit=%s source=%s",
            action,
            getattr(config, "bot_id", "") or "-",
            getattr(config, "display_name", "") or "-",
            room_id or "-",
            file_name or "-",
            actual_size,
            limit,
            source or "-",
        )

    def _check_upload_file_size(
        self,
        *,
        room_id: str,
        file_path: str,
        resolved_name: str,
    ) -> bool:
        limit = self._remote_media_size_limit()
        file_size = os.path.getsize(file_path)
        if file_size <= limit:
            return True
        self._log_media_size_limit_error(
            "上传媒体失败",
            actual_size=file_size,
            limit=limit,
            room_id=room_id,
            file_name=resolved_name,
            source=os.path.abspath(file_path),
        )
        return False

    def resolve_onebot_media_file_ref(self, media: dict[str, Any]) -> str:
        file_ref = str(media.get("path") or media.get("url") or "")
        if not file_ref:
            return ""

        local_path = str(media.get("path") or "").strip()
        if local_path:
            published_url = self.publish_local_media_file(
                local_path,
                name=str(media.get("name") or ""),
                content_type=str(media.get("content_type") or ""),
            )
            if published_url:
                media["url"] = published_url
                return published_url
            return ""

        return file_ref

    def build_onebot_segment_from_descriptor(self, media: dict[str, Any]) -> dict[str, Any] | None:
        kind = str(media.get("kind") or "")
        file_ref = self.resolve_onebot_media_file_ref(media)
        if not file_ref:
            return None

        if kind == "image":
            return {"type": "image", "data": {"file": file_ref}}
        if kind == "audio":
            return {"type": "record", "data": {"file": file_ref}}
        if kind == "video":
            return {"type": "video", "data": {"file": file_ref}}

        name = str(media.get("name") or "attachment")
        if media.get("path"):
            return {
                "type": "text",
                "data": {
                    "text": f"[加密文件] {name}",
                },
            }
        return {
            "type": "file",
            "data": {
                "url": file_ref,
                "file_name": name,
                "name": name,
            },
        }

    @staticmethod
    def _match_media_kind(candidate: Any) -> str | None:
        if not isinstance(candidate, str):
            return None
        normalized = candidate.strip().lower()
        if normalized.startswith("image/"):
            return "image"
        if normalized.startswith("audio/"):
            return "audio"
        if normalized.startswith("video/"):
            return "video"
        return None

    def classify_file_kind(self, file_obj: dict[str, Any]) -> str:
        for key in (
            "type",
            "mimeType",
            "contentType",
            "image_type",
            "audio_type",
            "video_type",
        ):
            matched_kind = self._match_media_kind(file_obj.get(key))
            if matched_kind:
                return matched_kind

        for key, kind in (
            ("image_url", "image"),
            ("imageUrl", "image"),
            ("audio_url", "audio"),
            ("audioUrl", "audio"),
            ("video_url", "video"),
            ("videoUrl", "video"),
        ):
            value = file_obj.get(key)
            if isinstance(value, str) and value:
                return kind

        for key in (
            "name",
            "title",
            "url",
            "path",
            "title_link",
            "titleLink",
            "link",
            "image_url",
            "imageUrl",
            "audio_url",
            "audioUrl",
            "video_url",
            "videoUrl",
        ):
            value = file_obj.get(key)
            if not isinstance(value, str) or not value:
                continue
            guessed, _ = mimetypes.guess_type(value.split("?", 1)[0])
            matched_kind = self._match_media_kind(guessed)
            if matched_kind:
                return matched_kind

        return "file"

    def get_all_attachments_recursive(
        self,
        payload: dict[str, Any],
        *,
        skip_quote_attachments: bool = False,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        att_raw = payload.get("attachments", [])
        atts = [att_raw] if isinstance(att_raw, dict) else [item for item in att_raw if isinstance(item, dict)]
        for att in atts:
            if skip_quote_attachments and att.get("message_link"):
                continue
            result.append(att)
            result.extend(
                self.get_all_attachments_recursive(
                    att,
                    skip_quote_attachments=skip_quote_attachments,
                )
            )
        return result

    def _iter_attachment_sources(
        self,
        payload: dict[str, Any],
        *,
        skip_quote_attachments: bool = False,
    ):
        attachments = payload.get("attachments", [])
        if isinstance(attachments, dict):
            attachments = [attachments]
        if not isinstance(attachments, list):
            return
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            if skip_quote_attachments and attachment.get("message_link"):
                continue
            yield attachment
            yield from self._iter_attachment_sources(
                attachment,
                skip_quote_attachments=skip_quote_attachments,
            )

    @staticmethod
    def _has_media_shaped_value(source: dict[str, Any], keys: tuple[str, ...]) -> bool:
        for key in keys:
            if source.get(key):
                return True
        return False

    @staticmethod
    def _normalize_attachment_list(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
        attachments = payload.get("attachments")
        if isinstance(attachments, dict):
            attachments = [attachments]
        if not isinstance(attachments, list):
            return None
        normalized = [attachment for attachment in attachments if isinstance(attachment, dict)]
        return normalized or None

    def _can_fast_extract_attachment_descriptors(
        self,
        payload: dict[str, Any],
        *,
        skip_quote_attachments: bool,
        include_url_images: bool,
    ) -> list[dict[str, Any]] | None:
        if payload.get("files") or payload.get("file") or payload.get("fileUpload"):
            return None
        if self._has_media_shaped_value(
            payload,
            (
                "type",
                "mimeType",
                "contentType",
                "image_url",
                "imageUrl",
                "audio_url",
                "audioUrl",
                "video_url",
                "videoUrl",
                "title_link",
                "titleLink",
                "url",
                "path",
                "link",
            ),
        ):
            return None
        if include_url_images and isinstance(payload.get("urls"), list) and payload.get("urls"):
            return None

        attachments = self._normalize_attachment_list(payload)
        if not attachments:
            return None

        fast_candidates: list[dict[str, Any]] = []
        for attachment in attachments:
            if skip_quote_attachments and attachment.get("message_link"):
                return None
            if attachment.get("attachments") or attachment.get("files") or attachment.get("file") or attachment.get("fileUpload"):
                return None
            fast_candidates.append(attachment)
        return fast_candidates or None

    async def extract_media_descriptors(
        self,
        payload: dict[str, Any],
        *,
        skip_quote_attachments: bool = True,
        include_url_images: bool = True,
    ) -> list[dict[str, str]]:
        media: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        media_shaped_keys = (
            "type",
            "mimeType",
            "contentType",
            "image_url",
            "imageUrl",
            "audio_url",
            "audioUrl",
            "video_url",
            "videoUrl",
            "title_link",
            "titleLink",
            "url",
            "path",
            "link",
        )

        async def append_candidate(candidate: dict[str, Any]) -> None:
            kind = self.classify_file_kind(candidate)
            materialized = await self._materialize_media_reference(candidate, kind)
            if not materialized:
                return
            file_ref = str(materialized.get("path") or materialized.get("url") or "")
            if not file_ref:
                return
            key = (kind, file_ref)
            if key in seen:
                return
            seen.add(key)
            media.append(
                {
                    "kind": kind,
                    "name": str(materialized.get("name") or self._extract_media_name(candidate, file_ref)),
                    "url": str(materialized.get("url") or ""),
                    "path": str(materialized.get("path") or ""),
                }
            )

        async def process_source(source: dict[str, Any]) -> None:
            files_raw = source.get("files", [])
            if isinstance(files_raw, dict):
                await append_candidate(files_raw)
            elif isinstance(files_raw, list):
                for item in files_raw:
                    if isinstance(item, dict):
                        await append_candidate(item)

            for key in ("file", "fileUpload"):
                single_file = source.get(key)
                if isinstance(single_file, dict):
                    await append_candidate(single_file)

            if self._has_media_shaped_value(source, media_shaped_keys):
                await append_candidate(source)

        fast_candidates = self._can_fast_extract_attachment_descriptors(
            payload,
            skip_quote_attachments=skip_quote_attachments,
            include_url_images=include_url_images,
        )
        if fast_candidates is not None:
            for candidate in fast_candidates:
                await append_candidate(candidate)
            return media

        await process_source(payload)
        for attachment in self._iter_attachment_sources(
            payload,
            skip_quote_attachments=skip_quote_attachments,
        ):
            await process_source(attachment)

        if include_url_images:
            for url_obj in payload.get("urls", []):
                if not isinstance(url_obj, dict):
                    continue
                meta = url_obj.get("meta") if isinstance(url_obj.get("meta"), dict) else {}
                headers = url_obj.get("headers") if isinstance(url_obj.get("headers"), dict) else {}
                content_type = (
                    meta.get("contentType")
                    or headers.get("contentType")
                    or headers.get("content-type")
                    or ""
                )
                if not str(content_type).startswith("image/"):
                    continue
                candidate = url_obj.get("url")
                if not isinstance(candidate, str) or not candidate:
                    continue
                normalized = await self.client._normalize_media_url(candidate)
                key = ("image", normalized)
                if key in seen:
                    continue
                seen.add(key)
                media.append(
                    {
                        "kind": "image",
                        "name": self._extract_media_name(url_obj, normalized),
                        "url": normalized,
                        "path": "",
                    }
                )

        return media

    def build_onebot_segments_from_descriptors(
        self,
        media_descriptors: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for media in media_descriptors:
            segment = self.build_onebot_segment_from_descriptor(media)
            if not segment:
                continue
            data = segment.get("data") or {}
            file_ref = str(data.get("file") or data.get("url") or "")
            key = (str(segment.get("type") or ""), file_ref)
            if not file_ref or key in seen:
                continue
            seen.add(key)
            segments.append(segment)

        return segments

    def _extract_media_name(self, payload: dict[str, Any], media_url: str) -> str:
        return str(
            payload.get("name")
            or payload.get("title")
            or payload.get("file_name")
            or os.path.basename(urlparse(media_url).path)
            or "attachment"
        )

    def _is_encrypted_media_attachment(self, file_obj: dict[str, Any]) -> bool:
        encryption = file_obj.get("encryption")
        return (
            isinstance(encryption, dict)
            and isinstance(encryption.get("key"), dict)
            and isinstance(encryption.get("iv"), str)
            and bool(encryption.get("iv"))
        )

    async def download_remote_bytes(self, url: str) -> Optional[bytes]:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            logger.warning(f"[RocketChatOneBotBridge] 拒绝下载不支持的媒体协议: {url}")
            return None
        if self.client._http_session is None:
            return None

        limit = self._remote_media_size_limit()
        try:
            async with self.client._http_session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
                allow_redirects=True,
                max_redirects=3,
            ) as resp:
                if resp.status >= 400:
                    logger.error(f"[RocketChatOneBotBridge] 下载媒体失败 {resp.status}: {url}")
                    return None

                content_length = resp.content_length
                if content_length is not None and content_length > limit:
                    self._log_media_size_limit_error(
                        "下载媒体失败",
                        actual_size=content_length,
                        limit=limit,
                        source=url,
                    )
                    return None

                raw = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > limit:
                        self._log_media_size_limit_error(
                            "下载媒体失败",
                            actual_size=len(raw),
                            limit=limit,
                            source=url,
                        )
                        return None
                return bytes(raw)
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] 下载媒体异常: {exc!r}")
            return None

    async def _select_media_url(
        self,
        file_obj: dict[str, Any],
        target_kind: str,
    ) -> Optional[str]:
        key_candidates: dict[str, tuple[str, ...]] = {
            "image": (
                "image_url",
                "imageUrl",
                "image",
                "thumb_url",
                "thumbUrl",
                "image_preview",
                "imagePreview",
                "title_link",
                "titleLink",
                "url",
                "path",
                "link",
            ),
            "audio": (
                "audio_url",
                "audioUrl",
                "title_link",
                "titleLink",
                "url",
                "path",
                "link",
            ),
            "video": (
                "video_url",
                "videoUrl",
                "title_link",
                "titleLink",
                "url",
                "path",
                "link",
            ),
            "file": (
                "title_link",
                "titleLink",
                "url",
                "path",
                "link",
            ),
        }

        for key in key_candidates.get(target_kind, ()):
            value = file_obj.get(key)
            if isinstance(value, str) and value:
                return await self.client._normalize_media_url(value)
        return None

    def _guess_media_suffix(
        self,
        file_obj: dict[str, Any],
        media_url: str,
        default_suffix: str,
    ) -> str:
        for candidate in (
            file_obj.get("name"),
            file_obj.get("title"),
            media_url,
        ):
            if not isinstance(candidate, str) or not candidate:
                continue
            parsed = urlparse(candidate)
            _, ext = os.path.splitext(parsed.path or candidate)
            if ext:
                return ext

        for key in (
            "type",
            "mimeType",
            "contentType",
            "image_type",
            "audio_type",
            "video_type",
        ):
            mime_value = file_obj.get(key)
            if not isinstance(mime_value, str) or not mime_value:
                continue
            ext = mimetypes.guess_extension(mime_value.split(";", 1)[0].strip())
            if ext:
                return ext

        return default_suffix

    def _write_temp_media_file(self, raw: bytes, suffix: str) -> str:
        return self._write_cached_media_file(raw, suffix)

    async def _materialize_media_reference(
        self,
        file_obj: dict[str, Any],
        target_kind: str,
    ) -> Optional[dict[str, str]]:
        media_url = await self._select_media_url(file_obj, target_kind)
        if not media_url:
            return None

        name = (
            file_obj.get("name")
            or file_obj.get("title")
            or os.path.basename(urlparse(media_url).path)
            or "attachment"
        )

        if not self._is_encrypted_media_attachment(file_obj):
            return {"name": str(name), "url": media_url}

        raw = await self.download_remote_bytes(media_url)
        if raw is None:
            return None

        try:
            encryption = file_obj["encryption"]
            decrypted = self.client.e2ee.decrypt_uploaded_media(
                raw,
                key_data=encryption["key"],
                iv_b64=encryption["iv"],
            )
        except Exception as exc:
            logger.warning(f"[RocketChatOneBotBridge][E2EE] 媒体解密失败: {exc!r}")
            return None

        expected_hash = (
            file_obj.get("hashes", {}).get("sha256")
            if isinstance(file_obj.get("hashes"), dict)
            else None
        )
        if expected_hash:
            actual_hash = hashlib.sha256(decrypted).hexdigest()
            if actual_hash.lower() != str(expected_hash).lower():
                logger.warning(
                    f"[RocketChatOneBotBridge][E2EE] 媒体哈希校验失败: expected={expected_hash} actual={actual_hash}"
                )
                return None

        suffix = self._guess_media_suffix(file_obj, media_url, ".bin")
        local_path = self._write_temp_media_file(decrypted, suffix)
        published_url = self.publish_local_media_file(
            local_path,
            name=str(name),
            content_type=str(
                file_obj.get("type")
                or file_obj.get("mimeType")
                or file_obj.get("contentType")
                or ""
            ),
        )
        return {
            "name": str(name),
            "path": local_path,
            "url": published_url or "",
        }

    async def _extract_media_payloads(
        self,
        raw_msg: dict[str, Any],
        target_kind: str,
    ) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []

        async def add_candidate(file_obj: dict[str, Any]) -> None:
            if self.classify_file_kind(file_obj) != target_kind:
                return
            materialized = await self._materialize_media_reference(file_obj, target_kind)
            if materialized:
                results.append(materialized)

        all_attachments = self.get_all_attachments_recursive(
            raw_msg,
            skip_quote_attachments=True,
        )

        for context in [raw_msg] + all_attachments:
            files_raw = context.get("files", [])
            iterable = [files_raw] if isinstance(files_raw, dict) else [item for item in files_raw if isinstance(item, dict)]
            for file_obj in iterable:
                await add_candidate(file_obj)

            for file_key in ("file", "fileUpload"):
                single_file = context.get(file_key)
                if isinstance(single_file, dict):
                    await add_candidate(single_file)

            if context is not raw_msg:
                await add_candidate(context)

        return results

    async def extract_onebot_segments(self, raw_msg: dict[str, Any]) -> list[dict[str, Any]]:
        descriptors = await self.extract_media_descriptors(raw_msg)
        return self.build_onebot_segments_from_descriptors(descriptors)

    def _detect_upload_content_type(self, file_path: str) -> str | None:
        try:
            with open(file_path, "rb") as fp:
                header = fp.read(64)
        except Exception:
            return None

        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if header.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if header.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if header.startswith(b"BM"):
            return "image/bmp"
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return "image/webp"
        if header.startswith(b"%PDF-"):
            return "application/pdf"
        if header.startswith(b"PK\x03\x04"):
            return "application/zip"
        if header.startswith(b"\x1f\x8b"):
            return "application/gzip"
        if header.startswith(b"ID3") or header[:2] in {
            b"\xff\xfb",
            b"\xff\xf3",
            b"\xff\xf2",
        }:
            return "audio/mpeg"
        if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
            return "audio/wav"
        if header.startswith(b"OggS"):
            return "audio/ogg"
        if len(header) >= 12 and header[4:8] == b"ftyp":
            return "video/mp4"
        if header.startswith(b"\x1aE\xdf\xa3"):
            return "video/webm"
        return None

    def infer_upload_content_type(self, file_path: str, filename: str) -> str:
        detected_type = self._detect_upload_content_type(file_path)
        if detected_type:
            return detected_type

        guessed_type, _ = mimetypes.guess_type(filename)
        if guessed_type:
            return guessed_type

        guessed_type, _ = mimetypes.guess_type(file_path)
        if guessed_type:
            return guessed_type
        return "application/octet-stream"

    @staticmethod
    def _sanitize_upload_filename(filename: str) -> str:
        raw_name = str(filename or "").strip()
        base_name = re.split(r"[\\/]", raw_name)[-1]
        base_name = re.sub(r"[\x00-\x1f\x7f]", "_", base_name)
        base_name = re.sub(r'[<>:"/\\|?*]', "_", base_name)
        base_name = base_name.rstrip(" .")
        if not base_name or base_name in {".", ".."}:
            base_name = "attachment"

        stem, suffix = os.path.splitext(base_name)
        if stem.upper() in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            "COM1",
            "COM2",
            "COM3",
            "COM4",
            "COM5",
            "COM6",
            "COM7",
            "COM8",
            "COM9",
            "LPT1",
            "LPT2",
            "LPT3",
            "LPT4",
            "LPT5",
            "LPT6",
            "LPT7",
            "LPT8",
            "LPT9",
        }:
            stem = f"_{stem}"
            base_name = f"{stem}{suffix}"

        if len(base_name) > 240:
            stem, suffix = os.path.splitext(base_name)
            base_name = f"{stem[: max(1, 240 - len(suffix))]}{suffix}"
        return base_name

    def prepare_upload_metadata(self, file_path: str, filename: str) -> tuple[str, str]:
        safe_name = self._sanitize_upload_filename(filename)
        detected_type = self._detect_upload_content_type(file_path)
        content_type = detected_type or self.infer_upload_content_type(file_path, safe_name)

        guessed_type, _ = mimetypes.guess_type(safe_name)
        if detected_type and guessed_type and guessed_type != detected_type:
            suffix = self._guess_suffix_from_content_type(detected_type, "")
            stem = os.path.splitext(safe_name)[0].rstrip(" .") or "attachment"
            safe_name = f"{stem}{suffix}" if suffix else stem
        elif detected_type and not os.path.splitext(safe_name)[1]:
            suffix = self._guess_suffix_from_content_type(detected_type, "")
            if suffix:
                safe_name = f"{safe_name}{suffix}"
        return safe_name, content_type

    def _normalize_plain_upload_endpoint(self, endpoint_name: str | None) -> str:
        if endpoint_name == self._PLAIN_UPLOAD_LEGACY_ENDPOINT:
            return self._PLAIN_UPLOAD_LEGACY_ENDPOINT
        return self._PLAIN_UPLOAD_MODERN_ENDPOINT

    def _alternate_plain_upload_endpoint(self, endpoint_name: str | None) -> str:
        normalized_endpoint = self._normalize_plain_upload_endpoint(endpoint_name)
        if normalized_endpoint == self._PLAIN_UPLOAD_MODERN_ENDPOINT:
            return self._PLAIN_UPLOAD_LEGACY_ENDPOINT
        return self._PLAIN_UPLOAD_MODERN_ENDPOINT

    def _build_plain_upload_url(self, endpoint_name: str | None, room_id: str) -> str:
        normalized_endpoint = self._normalize_plain_upload_endpoint(endpoint_name)
        return f"{self.client.config.server_url}/api/v1/{normalized_endpoint}/{room_id}"

    def _summarize_response_body(self, body_text: str, limit: int = 240) -> str:
        normalized = " ".join(str(body_text or "").split())
        if not normalized:
            return "-"
        if len(normalized) <= limit:
            return normalized
        return f"{normalized[: limit - 3]}..."

    def _is_plain_upload_endpoint_incompatible(self, result: dict[str, Any]) -> bool:
        status = result.get("status")
        if status in self._ENDPOINT_COMPATIBILITY_FAILURE_STATUSES:
            return True

        if result.get("ok"):
            return False

        content_type = str(result.get("content_type") or "").lower()
        response_preview = self._summarize_response_body(str(result.get("text") or "")).lower()
        if status == 404 and "not found" in response_preview:
            return True
        if status and status >= 400 and "text/plain" in content_type and "not found" in response_preview:
            return True
        return False

    async def post_multipart_json_result(
        self,
        url: str,
        form: aiohttp.FormData,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": False,
            "status": None,
            "content_type": "",
            "data": None,
            "text": "",
            "error": None,
        }
        headers = {
            "X-Auth-Token": self.client.auth_token,
            "X-User-Id": self.client.user_id,
        }
        if self.client._http_session is None:
            result["error"] = "Rocket.Chat HTTP session 尚未初始化"
            return result

        try:
            async with self.client._http_session.post(url, data=form, headers=headers) as resp:
                result["status"] = resp.status
                result["content_type"] = str(resp.headers.get("Content-Type") or "")
                response_text = await resp.text()
                result["text"] = response_text

                parsed_data: dict[str, Any] | None = None
                if response_text:
                    try:
                        candidate = json_loads(response_text)
                    except Exception as exc:
                        result["error"] = repr(exc)
                    else:
                        if isinstance(candidate, dict):
                            parsed_data = candidate
                        else:
                            result["error"] = (
                                f"unexpected JSON payload type: {type(candidate).__name__}"
                            )
                elif "json" in str(result["content_type"]).lower():
                    result["error"] = "empty JSON response body"

                result["data"] = parsed_data
                if resp.status < 400 and isinstance(parsed_data, dict) and parsed_data.get("success", True):
                    result["ok"] = True
                    return result

                if isinstance(parsed_data, dict):
                    logger.error(
                        "[RocketChatOneBotBridge] 上传请求失败: status=%s content_type=%s data=%s",
                        resp.status,
                        result["content_type"] or "-",
                        parsed_data,
                    )
                else:
                    logger.error(
                        "[RocketChatOneBotBridge] 上传请求失败: status=%s content_type=%s body=%s parse_error=%s",
                        resp.status,
                        result["content_type"] or "-",
                        self._summarize_response_body(response_text),
                        result["error"] or "-",
                    )
                return result
        except Exception as exc:
            result["error"] = repr(exc)
            logger.error(f"[RocketChatOneBotBridge] 上传请求异常: {exc!r}")
            return result

    async def post_multipart_json(
        self,
        url: str,
        form: aiohttp.FormData,
    ) -> Optional[dict[str, Any]]:
        result = await self.post_multipart_json_result(url, form)
        data = result.get("data")
        if result.get("ok") and isinstance(data, dict):
            return data
        return None

    async def _upload_plain_file_via_endpoint(
        self,
        endpoint_name: str,
        room_id: str,
        file_path: str,
        resolved_name: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> dict[str, Any]:
        url = self._build_plain_upload_url(endpoint_name, room_id)
        with open(file_path, "rb") as fp:
            form = aiohttp.FormData()
            content_type = self.infer_upload_content_type(file_path, resolved_name)
            form.add_field("file", fp, filename=resolved_name, content_type=content_type)
            if endpoint_name == self._PLAIN_UPLOAD_LEGACY_ENDPOINT:
                if description:
                    form.add_field("msg", description)
                if tmid:
                    form.add_field("tmid", tmid)
            return await self.post_multipart_json_result(url, form)

    def _build_plain_media_confirm_payload(
        self,
        *,
        file_name: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "msg": description,
            "description": description,
            "fileName": file_name,
        }
        if tmid:
            payload["tmid"] = tmid
        return payload

    def _guess_suffix_from_content_type(
        self,
        content_type: str,
        default_suffix: str,
    ) -> str:
        normalized_content_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if not normalized_content_type:
            return default_suffix

        if normalized_content_type == "image/svg+xml":
            return ".svg"
        if normalized_content_type == "image/jpeg":
            return ".jpg"

        guessed_suffix = mimetypes.guess_extension(normalized_content_type, strict=False)
        if guessed_suffix:
            return guessed_suffix
        return default_suffix

    async def _confirm_plain_uploaded_file(
        self,
        room_id: str,
        upload_data: dict[str, Any],
        *,
        resolved_name: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        uploaded_file = upload_data.get("file") or {}
        if not isinstance(uploaded_file, dict):
            raise RuntimeError(
                f"Rocket.Chat rooms.media response missing file object: "
                f"room_id={room_id!r} data={upload_data}"
            )

        file_id = str(uploaded_file.get("_id") or "").strip()
        if not file_id:
            raise RuntimeError(
                f"Rocket.Chat rooms.media response missing file._id: "
                f"room_id={room_id!r} data={upload_data}"
            )

        payload = self._build_plain_media_confirm_payload(
            file_name=resolved_name,
            description=description,
            tmid=tmid,
        )
        data = await self.client._request_json(
            "POST",
            f"{self.client.config.server_url}/api/v1/rooms.mediaConfirm/{room_id}/{file_id}",
            headers=self.client._auth_headers(),
            json=payload,
        )
        if not data.get("success"):
            raise RuntimeError(
                f"Rocket.Chat rooms.mediaConfirm failed: room_id={room_id!r} "
                f"file_id={file_id!r} data={data}"
            )
        self.client._mark_outbound_message_activity()

        message = self._extract_uploaded_message(data)
        if message is not None:
            return message

        logger.warning(
            "[RocketChatOneBotBridge] plain mediaConfirm 成功但未直接返回消息，准备回退到自回显兜底: room_id=%s file_id=%s keys=%s",
            room_id,
            file_id,
            ",".join(sorted(str(key) for key in data.keys())) or "-",
        )
        return data

    async def _finalize_plain_upload_response(
        self,
        endpoint_name: str,
        room_id: str,
        upload_data: dict[str, Any],
        *,
        resolved_name: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> tuple[dict[str, Any], bool]:
        if endpoint_name != self._PLAIN_UPLOAD_MODERN_ENDPOINT:
            return upload_data, False
        if self._extract_uploaded_message(upload_data) is not None:
            return upload_data, False

        confirmed_data = await self._confirm_plain_uploaded_file(
            room_id,
            upload_data,
            resolved_name=resolved_name,
            description=description,
            tmid=tmid,
        )
        if confirmed_data is not None:
            return confirmed_data, False
        return upload_data, True

    async def _try_plain_upload_endpoint(
        self,
        endpoint_name: str,
        room_id: str,
        file_path: str,
        resolved_name: str,
        *,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
        result = await self._upload_plain_file_via_endpoint(
            endpoint_name,
            room_id,
            file_path,
            resolved_name,
            description=description,
            tmid=tmid,
        )
        data = result.get("data")
        if not result.get("ok") or not isinstance(data, dict):
            return None, result, False

        try:
            finalized_data, needs_endpoint_fallback = await self._finalize_plain_upload_response(
                endpoint_name,
                room_id,
                data,
                resolved_name=resolved_name,
                description=description,
                tmid=tmid,
            )
        except RocketChatHTTPError as exc:
            error_result = {
                "ok": False,
                "status": exc.status,
                "content_type": exc.get_header("Content-Type"),
                "data": exc.data if isinstance(exc.data, dict) else None,
                "text": exc.response_text,
                "error": repr(exc),
            }
            return None, error_result, exc.endpoint_incompatible
        return finalized_data, result, needs_endpoint_fallback

    async def upload_plain_file(
        self,
        room_id: str,
        file_path: str,
        resolved_name: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        capabilities = getattr(self.client, "capabilities", None)
        legacy_fallback_allowed = bool(
            capabilities is None or capabilities.allows_legacy_upload_fallback
        )
        primary_endpoint = (
            self._normalize_plain_upload_endpoint(self._plain_upload_endpoint_preference)
            if legacy_fallback_allowed
            else self._PLAIN_UPLOAD_MODERN_ENDPOINT
        )
        primary_data, primary_result, primary_needs_fallback = await self._try_plain_upload_endpoint(
            primary_endpoint,
            room_id,
            file_path,
            resolved_name,
            description=description,
            tmid=tmid,
        )
        if primary_data is not None and not primary_needs_fallback:
            self._plain_upload_endpoint_preference = primary_endpoint
            return primary_data

        if not primary_needs_fallback and not self._is_plain_upload_endpoint_incompatible(primary_result):
            return None
        if not legacy_fallback_allowed:
            logger.error(
                "[RocketChatOneBotBridge] Rocket.Chat 8.x 上传链路失败，"
                "不会回退已移除的 rooms.upload: server=%s version=%s status=%s",
                self.client.config.server_url,
                getattr(capabilities, "version_text", "unknown"),
                primary_result.get("status") or "-",
            )
            return None

        fallback_endpoint = self._alternate_plain_upload_endpoint(primary_endpoint)
        logger.warning(
            "[RocketChatOneBotBridge] plain upload 端点需要回退: server=%s from=%s to=%s status=%s content_type=%s body=%s reason=%s",
            self.client.config.server_url,
            primary_endpoint,
            fallback_endpoint,
            primary_result.get("status") or "-",
            primary_result.get("content_type") or "-",
            self._summarize_response_body(str(primary_result.get("text") or "")),
            "mediaConfirm failed" if primary_needs_fallback else "endpoint incompatible",
        )
        fallback_data, fallback_result, fallback_needs_fallback = await self._try_plain_upload_endpoint(
            fallback_endpoint,
            room_id,
            file_path,
            resolved_name,
            description=description,
            tmid=tmid,
        )
        if fallback_data is not None and not fallback_needs_fallback:
            previous_endpoint = self._plain_upload_endpoint_preference
            self._plain_upload_endpoint_preference = fallback_endpoint
            if previous_endpoint != fallback_endpoint:
                logger.info(
                    "[RocketChatOneBotBridge] plain upload 端点已切换: server=%s from=%s to=%s",
                    self.client.config.server_url,
                    previous_endpoint or primary_endpoint,
                    fallback_endpoint,
                )
            return fallback_data
        if primary_data is not None:
            return primary_data
        return fallback_data

    async def upload_local_file(
        self,
        room_id: str,
        file_path: str,
        resolved_name: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        resolved_name, _ = self.prepare_upload_metadata(file_path, resolved_name)
        if not self._check_upload_file_size(
            room_id=room_id,
            file_path=file_path,
            resolved_name=resolved_name,
        ):
            return None

        room_info = await self.client.get_room_info(room_id)
        if self._is_e2ee_room_info(room_info):
            return await self.upload_encrypted_file(
                room_id,
                file_path,
                resolved_name,
                description=description,
                tmid=tmid,
            )
        return await self.upload_plain_file(
            room_id,
            file_path,
            resolved_name,
            description=description,
            tmid=tmid,
        )

    def _is_message_payload(self, payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        source_id = str(payload.get("_id") or "").strip()
        if not source_id:
            return False
        if str(payload.get("rid") or "").strip():
            return True
        if isinstance(payload.get("u"), dict):
            return True
        if payload.get("attachments") or payload.get("msg"):
            return True
        if str(payload.get("tmid") or "").strip():
            return True
        return False

    def _extract_uploaded_message(self, upload_data: Any) -> Optional[dict[str, Any]]:
        if self._is_message_payload(upload_data):
            return upload_data
        if not isinstance(upload_data, dict):
            return None
        nested_message = upload_data.get("message")
        if self._is_message_payload(nested_message):
            return nested_message
        return None

    def _build_unmapped_upload_placeholder(
        self,
        room_id: str,
        upload_data: Any,
        *,
        tmid: Optional[str] = None,
    ) -> dict[str, Any]:
        placeholder: dict[str, Any] = {"rid": room_id}
        if tmid:
            placeholder["tmid"] = tmid
        if isinstance(upload_data, dict):
            uploaded_file = upload_data.get("file")
            if isinstance(uploaded_file, dict):
                file_id = str(uploaded_file.get("_id") or "").strip()
                if file_id:
                    placeholder["_upload_file_id"] = file_id
        return placeholder

    async def _resolve_uploaded_message(
        self,
        room_id: str,
        upload_data: Any,
        *,
        media_kind: str,
        tmid: Optional[str] = None,
        require_mappable_message: bool = True,
    ) -> Optional[dict[str, Any]]:
        message = self._extract_uploaded_message(upload_data)
        if message is not None:
            return message
        if upload_data is None:
            return None

        if not require_mappable_message:
            if isinstance(upload_data, dict):
                return upload_data
            return self._build_unmapped_upload_placeholder(room_id, upload_data, tmid=tmid)

        logger.info(
            "[RocketChatOneBotBridge] %s发送结果未直接返回消息，等待自回显补全映射: room_id=%s",
            media_kind,
            room_id,
        )
        echoed_message = await self.client.await_sent_message_echo(
            room_id,
            timeout=self._UPLOAD_MESSAGE_ECHO_TIMEOUT,
        )
        if echoed_message is not None:
            return echoed_message

        if isinstance(upload_data, dict):
            logger.warning(
                "[RocketChatOneBotBridge] %s上传成功但未返回可映射消息，等待自回显超时: room_id=%s keys=%s",
                media_kind,
                room_id,
                ",".join(sorted(str(key) for key in upload_data.keys())) or "-",
            )
        else:
            logger.warning(
                "[RocketChatOneBotBridge] %s上传成功但返回值不可映射，等待自回显超时: room_id=%s upload_data=%r",
                media_kind,
                room_id,
                upload_data,
            )
        return self._build_unmapped_upload_placeholder(room_id, upload_data, tmid=tmid)

    def _is_e2ee_room_info(self, room_info: dict[str, Any]) -> bool:
        return bool(room_info.get("encrypted") and room_info.get("t") in {"d", "p"})

    async def upload_encrypted_file(
        self,
        room_id: str,
        file_path: str,
        resolved_name: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        if not os.path.exists(file_path):
            logger.error(f"[RocketChatOneBotBridge] 文件不存在: {file_path}")
            return None

        upload = None
        try:
            upload = await self.client.e2ee.prepare_encrypted_upload(
                room_id,
                file_name=resolved_name,
                mime_type=self.infer_upload_content_type(file_path, resolved_name),
                file_path=file_path,
            )
            if not upload:
                logger.warning(
                    f"[RocketChatOneBotBridge][E2EE] 未能准备加密上传数据，已跳过 room_id={room_id!r}"
                )
                return None

            file_content = await self.client.e2ee.build_upload_file_content(room_id, upload)
            if not file_content:
                logger.warning(
                    f"[RocketChatOneBotBridge][E2EE] 未能生成加密文件元数据，已跳过 room_id={room_id!r}"
                )
                return None

            with open(upload.encrypted_path, "rb") as encrypted_fp:
                form = aiohttp.FormData()
                form.add_field(
                    "file",
                    encrypted_fp,
                    filename=upload.encrypted_name,
                    content_type="application/octet-stream",
                )
                form.add_field("content", json_dumps_compact(file_content["encrypted"]))

                upload_resp = await self.post_multipart_json(
                    f"{self.client.config.server_url}/api/v1/rooms.media/{room_id}",
                    form,
                )
            if not upload_resp:
                return None

            uploaded_file = upload_resp.get("file") or {}
            file_id = uploaded_file.get("_id")
            file_url = uploaded_file.get("url")
            if not file_id or not file_url:
                logger.error(
                    f"[RocketChatOneBotBridge][E2EE] rooms.media 响应缺少文件信息: {upload_resp}"
                )
                return None

            confirm_payload = await self.client.e2ee.build_media_confirm_payload(
                room_id,
                upload_id=file_id,
                upload_url=file_url,
                upload=upload,
                text=description,
                tmid=tmid,
            )
            if not confirm_payload:
                logger.warning(
                    f"[RocketChatOneBotBridge][E2EE] 未能生成 mediaConfirm 负载，已跳过 room_id={room_id!r}"
                )
                return None

            data = await self.client._post_json_message(
                f"{self.client.config.server_url}/api/v1/rooms.mediaConfirm/{room_id}/{file_id}",
                confirm_payload,
            )
            return (data or {}).get("message") or data
        finally:
            encrypted_path = str(getattr(upload, "encrypted_path", "") or "")
            if encrypted_path and os.path.exists(encrypted_path):
                try:
                    os.unlink(encrypted_path)
                except OSError as exc:
                    logger.warning(
                        f"[RocketChatOneBotBridge][E2EE] 清理加密临时文件失败: {encrypted_path} error={exc!r}"
                    )

    async def send_remote_media_fallback(
        self,
        room_id: str,
        media_url: str,
        *,
        media_kind: str,
        text: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        fallback_text = f"远程{media_kind}发送失败，原文件链接：{media_url}"
        if text:
            fallback_text = f"{text}\n{fallback_text}".strip()
        return await self.client.send_text(room_id, fallback_text, tmid=tmid)

    async def send_image_url(
        self,
        room_id: str,
        image_url: str,
        text: str = "",
        tmid: Optional[str] = None,
        *,
        require_mappable_message: bool = True,
    ) -> Optional[dict[str, Any]]:
        local_path, cleanup = await self.download_remote_media(image_url, ".png")
        if not local_path:
            room_info = await self.client.get_room_info(room_id)
            if self._is_e2ee_room_info(room_info):
                return await self.send_remote_media_fallback(
                    room_id,
                    image_url,
                    media_kind="图片",
                    text=text,
                    tmid=tmid,
                )
            if text:
                return await self.client._send_structured_message(
                    room_id,
                    text,
                    attachments=[{"image_url": image_url}],
                    tmid=tmid,
                )
            return await self.send_remote_media_fallback(
                room_id,
                image_url,
                media_kind="图片",
                text=text,
                tmid=tmid,
            )

        try:
            return await self.send_image_file(
                room_id,
                local_path,
                description=text,
                tmid=tmid,
                require_mappable_message=require_mappable_message,
            )
        finally:
            if cleanup:
                cleanup()

    async def send_image_file(
        self,
        room_id: str,
        file_path: str,
        description: str = "",
        tmid: Optional[str] = None,
        *,
        require_mappable_message: bool = True,
    ) -> Optional[dict[str, Any]]:
        try:
            filename = os.path.basename(file_path) or "image.png"
            data = await self.upload_local_file(
                room_id,
                file_path,
                filename,
                description=description,
                tmid=tmid,
            )
            return await self._resolve_uploaded_message(
                room_id,
                data,
                media_kind="图片",
                tmid=tmid,
                require_mappable_message=require_mappable_message,
            )
        except FileNotFoundError:
            logger.error(f"[RocketChatOneBotBridge] 图片文件不存在: {file_path}")
            return None
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] 上传图片异常: {exc!r}")
            return None

    async def send_file(
        self,
        room_id: str,
        file_path: str,
        filename: Optional[str] = None,
        description: str = "",
        tmid: Optional[str] = None,
        *,
        require_mappable_message: bool = True,
    ) -> Optional[dict[str, Any]]:
        try:
            resolved_name = filename or os.path.basename(file_path) or "attachment"
            data = await self.upload_local_file(
                room_id,
                file_path,
                resolved_name,
                description=description,
                tmid=tmid,
            )
            return await self._resolve_uploaded_message(
                room_id,
                data,
                media_kind="文件",
                tmid=tmid,
                require_mappable_message=require_mappable_message,
            )
        except FileNotFoundError:
            logger.error(f"[RocketChatOneBotBridge] 文件不存在: {file_path}")
            return None
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] 上传文件异常: {exc!r}")
            return None

    async def download_remote_media(
        self,
        url: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        url = await self.client._normalize_media_url(url)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            logger.warning(f"[RocketChatOneBotBridge] 拒绝下载不支持的媒体协议: {url}")
            return None, None
        if self.client._http_session is None:
            return None, None

        filename = os.path.basename(parsed.path)
        _, ext = os.path.splitext(filename)
        suffix = ext if ext else default_suffix
        tmp_path: str | None = None
        try:
            async with self.client._http_session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
                allow_redirects=True,
                max_redirects=3,
            ) as resp:
                if resp.status >= 400:
                    logger.error(f"[RocketChatOneBotBridge] 下载媒体失败 {resp.status}: {url}")
                    return None, None

                limit = self._remote_media_size_limit()
                content_length = resp.content_length
                if content_length is not None and content_length > limit:
                    self._log_media_size_limit_error(
                        "下载媒体失败",
                        actual_size=content_length,
                        limit=limit,
                        source=url,
                    )
                    return None, None

                if not ext:
                    suffix = self._guess_suffix_from_content_type(
                        str(resp.headers.get("Content-Type") or ""),
                        default_suffix,
                    )

                tmp = self._create_media_temp_file(suffix)
                tmp_path = tmp.name
                try:
                    downloaded = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        downloaded += len(chunk)
                        if downloaded > limit:
                            self._log_media_size_limit_error(
                                "下载媒体失败",
                                actual_size=downloaded,
                                limit=limit,
                                source=url,
                            )
                            tmp.close()
                            os.unlink(tmp_path)
                            return None, None
                        tmp.write(chunk)
                    tmp.close()
                    return tmp_path, lambda path=tmp_path: os.unlink(path)
                except Exception:
                    tmp.close()
                    if tmp_path and os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    raise
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] 下载媒体异常: {exc!r}")
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            return None, None

    def decode_base64_media(
        self,
        file_ref: str,
        default_suffix: str,
    ) -> tuple[str | None, Callable[[], None] | None]:
        encoded = "".join(file_ref[len("base64://") :].split())
        limit = self._remote_media_size_limit()
        estimated_size = (len(encoded) // 4) * 3
        if estimated_size > limit + 2:
            self._log_media_size_limit_error(
                "Base64 媒体处理失败",
                actual_size=estimated_size,
                limit=limit,
                source="base64://",
            )
            return None, None

        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] Base64 媒体处理失败: {exc!r}")
            return None, None

        if len(raw) > limit:
            self._log_media_size_limit_error(
                "Base64 媒体处理失败",
                actual_size=len(raw),
                limit=limit,
                source="base64://",
            )
            return None, None

        tmp = self._create_media_temp_file(default_suffix)
        try:
            tmp.write(raw)
            tmp.close()
            return tmp.name, lambda: os.unlink(tmp.name)
        except Exception:
            tmp.close()
            os.unlink(tmp.name)
            raise

def summarize_unsupported_media(raw_msg: dict) -> str | None:
    attachment_count = 0
    attachments = raw_msg.get("attachments")
    if isinstance(attachments, dict):
        attachment_count += 1
    elif isinstance(attachments, list):
        attachment_count += len([item for item in attachments if isinstance(item, dict)])

    file_count = 0
    if raw_msg.get("file"):
        file_count += 1
    files = raw_msg.get("files")
    if isinstance(files, list):
        file_count += len(files)

    if attachment_count == 0 and file_count == 0:
        return None

    total = attachment_count + file_count
    return f"[当前仍有未识别媒体消息，共 {total} 个媒体项]"

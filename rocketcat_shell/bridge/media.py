from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import tempfile
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import aiohttp

from rocketcat_shell.logger import logger

from .json_codec import json_dumps_compact, json_loads


class RocketChatMediaBridge:
    _PLAIN_UPLOAD_LEGACY_ENDPOINT = "rooms.upload"
    _PLAIN_UPLOAD_MODERN_ENDPOINT = "rooms.media"
    _ENDPOINT_COMPATIBILITY_FAILURE_STATUSES = {404, 405, 410, 501}
    _UPLOAD_MESSAGE_ECHO_TIMEOUT = 5.0

    def __init__(self, client: Any) -> None:
        self.client = client
        self._plain_upload_endpoint_preference: str | None = None

    def _resolve_shared_media_dir(self) -> str:
        target_dir = str(os.environ.get("ROCKETCAT_SHARED_MEDIA_DIR") or "").strip()
        if not target_dir:
            target_dir = "/app/data/bots/_shared_media"
        os.makedirs(target_dir, exist_ok=True)
        return target_dir

    def _create_shared_media_temp_file(self, suffix: str):
        return tempfile.NamedTemporaryFile(
            suffix=suffix,
            delete=False,
            dir=self._resolve_shared_media_dir(),
        )

    def translate_shared_media_path_to_host(self, file_ref: str) -> str:
        candidate = str(file_ref or "").strip()
        if not candidate:
            return ""

        host_dir = str(os.environ.get("ROCKETCAT_SHARED_MEDIA_HOST_DIR") or "").strip()
        if not host_dir:
            return candidate

        container_dir = self._resolve_shared_media_dir().replace("\\", "/").rstrip("/")
        normalized_candidate = candidate.replace("\\", "/")
        if normalized_candidate == container_dir:
            relative_path = ""
        elif normalized_candidate.startswith(container_dir + "/"):
            relative_path = normalized_candidate[len(container_dir) + 1 :]
        else:
            return candidate

        host_base = host_dir.rstrip("/\\")
        if not relative_path:
            return host_base or host_dir

        separator = "\\" if "\\" in host_dir and "/" not in host_dir else "/"
        suffix = relative_path.replace("/", separator)
        if not host_base:
            return suffix
        return f"{host_base}{separator}{suffix}"

    def _is_base64_media_transport_enabled(self) -> bool:
        return bool(getattr(self.client, "enable_base64_media_transport", False))

    def _encode_media_file_to_base64(self, file_path: str) -> str | None:
        candidate = str(file_path or "").strip()
        if not candidate:
            return None

        try:
            with open(candidate, "rb") as fp:
                return f"base64://{base64.b64encode(fp.read()).decode('ascii')}"
        except Exception as exc:
            logger.warning(
                "[RocketChatOneBotBridge] Base64 媒体编码失败，已回退到路径模式: %s error=%r",
                candidate,
                exc,
            )
            return None

    def resolve_onebot_media_file_ref(self, media: dict[str, Any]) -> str:
        file_ref = str(media.get("path") or media.get("url") or "")
        if not file_ref:
            return ""

        local_path = str(media.get("path") or "").strip()
        if self._is_base64_media_transport_enabled() and local_path:
            base64_ref = self._encode_media_file_to_base64(local_path)
            if base64_ref:
                return base64_ref

        return self.translate_shared_media_path_to_host(file_ref)

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
                if (
                    content_length is not None
                    and content_length > self.client.config.remote_media_max_size
                ):
                    logger.error(
                        f"[RocketChatOneBotBridge] 下载媒体失败，文件过大: {content_length} > {self.client.config.remote_media_max_size} ({url})"
                    )
                    return None

                raw = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    raw.extend(chunk)
                    if len(raw) > self.client.config.remote_media_max_size:
                        logger.error(
                            f"[RocketChatOneBotBridge] 下载媒体失败，文件超过限制: {len(raw)} > {self.client.config.remote_media_max_size} ({url})"
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
        tmp = self._create_shared_media_temp_file(suffix)
        try:
            tmp.write(raw)
            tmp.close()
            return tmp.name
        except Exception:
            tmp.close()
            os.unlink(tmp.name)
            raise

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
        return {"name": str(name), "path": local_path}

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

    def infer_upload_content_type(self, file_path: str, filename: str) -> str:
        guessed_type, _ = mimetypes.guess_type(filename)
        if guessed_type:
            return guessed_type

        guessed_type, _ = mimetypes.guess_type(file_path)
        if guessed_type:
            return guessed_type

        try:
            with open(file_path, "rb") as fp:
                header = fp.read(16)
        except Exception:
            return "application/octet-stream"

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

        return "application/octet-stream"

    def _normalize_plain_upload_endpoint(self, endpoint_name: str | None) -> str:
        if endpoint_name == self._PLAIN_UPLOAD_MODERN_ENDPOINT:
            return self._PLAIN_UPLOAD_MODERN_ENDPOINT
        return self._PLAIN_UPLOAD_LEGACY_ENDPOINT

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
            if description:
                if endpoint_name == self._PLAIN_UPLOAD_LEGACY_ENDPOINT:
                    form.add_field("msg", description)
                else:
                    form.add_field("description", description)
                    form.add_field("msg", description)
            if tmid:
                form.add_field("tmid", tmid)
            return await self.post_multipart_json_result(url, form)

    def _build_plain_media_confirm_payload(
        self,
        *,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if description:
            payload["msg"] = description
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
        description: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        uploaded_file = upload_data.get("file") or {}
        if not isinstance(uploaded_file, dict):
            logger.error(
                "[RocketChatOneBotBridge] plain media upload 响应缺少 file 对象，无法继续 mediaConfirm: room_id=%s data=%s",
                room_id,
                upload_data,
            )
            return None

        file_id = str(uploaded_file.get("_id") or "").strip()
        if not file_id:
            logger.error(
                "[RocketChatOneBotBridge] plain media upload 响应缺少 file._id，无法继续 mediaConfirm: room_id=%s data=%s",
                room_id,
                upload_data,
            )
            return None

        payload = self._build_plain_media_confirm_payload(
            description=description,
            tmid=tmid,
        )
        data = await self.client._post_json_message(
            f"{self.client.config.server_url}/api/v1/rooms.mediaConfirm/{room_id}/{file_id}",
            payload,
        )
        if not data:
            logger.warning(
                "[RocketChatOneBotBridge] plain mediaConfirm 失败，准备回退到自回显兜底: room_id=%s file_id=%s",
                room_id,
                file_id,
            )
            return None

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
        description: str = "",
        tmid: Optional[str] = None,
    ) -> dict[str, Any]:
        if endpoint_name != self._PLAIN_UPLOAD_MODERN_ENDPOINT:
            return upload_data
        if self._extract_uploaded_message(upload_data) is not None:
            return upload_data

        confirmed_data = await self._confirm_plain_uploaded_file(
            room_id,
            upload_data,
            description=description,
            tmid=tmid,
        )
        return confirmed_data or upload_data

    async def upload_plain_file(
        self,
        room_id: str,
        file_path: str,
        resolved_name: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        primary_endpoint = self._normalize_plain_upload_endpoint(self._plain_upload_endpoint_preference)
        primary_result = await self._upload_plain_file_via_endpoint(
            primary_endpoint,
            room_id,
            file_path,
            resolved_name,
            description=description,
            tmid=tmid,
        )
        primary_data = primary_result.get("data")
        if primary_result.get("ok") and isinstance(primary_data, dict):
            self._plain_upload_endpoint_preference = primary_endpoint
            return await self._finalize_plain_upload_response(
                primary_endpoint,
                room_id,
                primary_data,
                description=description,
                tmid=tmid,
            )

        if not self._is_plain_upload_endpoint_incompatible(primary_result):
            return None

        fallback_endpoint = self._alternate_plain_upload_endpoint(primary_endpoint)
        logger.warning(
            "[RocketChatOneBotBridge] 检测到 plain upload 端点不兼容，准备回退: server=%s endpoint=%s status=%s content_type=%s body=%s",
            self.client.config.server_url,
            primary_endpoint,
            primary_result.get("status") or "-",
            primary_result.get("content_type") or "-",
            self._summarize_response_body(str(primary_result.get("text") or "")),
        )
        fallback_result = await self._upload_plain_file_via_endpoint(
            fallback_endpoint,
            room_id,
            file_path,
            resolved_name,
            description=description,
            tmid=tmid,
        )
        fallback_data = fallback_result.get("data")
        if fallback_result.get("ok") and isinstance(fallback_data, dict):
            previous_endpoint = self._plain_upload_endpoint_preference
            self._plain_upload_endpoint_preference = fallback_endpoint
            if previous_endpoint != fallback_endpoint:
                logger.info(
                    "[RocketChatOneBotBridge] plain upload 端点已切换: server=%s from=%s to=%s",
                    self.client.config.server_url,
                    previous_endpoint or primary_endpoint,
                    fallback_endpoint,
                )
            return await self._finalize_plain_upload_response(
                fallback_endpoint,
                room_id,
                fallback_data,
                description=description,
                tmid=tmid,
            )
        return None

    async def upload_local_file(
        self,
        room_id: str,
        file_path: str,
        resolved_name: str,
        description: str = "",
        tmid: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
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

                limit = self.client.config.remote_media_max_size
                content_length = resp.content_length
                if content_length is not None and content_length > limit:
                    logger.error(
                        f"[RocketChatOneBotBridge] 下载媒体失败，文件过大: {content_length} > {limit} ({url})"
                    )
                    return None, None

                if not ext:
                    suffix = self._guess_suffix_from_content_type(
                        str(resp.headers.get("Content-Type") or ""),
                        default_suffix,
                    )

                tmp = self._create_shared_media_temp_file(suffix)
                tmp_path = tmp.name
                try:
                    downloaded = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        downloaded += len(chunk)
                        if downloaded > limit:
                            logger.error(
                                f"[RocketChatOneBotBridge] 下载媒体失败，文件超过限制: {downloaded} > {limit} ({url})"
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
        limit = self.client.config.remote_media_max_size
        estimated_size = (len(encoded) // 4) * 3
        if estimated_size > limit + 2:
            logger.error(
                f"[RocketChatOneBotBridge] Base64 媒体处理失败，文件过大: {estimated_size} > {limit}"
            )
            return None, None

        try:
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            logger.error(f"[RocketChatOneBotBridge] Base64 媒体处理失败: {exc!r}")
            return None, None

        if len(raw) > limit:
            logger.error(
                f"[RocketChatOneBotBridge] Base64 媒体处理失败，文件超过限制: {len(raw)} > {limit}"
            )
            return None, None

        tmp = self._create_shared_media_temp_file(default_suffix)
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

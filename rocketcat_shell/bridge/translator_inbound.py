from __future__ import annotations

import asyncio
import re
import time
from copy import deepcopy
from typing import Any
from urllib.parse import parse_qs, urlparse

from rocketcat_shell.logger import logger

from .id_map import DurableIdMap
from .media import summarize_unsupported_media
from .perf import PerfTrace, perf_stage
from .rocketchat_client import RocketChatClient
from .storage import ContextRoomStore, MessageStore, PrivateRoomStore


class InboundTranslator:
    _QUOTE_PATTERN = re.compile(r"\[[^\]]*\]\(([^)]*msg=[^)]*)\)|(https?://\S*msg=\S+)", re.IGNORECASE)
    _MAX_QUOTE_DEPTH = 2

    def __init__(
        self,
        rocketchat: RocketChatClient,
        id_map: DurableIdMap,
        messages: MessageStore,
        private_rooms: PrivateRoomStore,
        context_rooms: ContextRoomStore,
        self_id: int,
    ):
        self._rocketchat = rocketchat
        self._id_map = id_map
        self._messages = messages
        self._private_rooms = private_rooms
        self._context_rooms = context_rooms
        self._self_id = self_id

    async def translate(self, raw_msg: dict, *, perf_trace: PerfTrace | None = None) -> dict | None:
        room_id = str(raw_msg.get("rid") or "")
        sender = raw_msg.get("u", {}) or {}
        sender_source_id = str(sender.get("_id") or "")
        source_message_id = str(raw_msg.get("_id") or "")
        if not room_id or not sender_source_id or not source_message_id:
            return None

        quote_contexts_task = asyncio.create_task(
            self._build_quote_contexts(raw_msg, max_depth=self._MAX_QUOTE_DEPTH)
        )
        runtime_batch = self._begin_runtime_batch()
        try:
            with perf_stage(perf_trace, "room_lookup"):
                room_info = await self._rocketchat.get_room_info(room_id)
                room_type = str(room_info.get("t") or "c")
            with perf_stage(perf_trace, "mapping_alloc"):
                room_mapping = await self._get_or_create_mapping("room", room_id, batch=runtime_batch)
                sender_mapping = await self._get_or_create_mapping("user", sender_source_id, batch=runtime_batch)
                message_mapping = await self._get_or_create_mapping("message", source_message_id, batch=runtime_batch)
                context_source_id = self._build_group_context_source_id(room_type, room_id)
                context_surrogate_id = await self._resolve_context_surrogate_id(
                    room_type=room_type,
                    room_mapping=room_mapping,
                    context_source_id=context_source_id,
                    batch=runtime_batch,
                )
            room_name = self._resolve_room_display_name(room_info, room_id)
            room_slug = self._resolve_room_slug(room_info, room_id)
            room_context_label = self._format_room_context_label(room_type, room_name)
            sender_name = sender.get("name") or sender.get("username") or sender_source_id
            thread_source_id = str(raw_msg.get("tmid") or "").strip()
            timestamp = self._extract_timestamp(raw_msg)

            with perf_stage(perf_trace, "room_bindings"):
                if room_type == "d":
                    await self._bind_private_room(
                        sender_source_id,
                        sender_mapping.surrogate_id,
                        room_id,
                        batch=runtime_batch,
                    )
                elif context_surrogate_id is not None:
                    await self._bind_context_room(
                        context_source_id=context_source_id,
                        context_surrogate_id=context_surrogate_id,
                        room_source_id=room_id,
                        room_surrogate_id=room_mapping.surrogate_id,
                        room_name=room_name,
                        room_slug=room_slug,
                        thread_source_id=thread_source_id,
                        timestamp=timestamp,
                        batch=runtime_batch,
                    )

            reply_source_id, cleaned_text = self._extract_reply_source_id(raw_msg)
            current_input_text = cleaned_text.strip()
            with perf_stage(perf_trace, "mention_segments"):
                message_segments, cleaned_text = await self._build_mention_segments(
                    raw_msg,
                    cleaned_text,
                    batch=runtime_batch,
                )
            with perf_stage(perf_trace, "quote_contexts"):
                quote_contexts = await quote_contexts_task
            with perf_stage(perf_trace, "mention_metadata"):
                mention_metadata = await self._extract_mention_metadata(raw_msg, batch=runtime_batch)
            mention_display_names = [
                str(item.get("name") or "")
                for item in mention_metadata
                if str(item.get("name") or "")
            ]
            with perf_stage(perf_trace, "context_media"):
                current_media = await self._extract_context_media_descriptors(raw_msg)
            with perf_stage(perf_trace, "media_segments"):
                media_segments = self._rocketchat.media.build_onebot_segments_from_descriptors(current_media)
            quote_media_segments = self._build_quote_media_segments(
                quote_contexts,
                max_depth=self._MAX_QUOTE_DEPTH,
            )
            quote_context_block = self._format_quote_context_block(quote_contexts)

            segments: list[dict] = []
            if reply_source_id:
                reply_mapping = await self._get_or_create_mapping(
                    "message",
                    reply_source_id,
                    batch=runtime_batch,
                )
                segments.append({"type": "reply", "data": {"id": str(reply_mapping.surrogate_id)}})
            segments.extend(message_segments)

            message_text = cleaned_text.strip()
            current_message_line = self._format_current_message_line(
                room_context_label=room_context_label,
                sender_name=sender_name,
                message_text=message_text,
                mention_names=mention_display_names,
                media=current_media,
            )

            segments.extend(quote_media_segments)
            segments.extend(media_segments)

            if not segments:
                media_placeholder = summarize_unsupported_media(raw_msg)
                if media_placeholder:
                    segments.append({"type": "text", "data": {"text": media_placeholder}})
                    message_text = media_placeholder
                    current_input_text = media_placeholder

            if not segments:
                return None

            direct_reply_context = quote_contexts[0] if quote_contexts else {}
            combined_raw_message = self._compose_raw_message(
                current_message_text=current_input_text,
                fallback=message_text,
            )
            event = {
                "time": timestamp,
                "self_id": self._self_id,
                "post_type": "message",
                "message_type": "private" if room_type == "d" else "group",
                "sub_type": "friend" if room_type == "d" else "normal",
                "message_id": message_mapping.surrogate_id,
                "user_id": sender_mapping.surrogate_id,
                "message": segments,
                "raw_message": combined_raw_message,
                "font": 0,
                "sender": {
                    "user_id": sender_mapping.surrogate_id,
                    "nickname": sender_name,
                    "card": sender_name,
                },
                "message_format": "array",
                "rocketchat_sender_name": sender_name,
                "rocketchat_sender_username": str(sender.get("username") or ""),
                "rocketchat_sender_source_id": sender_source_id,
                "rocketchat_sender_surrogate_id": sender_mapping.surrogate_id,
                "rocketchat_mentions": mention_metadata,
                "rocketchat_quote_contexts": quote_contexts,
                "rocketchat_quote_context_text": quote_context_block,
                "rocketchat_current_message_input_text": current_input_text,
                "rocketchat_quote_media_segments": quote_media_segments,
                "rocketchat_current_message_text": message_text,
                "rocketchat_current_message_line": current_message_line,
                "rocketchat_reply_source_id": reply_source_id,
                "rocketchat_reply_sender_name": str(direct_reply_context.get("sender_name") or ""),
                "rocketchat_reply_message_text": str(direct_reply_context.get("text") or ""),
                "rocketchat_room_source_id": room_id,
                "rocketchat_room_name": room_name,
                "rocketchat_room_slug": room_slug,
                "rocketchat_room_label": room_context_label,
                "rocketchat_room_surrogate_id": room_mapping.surrogate_id,
                "rocketchat_context_source_id": context_source_id,
                "rocketchat_context_group_id": context_surrogate_id,
                "rocketchat_thread_source_id": thread_source_id,
            }

            if room_type != "d":
                event["group_id"] = context_surrogate_id if context_surrogate_id is not None else room_mapping.surrogate_id
                event["group_name"] = room_name

            if quote_contexts:
                logger.info(
                    self._format_inbound_quote_log(
                        room_context_label=room_context_label,
                        sender_name=sender_name,
                        sender_surrogate_id=sender_mapping.surrogate_id,
                        current_message_line=current_message_line,
                        quote_contexts=quote_contexts,
                    )
                )
            else:
                logger.info(
                    self._format_inbound_message_log(
                        room_context_label=room_context_label,
                        sender_name=sender_name,
                        sender_surrogate_id=sender_mapping.surrogate_id,
                        current_message_line=current_message_line,
                    )
                )

            with perf_stage(perf_trace, "message_store"):
                await self._put_message_entry(
                    {
                        "source_id": source_message_id,
                        "surrogate_id": message_mapping.surrogate_id,
                        "room_source_id": room_id,
                        "room_surrogate_id": room_mapping.surrogate_id,
                        "room_type": room_type,
                        "room_name": room_name,
                        "room_slug": room_slug,
                        "context_source_id": context_source_id,
                        "context_surrogate_id": context_surrogate_id,
                        "sender_source_id": sender_source_id,
                        "sender_surrogate_id": sender_mapping.surrogate_id,
                        "sender_name": sender_name,
                        "sender_username": str(sender.get("username") or ""),
                        "mention_metadata": mention_metadata,
                        "input_text": current_input_text,
                        "text": message_text,
                        "quote_contexts": quote_contexts,
                        "quote_context_text": quote_context_block,
                        "reply_sender_name": str(direct_reply_context.get("sender_name") or ""),
                        "reply_message_text": str(direct_reply_context.get("text") or ""),
                        "timestamp": timestamp,
                        "onebot_message_segments": segments,
                        "raw_message": combined_raw_message,
                        "self_id": self._self_id,
                        "reply_source_id": reply_source_id,
                        "quote_media_segments": quote_media_segments,
                        "current_message_line": current_message_line,
                        "room_label": room_context_label,
                        "group_id": event.get("group_id"),
                        "group_name": event.get("group_name"),
                        "thread_source_id": thread_source_id,
                    },
                    batch=runtime_batch,
                )
            return event
        finally:
            await self._cancel_pending_task(quote_contexts_task)
            with perf_stage(perf_trace, "batch_commit"):
                self._commit_runtime_batch(runtime_batch)

    async def hydrate(self, surrogate_message_id: int | str) -> dict | None:
        cached = await self._messages.get_by_surrogate(surrogate_message_id)
        cached_event = self._extract_cached_event(cached)
        source_id = str(cached.get("source_id") or "") if isinstance(cached, dict) else ""
        if cached_event and not self._should_refresh_cached_reply_message(cached):
            return cached_event

        if not source_id:
            resolved_source_id = await self._id_map.get_source("message", surrogate_message_id)
            source_id = str(resolved_source_id or "")
        if not source_id:
            return cached_event
        raw_msg = await self._rocketchat.fetch_message_by_id(source_id)
        if not raw_msg:
            return cached_event
        return await self.translate(raw_msg)

    def _extract_cached_event(self, cached: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(cached, dict):
            return None
        cached_event = cached.get("onebot_message")
        if isinstance(cached_event, dict):
            return cached_event
        return self._rebuild_cached_event(cached)

    def _rebuild_cached_event(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        source_id = str(entry.get("source_id") or "").strip()
        surrogate_id = entry.get("surrogate_id")
        sender_surrogate_id = entry.get("sender_surrogate_id")
        if not source_id or surrogate_id is None or sender_surrogate_id is None:
            return None

        room_type = str(entry.get("room_type") or "c")
        room_name = str(entry.get("room_name") or "")
        room_label = str(entry.get("room_label") or self._format_room_context_label(room_type, room_name))
        message_text = str(entry.get("text") or "")
        current_input_text = str(entry.get("input_text") or message_text)
        sender_name = str(entry.get("sender_name") or entry.get("sender_source_id") or "")
        quote_contexts = deepcopy(entry.get("quote_contexts") or [])
        quote_media_segments = deepcopy(entry.get("quote_media_segments") or [])
        segments = deepcopy(entry.get("onebot_message_segments") or [])
        timestamp = int(entry.get("timestamp") or time.time())
        context_surrogate_id = entry.get("context_surrogate_id")
        direct_reply_context = quote_contexts[0] if quote_contexts else {}

        event: dict[str, Any] = {
            "time": timestamp,
            "self_id": int(entry.get("self_id") or self._self_id),
            "post_type": "message",
            "message_type": "private" if room_type == "d" else "group",
            "sub_type": "friend" if room_type == "d" else "normal",
            "message_id": int(surrogate_id),
            "user_id": int(sender_surrogate_id),
            "message": segments,
            "raw_message": str(entry.get("raw_message") or current_input_text or message_text),
            "font": 0,
            "sender": {
                "user_id": int(sender_surrogate_id),
                "nickname": sender_name,
                "card": sender_name,
            },
            "message_format": "array",
            "rocketchat_sender_name": sender_name,
            "rocketchat_sender_username": str(entry.get("sender_username") or ""),
            "rocketchat_sender_source_id": str(entry.get("sender_source_id") or ""),
            "rocketchat_sender_surrogate_id": int(sender_surrogate_id),
            "rocketchat_mentions": deepcopy(entry.get("mention_metadata") or []),
            "rocketchat_quote_contexts": quote_contexts,
            "rocketchat_quote_context_text": str(entry.get("quote_context_text") or ""),
            "rocketchat_current_message_input_text": current_input_text,
            "rocketchat_quote_media_segments": quote_media_segments,
            "rocketchat_current_message_text": message_text,
            "rocketchat_current_message_line": str(
                entry.get("current_message_line")
                or self._format_current_message_line(
                    room_context_label=room_label,
                    sender_name=sender_name,
                    message_text=message_text,
                    mention_names=[
                        str(item.get("name") or "")
                        for item in (entry.get("mention_metadata") or [])
                        if isinstance(item, dict) and str(item.get("name") or "")
                    ],
                    media=[],
                )
            ),
            "rocketchat_reply_source_id": str(entry.get("reply_source_id") or ""),
            "rocketchat_reply_sender_name": str(
                entry.get("reply_sender_name") or direct_reply_context.get("sender_name") or ""
            ),
            "rocketchat_reply_message_text": str(
                entry.get("reply_message_text") or direct_reply_context.get("text") or ""
            ),
            "rocketchat_room_source_id": str(entry.get("room_source_id") or ""),
            "rocketchat_room_name": room_name,
            "rocketchat_room_slug": str(entry.get("room_slug") or ""),
            "rocketchat_room_label": room_label,
            "rocketchat_room_surrogate_id": int(entry.get("room_surrogate_id") or 0),
            "rocketchat_context_source_id": str(entry.get("context_source_id") or ""),
            "rocketchat_context_group_id": context_surrogate_id,
            "rocketchat_thread_source_id": str(entry.get("thread_source_id") or ""),
        }

        if room_type != "d":
            group_id = entry.get("group_id")
            if group_id is None:
                group_id = context_surrogate_id if context_surrogate_id is not None else entry.get("room_surrogate_id")
            event["group_id"] = int(group_id)
            event["group_name"] = str(entry.get("group_name") or room_name)
        return event

    def _should_refresh_cached_reply_message(self, cached: dict[str, Any] | None) -> bool:
        if not isinstance(cached, dict):
            return False
        if cached.get("quote_contexts"):
            return True
        if cached.get("reply_source_id"):
            return True
        event = cached.get("onebot_message")
        if not isinstance(event, dict):
            return False
        return bool(event.get("rocketchat_reply_source_id"))

    def _begin_runtime_batch(self) -> Any | None:
        begin_batch = getattr(self._id_map, "begin_batch", None)
        if callable(begin_batch):
            return begin_batch()
        return None

    def _commit_runtime_batch(self, batch: Any = None) -> None:
        if batch is None:
            return
        has_pending = getattr(batch, "has_pending", None)
        commit = getattr(batch, "commit", None)
        if callable(has_pending) and callable(commit) and has_pending():
            commit()

    async def _cancel_pending_task(self, task: asyncio.Task[Any] | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return

    async def _get_or_create_mapping(self, namespace: str, source_id: str, *, batch: Any = None) -> Any:
        if batch is not None:
            getter = getattr(batch, "get_or_create_mapping", None)
            if callable(getter):
                return getter(namespace, source_id)
        return await self._id_map.get_or_create(namespace, source_id)

    async def _bind_private_room(
        self,
        user_source_id: str,
        user_surrogate_id: int,
        room_source_id: str,
        *,
        batch: Any = None,
    ) -> None:
        if batch is not None:
            binder = getattr(batch, "bind_private_room", None)
            if callable(binder):
                binder(user_source_id, user_surrogate_id, room_source_id)
                return
        await self._private_rooms.bind(user_source_id, user_surrogate_id, room_source_id)

    async def _bind_context_room(self, *, batch: Any = None, **kwargs) -> None:
        if batch is not None:
            binder = getattr(batch, "bind_context_room", None)
            if callable(binder):
                binder(**kwargs)
                return
        await self._context_rooms.bind(**kwargs)

    async def _put_message_entry(self, entry: dict[str, Any], *, batch: Any = None) -> None:
        if batch is not None:
            putter = getattr(batch, "put_message", None)
            if callable(putter):
                putter(entry)
                return
        await self._messages.put(entry)

    async def _build_mention_segments(self, raw_msg: dict, text: str, *, batch: Any = None) -> tuple[list[dict], str]:
        mentions = raw_msg.get("mentions")
        if not isinstance(mentions, list) or not mentions:
            stripped = text.strip()
            return self._build_text_segments(stripped), stripped

        segments: list[dict] = []
        text_parts: list[str] = []
        unmatched_segments: list[dict] = []
        cursor = 0

        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mention_id = mention.get("_id")
            if not mention_id:
                continue

            mention_segment = await self._build_mention_segment(mention, batch=batch)
            if mention_segment is None:
                continue

            username = mention.get("username")
            token = f"@{username}" if username else ""
            if not token:
                unmatched_segments.append(mention_segment)
                continue

            position = text.find(token, cursor)
            if position < 0:
                unmatched_segments.append(mention_segment)
                continue

            if position > cursor:
                chunk = text[cursor:position]
                text_parts.append(chunk)
                self._append_text_segment(segments, chunk)

            segments.append(mention_segment)
            cursor = position + len(token)

        if cursor < len(text):
            chunk = text[cursor:]
            text_parts.append(chunk)
            self._append_text_segment(segments, chunk)

        segments.extend(unmatched_segments)
        return segments, "".join(text_parts).strip()

    async def _build_mention_segment(self, mention: dict[str, Any], *, batch: Any = None) -> dict[str, Any] | None:
        mention_id = mention.get("_id")
        if not mention_id:
            return None

        username = mention.get("username")
        name = mention.get("name") or username or str(mention_id)
        if str(mention_id) == str(self._rocketchat.user_id):
            mention_qq = str(self._self_id)
        else:
            mapping = await self._get_or_create_mapping("user", str(mention_id), batch=batch)
            mention_qq = str(mapping.surrogate_id)
        return {"type": "at", "data": {"qq": mention_qq, "name": name}}

    def _append_text_segment(self, segments: list[dict[str, Any]], text: str) -> None:
        if not text or not text.strip():
            return
        segments.append({"type": "text", "data": {"text": text}})

    def _build_text_segments(self, text: str) -> list[dict[str, Any]]:
        if not text:
            return []
        return [{"type": "text", "data": {"text": text}}]

    def _extract_reply_source_id(self, raw_msg: dict) -> tuple[str | None, str]:
        text = str(raw_msg.get("msg") or "")
        urls = raw_msg.get("urls")
        if isinstance(urls, list):
            for url_obj in urls:
                if not isinstance(url_obj, dict):
                    continue
                parsed_url = url_obj.get("parsedUrl", {})
                if isinstance(parsed_url, dict):
                    query = parsed_url.get("query", {})
                    if isinstance(query, dict) and query.get("msg"):
                        value = query.get("msg")
                        if isinstance(value, list):
                            value = value[0] if value else None
                        if value:
                            return str(value), self._QUOTE_PATTERN.sub("", text).strip()
                candidate = self._extract_message_id_from_url(str(url_obj.get("url") or ""))
                if candidate:
                    return candidate, self._QUOTE_PATTERN.sub("", text).strip()

        attachments = raw_msg.get("attachments")
        if isinstance(attachments, dict):
            attachments = [attachments]
        if isinstance(attachments, list):
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    continue
                candidate = self._extract_message_id_from_url(str(attachment.get("message_link") or ""))
                if candidate:
                    return candidate, self._QUOTE_PATTERN.sub("", text).strip()

        for match in self._QUOTE_PATTERN.finditer(text):
            candidate = self._extract_message_id_from_url(match.group(1) or match.group(2) or "")
            if candidate:
                return candidate, self._QUOTE_PATTERN.sub("", text).strip()

        return None, text

    async def _build_quote_contexts(
        self,
        raw_msg: dict[str, Any],
        *,
        max_depth: int,
    ) -> list[dict[str, Any]]:
        contexts: list[dict[str, Any]] = []
        visited: set[str] = set()
        await self._collect_quote_contexts_from_payload(
            raw_msg,
            contexts=contexts,
            depth=1,
            max_depth=max_depth,
            visited=visited,
        )
        if contexts:
            return contexts

        reply_source_id, _ = self._extract_reply_source_id(raw_msg)
        if not reply_source_id:
            return contexts

        await self._collect_quote_contexts_from_message_id(
            reply_source_id,
            contexts=contexts,
            depth=1,
            max_depth=max_depth,
            visited=visited,
        )
        return contexts

    async def _collect_quote_contexts_from_payload(
        self,
        payload: dict[str, Any],
        *,
        contexts: list[dict[str, Any]],
        depth: int,
        max_depth: int,
        visited: set[str],
    ) -> None:
        if depth > max_depth:
            return

        attachments = payload.get("attachments")
        if isinstance(attachments, dict):
            attachments = [attachments]
        if not isinstance(attachments, list):
            return

        candidates: list[tuple[dict[str, Any], str]] = []
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            source_id = self._extract_message_id_from_url(str(attachment.get("message_link") or ""))
            if not source_id or source_id in visited:
                continue
            visited.add(source_id)
            candidates.append((attachment, source_id))

        if not candidates:
            return

        branches = await asyncio.gather(
            *[
                self._build_quote_context_branch(
                    attachment,
                    source_id,
                    depth=depth,
                    max_depth=max_depth,
                    visited=visited,
                )
                for attachment, source_id in candidates
            ]
        )
        for branch in branches:
            contexts.extend(branch)

    async def _build_quote_context_branch(
        self,
        payload: dict[str, Any],
        source_id: str,
        *,
        depth: int,
        max_depth: int,
        visited: set[str],
    ) -> list[dict[str, Any]]:
        branch = [await self._build_quote_context_entry(payload, source_id, depth)]
        await self._collect_quote_contexts_from_payload(
            payload,
            contexts=branch,
            depth=depth + 1,
            max_depth=max_depth,
            visited=visited,
        )
        return branch

    async def _collect_quote_contexts_from_message_id(
        self,
        source_id: str,
        *,
        contexts: list[dict[str, Any]],
        depth: int,
        max_depth: int,
        visited: set[str],
    ) -> None:
        if depth > max_depth or source_id in visited:
            return

        raw_msg = await self._rocketchat.fetch_message_by_id(source_id)
        if not raw_msg:
            return

        visited.add(source_id)
        contexts.append(await self._build_quote_context_entry(raw_msg, source_id, depth))
        await self._collect_quote_contexts_from_payload(
            raw_msg,
            contexts=contexts,
            depth=depth + 1,
            max_depth=max_depth,
            visited=visited,
        )

    async def _build_quote_context_entry(
        self,
        payload: dict[str, Any],
        source_id: str,
        depth: int,
    ) -> dict[str, Any]:
        return {
            "depth": depth,
            "source_id": source_id,
            "sender_name": self._extract_context_sender_name(payload),
            "text": self._clean_quote_text(payload),
            "media": await self._extract_context_media_descriptors(payload),
        }

    def _build_quote_media_segments(
        self,
        quote_contexts: list[dict[str, Any]],
        *,
        max_depth: int,
    ) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for context in quote_contexts:
            depth = int(context.get("depth") or 0)
            if depth <= 0 or depth > max_depth:
                continue
            for media in context.get("media") or []:
                segment = self._build_media_segment_from_descriptor(media)
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

    def _build_media_segment_from_descriptor(self, media: dict[str, Any]) -> dict[str, Any] | None:
        return self._rocketchat.media.build_onebot_segment_from_descriptor(media)

    async def _extract_context_media_descriptors(
        self,
        payload: dict[str, Any],
    ) -> list[dict[str, str]]:
        return await self._rocketchat.media.extract_media_descriptors(
            payload,
            skip_quote_attachments=True,
        )

    def _extract_context_sender_name(self, payload: dict[str, Any]) -> str:
        sender = payload.get("u") if isinstance(payload.get("u"), dict) else {}
        return str(
            payload.get("author_name")
            or sender.get("name")
            or sender.get("username")
            or sender.get("_id")
            or "未知"
        )

    def _clean_quote_text(self, payload: dict[str, Any]) -> str:
        text = str(payload.get("text") or payload.get("msg") or "")
        return self._QUOTE_PATTERN.sub("", text).strip()

    async def _extract_mention_metadata(self, raw_msg: dict[str, Any], *, batch: Any = None) -> list[dict[str, Any]]:
        mentions = raw_msg.get("mentions")
        if not isinstance(mentions, list):
            return []

        metadata: list[dict[str, Any]] = []
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mention_id = mention.get("_id")
            if not mention_id:
                continue

            segment = await self._build_mention_segment(mention, batch=batch)
            if segment is None:
                continue

            data = segment.get("data") or {}
            metadata.append(
                {
                    "source_id": str(mention_id),
                    "username": str(mention.get("username") or ""),
                    "name": str(mention.get("name") or data.get("name") or mention_id),
                    "qq": str(data.get("qq") or ""),
                }
            )
        return metadata

    def _format_quote_context_block(self, quote_contexts: list[dict[str, Any]]) -> str:
        if not quote_contexts:
            return ""
        return "引用历史上下文：[\n" + self._format_quote_context_lines(quote_contexts) + "\n]"

    def _format_inbound_quote_log(
        self,
        *,
        room_context_label: str,
        sender_name: str,
        sender_surrogate_id: int | str,
        current_message_line: str,
        quote_contexts: list[dict[str, Any]],
    ) -> str:
        lines = [
            "[RocketChatOneBotBridge] 收到 Rocket.Chat 引用消息",
        ]

        if room_context_label:
            lines.append(f"来源房间：{room_context_label}")

        lines.extend(
            [
                f"当前消息：{current_message_line}",
                f"发送者映射：{sender_name}/{sender_surrogate_id}",
            ]
        )

        quote_chain = self._format_quote_chain_summary(sender_name, quote_contexts)
        if quote_chain:
            lines.append(f"引用链：{quote_chain}")

        lines.append("引用历史上下文：")
        lines.extend(self._format_quote_context_log_lines(quote_contexts))
        return "\n".join(lines)

    def _format_inbound_message_log(
        self,
        *,
        room_context_label: str,
        sender_name: str,
        sender_surrogate_id: int | str,
        current_message_line: str,
    ) -> str:
        parts = ["[RocketChatOneBotBridge] 收到 Rocket.Chat 消息"]
        if room_context_label:
            parts.append(f"room={room_context_label}")
        parts.append(f"sender={sender_name}/{sender_surrogate_id}")
        parts.append(f"message={current_message_line}")
        return " | ".join(parts)

    def _format_quote_context_log_payload(self, quote_contexts: list[dict[str, Any]]) -> str:
        return self._format_quote_context_block(quote_contexts)

    def _format_quote_chain_summary(
        self,
        current_sender_name: str,
        quote_contexts: list[dict[str, Any]],
    ) -> str:
        chain = [f"{current_sender_name}(当前消息)"]
        for index, context in enumerate(quote_contexts, start=1):
            depth = int(context.get("depth") or index)
            sender_name = str(context.get("sender_name") or "未知")
            chain.append(f"{sender_name}(第{depth}层)")
        return " -> ".join(chain)

    def _format_quote_context_log_lines(self, quote_contexts: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for index, context in enumerate(quote_contexts, start=1):
            depth = int(context.get("depth") or index)
            lines.append(f"  {index}. 第{depth}层：{self._format_context_message_line(context)}")
        return lines

    def _format_quote_context_lines(self, quote_contexts: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for index, context in enumerate(quote_contexts):
            suffix = "  引用回复：" if index + 1 < len(quote_contexts) else ""
            lines.append(f"    {self._format_context_message_line(context)}{suffix}")

        return "\n".join(lines)

    def _format_context_message_line(self, context: dict[str, Any]) -> str:
        sender_name = str(context.get("sender_name") or "未知")
        content = self._format_message_content(
            message_text=str(context.get("text") or ""),
            media=context.get("media") or [],
        )
        return f"{sender_name}：{content}"

    def _format_current_message_line(
        self,
        *,
        room_context_label: str,
        sender_name: str,
        message_text: str,
        mention_names: list[str],
        media: list[dict[str, str]],
    ) -> str:
        content = self._format_message_content(
            message_text=message_text,
            mention_names=mention_names,
            media=media,
        )
        if room_context_label:
            sender_prefix = sender_name or "未知"
            return f"{room_context_label}：{sender_prefix}：{content}"
        return content

    def _format_message_content(
        self,
        *,
        message_text: str,
        mention_names: list[str] | None = None,
        media: list[dict[str, str]] | None = None,
    ) -> str:
        text = message_text.strip()
        mention_prefix = " ".join(f"@{name}" for name in (mention_names or []) if name)
        if mention_prefix:
            text = " ".join(part for part in (mention_prefix.strip(), text) if part)

        media_text = self._format_media_brief(media or [])
        if text and media_text:
            return f"{text} {media_text}"
        if text:
            return text
        if media_text:
            return media_text
        return "(无纯文本，仅引用上文)"

    def _format_media_brief(self, media: list[dict[str, str]]) -> str:
        parts: list[str] = []
        for item in media:
            kind = str(item.get("kind") or "file")
            name = str(item.get("name") or "attachment")
            if kind == "image":
                parts.append(f"[图片:{name}]")
            elif kind == "audio":
                parts.append(f"[语音:{name}]")
            elif kind == "video":
                parts.append(f"[视频:{name}]")
            else:
                parts.append(f"[文件:{name}]")
        return " ".join(parts)

    def _compose_raw_message(
        self,
        *,
        current_message_text: str,
        fallback: str,
    ) -> str:
        return current_message_text or fallback

    async def _resolve_context_surrogate_id(
        self,
        *,
        room_type: str,
        room_mapping: Any,
        context_source_id: str,
        batch: Any = None,
    ) -> int | None:
        if room_type == "d" or not context_source_id:
            return None
        if getattr(self._rocketchat.config, "enable_subchannel_session_isolation", False):
            return int(room_mapping.surrogate_id)
        context_mapping = await self._get_or_create_mapping("context", context_source_id, batch=batch)
        return None if context_mapping is None else int(context_mapping.surrogate_id)

    def _build_group_context_source_id(self, room_type: str, room_id: str) -> str:
        if room_type == "d":
            return ""
        server_url = str(getattr(self._rocketchat.config, "server_url", "") or "").rstrip("/")
        if getattr(self._rocketchat.config, "enable_subchannel_session_isolation", False):
            return f"rocketchat-room-context::{server_url or 'default'}::{room_id}"
        return f"rocketchat-group-context::{server_url or 'default'}"

    def _resolve_room_display_name(self, room_info: dict[str, Any], room_id: str) -> str:
        return str(room_info.get("fname") or room_info.get("name") or room_id)

    def _resolve_room_slug(self, room_info: dict[str, Any], room_id: str) -> str:
        return str(room_info.get("name") or room_info.get("fname") or room_id)

    def _format_room_context_label(self, room_type: str, room_name: str) -> str:
        if room_type == "d" or not room_name:
            return ""
        return f"子频道：[{room_name}]"

    def _extract_message_id_from_url(self, url: str) -> str | None:
        if not url or "msg=" not in url:
            return None
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        values = query.get("msg")
        if not values:
            return None
        return str(values[0])

    def _extract_timestamp(self, raw_msg: dict) -> int:
        ts = raw_msg.get("ts")
        if isinstance(ts, dict) and "$date" in ts:
            return int(int(ts["$date"]) / 1000)
        return int(time.time())

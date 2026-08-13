from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import inspect
import json
import math
import os
import statistics
import sys
import tempfile
import time
import types
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import psutil


DEFAULT_SELF_ID = 910001


@dataclass(slots=True)
class Scenario:
    name: str
    raw_msg: dict[str, Any]
    quoted_messages: dict[str, dict[str, Any]]


@dataclass(slots=True)
class ScenarioStats:
    mean_ms: float
    median_ms: float
    p95_ms: float
    p99_ms: float
    minimum_ms: float
    maximum_ms: float
    throughput_per_second: float
    elapsed_seconds: float
    cpu_seconds: float
    rss_before_bytes: int
    rss_after_bytes: int
    rss_peak_bytes: int
    threads_before: int
    threads_after: int
    handles_before: int
    handles_after: int


@dataclass(slots=True)
class LoadedBranch:
    name: str
    root: Path
    translator_cls: type
    is_rebuild: bool
    build_runtime_hot_stores: Callable[..., Any] | None = None
    storage_mod: types.ModuleType | None = None
    id_map_cls: type | None = None


class FakeMedia:
    def __init__(self, *, media_delay_seconds: float = 0.0) -> None:
        self.media_delay_seconds = max(0.0, float(media_delay_seconds))

    def get_all_attachments_recursive(
        self,
        payload: dict[str, Any],
        *,
        skip_quote_attachments: bool = False,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        attachments_raw = payload.get("attachments", [])
        attachments = (
            [attachments_raw]
            if isinstance(attachments_raw, dict)
            else [item for item in attachments_raw if isinstance(item, dict)]
        )
        for attachment in attachments:
            if skip_quote_attachments and attachment.get("message_link"):
                continue
            result.append(attachment)
            result.extend(
                self.get_all_attachments_recursive(
                    attachment,
                    skip_quote_attachments=skip_quote_attachments,
                )
            )
        return result

    def classify_file_kind(self, file_obj: dict[str, Any]) -> str:
        for key in ("type", "mimeType", "contentType"):
            value = str(file_obj.get(key) or "")
            if value.startswith("image/"):
                return "image"
            if value.startswith("audio/"):
                return "audio"
            if value.startswith("video/"):
                return "video"

        for key in (
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
            if "image" in key:
                return "image"
            if "audio" in key:
                return "audio"
            if "video" in key:
                return "video"

        return "file"

    async def _materialize_media_reference(
        self,
        file_obj: dict[str, Any],
        target_kind: str,
    ) -> dict[str, str] | None:
        if self.media_delay_seconds > 0:
            await asyncio.sleep(self.media_delay_seconds)

        key_candidates = {
            "image": ("image_url", "imageUrl", "url", "title_link", "titleLink", "path", "link"),
            "audio": ("audio_url", "audioUrl", "url", "title_link", "titleLink", "path", "link"),
            "video": ("video_url", "videoUrl", "url", "title_link", "titleLink", "path", "link"),
            "file": ("url", "title_link", "titleLink", "path", "link"),
        }
        media_url = ""
        for key in key_candidates.get(target_kind, key_candidates["file"]):
            candidate = file_obj.get(key)
            if isinstance(candidate, str) and candidate:
                media_url = candidate
                break
        if not media_url:
            return None

        return {
            "name": self._extract_media_name(file_obj, media_url),
            "url": media_url,
            "path": "",
        }

    async def extract_media_descriptors(
        self,
        payload: dict[str, Any],
        *,
        skip_quote_attachments: bool = True,
        include_url_images: bool = True,
    ) -> list[dict[str, str]]:
        descriptors: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        candidates: list[dict[str, Any]] = []

        def collect_candidates(source: dict[str, Any]) -> None:
            files_raw = source.get("files", [])
            if isinstance(files_raw, dict):
                candidates.append(files_raw)
            elif isinstance(files_raw, list):
                candidates.extend([item for item in files_raw if isinstance(item, dict)])

            for key in ("file", "fileUpload"):
                single_file = source.get(key)
                if isinstance(single_file, dict):
                    candidates.append(single_file)

            if any(
                source.get(key)
                for key in (
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
            ):
                candidates.append(source)

        collect_candidates(payload)
        for attachment in self.get_all_attachments_recursive(
            payload,
            skip_quote_attachments=skip_quote_attachments,
        ):
            collect_candidates(attachment)

        for candidate in candidates:
            kind = self.classify_file_kind(candidate)
            materialized = await self._materialize_media_reference(candidate, kind)
            if not materialized:
                continue
            file_ref = str(materialized.get("path") or materialized.get("url") or "")
            if not file_ref:
                continue
            key = (kind, file_ref)
            if key in seen:
                continue
            seen.add(key)
            descriptors.append(
                {
                    "kind": kind,
                    "name": str(materialized.get("name") or self._extract_media_name(candidate, file_ref)),
                    "url": str(materialized.get("url") or ""),
                    "path": str(materialized.get("path") or ""),
                }
            )

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
                key = ("image", candidate)
                if key in seen:
                    continue
                seen.add(key)
                descriptors.append(
                    {
                        "kind": "image",
                        "name": self._extract_media_name(url_obj, candidate),
                        "url": candidate,
                        "path": "",
                    }
                )

        return descriptors

    def build_onebot_segment_from_descriptor(self, media: dict[str, Any]) -> dict[str, Any] | None:
        kind = str(media.get("kind") or "")
        file_ref = str(media.get("path") or media.get("url") or "")
        if not file_ref:
            return None
        if kind == "image":
            return {"type": "image", "data": {"file": file_ref}}
        if kind == "audio":
            return {"type": "record", "data": {"file": file_ref}}
        if kind == "video":
            return {"type": "video", "data": {"file": file_ref}}
        name = str(media.get("name") or "attachment")
        return {
            "type": "file",
            "data": {
                "url": file_ref,
                "file_name": name,
                "name": name,
            },
        }

    def build_onebot_segments_from_descriptors(
        self,
        media_descriptors: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        segments: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        for media in media_descriptors:
            kind = str(media.get("kind") or "")
            file_ref = str(media.get("path") or media.get("url") or "")
            if not file_ref:
                continue

            if kind == "image":
                key = ("image", file_ref)
                if key in seen:
                    continue
                seen.add(key)
                segments.append({"type": "image", "data": {"file": file_ref}})
                continue

            if kind == "audio":
                key = ("record", file_ref)
                if key in seen:
                    continue
                seen.add(key)
                segments.append({"type": "record", "data": {"file": file_ref}})
                continue

            if kind == "video":
                key = ("video", file_ref)
                if key in seen:
                    continue
                seen.add(key)
                segments.append({"type": "video", "data": {"file": file_ref}})
                continue

            key = ("file", file_ref)
            if key in seen:
                continue
            seen.add(key)
            name = str(media.get("name") or "attachment")
            segments.append(
                {
                    "type": "file",
                    "data": {
                        "url": file_ref,
                        "file_name": name,
                        "name": name,
                    },
                }
            )

        return segments

    async def extract_onebot_segments(self, raw_msg: dict[str, Any]) -> list[dict[str, Any]]:
        descriptors = await self.extract_media_descriptors(raw_msg)
        return self.build_onebot_segments_from_descriptors(descriptors)

    @staticmethod
    def _extract_media_name(payload: dict[str, Any], media_url: str) -> str:
        return str(
            payload.get("name")
            or payload.get("title")
            or payload.get("file_name")
            or Path(urlparse(media_url).path).name
            or "attachment"
        )


class FakeRocketChat:
    def __init__(
        self,
        *,
        quoted_messages: dict[str, dict[str, Any]],
        room_info_delay_seconds: float,
        quote_fetch_delay_seconds: float,
        media_delay_seconds: float,
    ) -> None:
        self.user_id = "bot-user"
        self._quoted_messages = dict(quoted_messages)
        self._room_info_delay_seconds = max(0.0, float(room_info_delay_seconds))
        self._quote_fetch_delay_seconds = max(0.0, float(quote_fetch_delay_seconds))
        self.media = FakeMedia(media_delay_seconds=media_delay_seconds)
        self.config = types.SimpleNamespace(
            server_url="https://example.test",
            enable_subchannel_session_isolation=False,
            remote_media_max_size=20 * 1024 * 1024,
            perf_trace_enabled=False,
        )

    async def get_room_type(self, room_id: str) -> str:
        return "c"

    async def get_room_info(self, room_id: str) -> dict[str, Any]:
        if self._room_info_delay_seconds > 0:
            await asyncio.sleep(self._room_info_delay_seconds)
        return {"_id": room_id, "t": "c", "name": "room-a", "fname": "Room A"}

    async def fetch_message_by_id(self, source_id: str) -> dict[str, Any] | None:
        if self._quote_fetch_delay_seconds > 0:
            await asyncio.sleep(self._quote_fetch_delay_seconds)
        payload = self._quoted_messages.get(str(source_id))
        return copy.deepcopy(payload) if isinstance(payload, dict) else None


def _clear_rocketcat_modules() -> None:
    for name in list(sys.modules):
        if name == "rocketcat_shell" or name.startswith("rocketcat_shell."):
            sys.modules.pop(name, None)


def _load_module(module_name: str, file_path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module: {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_branch(name: str, root: Path) -> LoadedBranch:
    project_root = root / "rocketcat_shell"
    bridge_root = project_root / "bridge"
    if not bridge_root.exists():
        raise FileNotFoundError(f"Bridge directory not found: {bridge_root}")

    _clear_rocketcat_modules()
    rocketcat_pkg = types.ModuleType("rocketcat_shell")
    rocketcat_pkg.__path__ = [str(project_root)]
    sys.modules["rocketcat_shell"] = rocketcat_pkg

    bridge_pkg = types.ModuleType("rocketcat_shell.bridge")
    bridge_pkg.__path__ = [str(bridge_root)]
    sys.modules["rocketcat_shell.bridge"] = bridge_pkg

    media_stub = types.ModuleType("rocketcat_shell.bridge.media")
    media_stub.summarize_unsupported_media = lambda raw_msg: ""
    sys.modules["rocketcat_shell.bridge.media"] = media_stub

    rocketchat_client_stub = types.ModuleType("rocketcat_shell.bridge.rocketchat_client")
    rocketchat_client_stub.RocketChatClient = object
    sys.modules["rocketcat_shell.bridge.rocketchat_client"] = rocketchat_client_stub

    _load_module("rocketcat_shell.logger", project_root / "logger.py")
    storage_mod = _load_module("rocketcat_shell.bridge.storage", bridge_root / "storage.py")
    id_map_mod = _load_module("rocketcat_shell.bridge.id_map", bridge_root / "id_map.py")

    build_runtime_hot_stores = None
    is_rebuild = (bridge_root / "hot_storage.py").exists()
    if (bridge_root / "perf.py").exists():
        _load_module("rocketcat_shell.bridge.perf", bridge_root / "perf.py")
    if is_rebuild:
        hot_storage_mod = _load_module("rocketcat_shell.bridge.hot_storage", bridge_root / "hot_storage.py")
        build_runtime_hot_stores = hot_storage_mod.build_runtime_hot_stores

    translator_mod = _load_module("rocketcat_shell.bridge.translator_inbound", bridge_root / "translator_inbound.py")
    return LoadedBranch(
        name=name,
        root=root,
        translator_cls=translator_mod.InboundTranslator,
        is_rebuild=is_rebuild,
        build_runtime_hot_stores=build_runtime_hot_stores,
        storage_mod=storage_mod,
        id_map_cls=id_map_mod.DurableIdMap,
    )


def make_scenarios() -> dict[str, Scenario]:
    sender = {"_id": "user-1", "name": "Alice", "username": "alice"}
    quoted_sender = {"_id": "user-2", "name": "Bob", "username": "bob"}
    return {
        "text": Scenario(
            name="text",
            raw_msg={
                "_id": "text-msg",
                "rid": "room-1",
                "u": sender,
                "msg": "hello world",
                "ts": {"$date": 1710000000000},
            },
            quoted_messages={},
        ),
        "quote": Scenario(
            name="quote",
            raw_msg={
                "_id": "quote-msg",
                "rid": "room-1",
                "u": sender,
                "msg": "[reply](https://example.test/channel/room-a?msg=quoted-1) answer this",
                "ts": {"$date": 1710000001000},
            },
            quoted_messages={
                "quoted-1": {
                    "_id": "quoted-1",
                    "rid": "room-1",
                    "u": quoted_sender,
                    "msg": "quoted body",
                    "ts": {"$date": 1710000000500},
                }
            },
        ),
        "thread": Scenario(
            name="thread",
            raw_msg={
                "_id": "thread-msg",
                "rid": "room-1",
                "u": sender,
                "msg": "thread reply",
                "tmid": "thread-root-1",
                "ts": {"$date": 1710000002000},
            },
            quoted_messages={},
        ),
        "image": Scenario(
            name="image",
            raw_msg={
                "_id": "image-msg",
                "rid": "room-1",
                "u": sender,
                "msg": "what is in this image",
                "attachments": [
                    {
                        "image_url": "https://example.test/cat.png",
                        "title": "cat.png",
                        "type": "image/png",
                    }
                ],
                "ts": {"$date": 1710000003000},
            },
            quoted_messages={},
        ),
        "quote_image": Scenario(
            name="quote_image",
            raw_msg={
                "_id": "quote-image-msg",
                "rid": "room-1",
                "u": sender,
                "msg": "[reply](https://example.test/channel/room-a?msg=quoted-image-1) please inspect the quoted image",
                "ts": {"$date": 1710000003500},
            },
            quoted_messages={
                "quoted-image-1": {
                    "_id": "quoted-image-1",
                    "rid": "room-1",
                    "u": quoted_sender,
                    "msg": "quoted image payload",
                    "attachments": [
                        {
                            "image_url": "https://example.test/quoted-cat.png",
                            "title": "quoted-cat.png",
                            "type": "image/png",
                        }
                    ],
                    "ts": {"$date": 1710000003200},
                }
            },
        ),
        "media_mix": Scenario(
            name="media_mix",
            raw_msg={
                "_id": "media-mix-msg",
                "rid": "room-1",
                "u": sender,
                "msg": "mixed media payload",
                "attachments": [
                    {
                        "image_url": "https://example.test/cat.png",
                        "title": "cat.png",
                        "type": "image/png",
                    },
                    {
                        "audio_url": "https://example.test/sample.mp3",
                        "title": "sample.mp3",
                    },
                    {
                        "url": "https://example.test/report.pdf",
                        "title": "report.pdf",
                    },
                ],
                "urls": [
                    {
                        "url": "https://example.test/preview.png",
                        "meta": {"contentType": "image/png"},
                    }
                ],
                "ts": {"$date": 1710000003600},
            },
            quoted_messages={},
        ),
    }


def apply_profile_defaults(args: argparse.Namespace) -> argparse.Namespace:
    profile_defaults = {
        "micro": {
            "iterations": 200,
            "warmup": 20,
            "room_info_delay_ms": 0.0,
            "quote_fetch_delay_ms": 0.0,
            "media_delay_ms": 0.0,
            "scenarios": ["text", "quote", "thread", "image"],
        },
        "realistic": {
            "iterations": 120,
            "warmup": 20,
            "room_info_delay_ms": 2.5,
            "quote_fetch_delay_ms": 4.0,
            "media_delay_ms": 1.5,
            "scenarios": ["text", "quote", "thread", "image", "quote_image", "media_mix"],
        },
    }
    defaults = profile_defaults[args.profile]
    if args.iterations is None:
        args.iterations = defaults["iterations"]
    if args.warmup is None:
        args.warmup = defaults["warmup"]
    if args.room_info_delay_ms is None:
        args.room_info_delay_ms = defaults["room_info_delay_ms"]
    if args.quote_fetch_delay_ms is None:
        args.quote_fetch_delay_ms = defaults["quote_fetch_delay_ms"]
    if args.media_delay_ms is None:
        args.media_delay_ms = defaults["media_delay_ms"]
    if not args.scenario:
        args.scenario = list(defaults["scenarios"])
    return args


def create_translator(
    branch: LoadedBranch,
    temp_root: Path,
    args: argparse.Namespace,
    quoted_messages: dict[str, dict[str, Any]],
):
    rocketchat = FakeRocketChat(
        quoted_messages=quoted_messages,
        room_info_delay_seconds=args.room_info_delay_ms / 1000.0,
        quote_fetch_delay_seconds=args.quote_fetch_delay_ms / 1000.0,
        media_delay_seconds=args.media_delay_ms / 1000.0,
    )

    if branch.is_rebuild:
        if branch.build_runtime_hot_stores is None:
            raise RuntimeError("Rebuild branch does not expose build_runtime_hot_stores")
        bundle = branch.build_runtime_hot_stores(
            temp_root / "bot_data",
            message_window_size=args.message_window_size,
        )
        translator = branch.translator_cls(
            rocketchat=rocketchat,
            id_map=bundle.id_map,
            messages=bundle.message_store,
            private_rooms=bundle.private_room_store,
            context_rooms=bundle.context_room_store,
            self_id=DEFAULT_SELF_ID,
        )

        async def cleanup() -> None:
            await asyncio.to_thread(bundle.close)

        return translator, cleanup

    if branch.storage_mod is None or branch.id_map_cls is None:
        raise RuntimeError("Control branch dependencies were not loaded")

    id_map = branch.id_map_cls(
        branch.storage_mod.JsonStore(temp_root / "id_map.json"),
        message_window_size=args.message_window_size,
    )
    messages = branch.storage_mod.MessageStore(branch.storage_mod.JsonStore(temp_root / "message_registry.json"))
    private_rooms = branch.storage_mod.PrivateRoomStore(branch.storage_mod.JsonStore(temp_root / "private_rooms.json"))
    context_rooms = branch.storage_mod.ContextRoomStore(
        branch.storage_mod.JsonStore(temp_root / "context_room_registry.json")
    )
    translator = branch.translator_cls(
        rocketchat=rocketchat,
        id_map=id_map,
        messages=messages,
        private_rooms=private_rooms,
        context_rooms=context_rooms,
        self_id=DEFAULT_SELF_ID,
    )

    async def cleanup() -> None:
        return None

    return translator, cleanup


def _percentile(ordered: list[float], percentile: float) -> float:
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def _handle_count(process: psutil.Process) -> int:
    return int(process.num_handles()) if hasattr(process, "num_handles") else 0


def _materialize_iteration(
    scenario: Scenario,
    index: int,
    *,
    cache_mode: str,
    run_number: int,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    token = f"run{run_number}-item{index}"
    raw_msg = copy.deepcopy(scenario.raw_msg)
    raw_msg["_id"] = f"{raw_msg.get('_id', scenario.name)}-{token}"
    timestamp = raw_msg.get("ts")
    if isinstance(timestamp, dict) and "$date" in timestamp:
        timestamp["$date"] = int(timestamp["$date"]) + index + (run_number * 1_000_000)
    if raw_msg.get("tmid"):
        raw_msg["tmid"] = f"{raw_msg['tmid']}-{token}"
    if cache_mode == "cold":
        raw_msg["rid"] = f"{raw_msg.get('rid', 'room')}-{token}"
        sender = raw_msg.get("u")
        if isinstance(sender, dict):
            sender["_id"] = f"{sender.get('_id', 'user')}-{token}"

    quoted_messages: dict[str, dict[str, Any]] = {}
    for quote_id, quote_payload in scenario.quoted_messages.items():
        unique_quote_id = f"{quote_id}-{token}"
        raw_msg["msg"] = str(raw_msg.get("msg") or "").replace(quote_id, unique_quote_id)
        unique_quote = copy.deepcopy(quote_payload)
        unique_quote["_id"] = unique_quote_id
        if cache_mode == "cold":
            unique_quote["rid"] = raw_msg["rid"]
            quote_sender = unique_quote.get("u")
            if isinstance(quote_sender, dict):
                quote_sender["_id"] = f"{quote_sender.get('_id', 'quote-user')}-{token}"
        quote_timestamp = unique_quote.get("ts")
        if isinstance(quote_timestamp, dict) and "$date" in quote_timestamp:
            quote_timestamp["$date"] = int(quote_timestamp["$date"]) + index + (run_number * 1_000_000)
        quoted_messages[unique_quote_id] = unique_quote
    return raw_msg, quoted_messages


def summarize_timings(
    timings_ms: list[float],
    *,
    elapsed_seconds: float,
    cpu_seconds: float,
    rss_before_bytes: int,
    rss_after_bytes: int,
    rss_peak_bytes: int,
    threads_before: int,
    threads_after: int,
    handles_before: int,
    handles_after: int,
) -> ScenarioStats:
    if not timings_ms:
        raise ValueError("timings_ms cannot be empty")
    ordered = sorted(timings_ms)
    return ScenarioStats(
        mean_ms=statistics.fmean(ordered),
        median_ms=statistics.median(ordered),
        p95_ms=_percentile(ordered, 0.95),
        p99_ms=_percentile(ordered, 0.99),
        minimum_ms=ordered[0],
        maximum_ms=ordered[-1],
        throughput_per_second=len(ordered) / max(elapsed_seconds, 1e-9),
        elapsed_seconds=elapsed_seconds,
        cpu_seconds=cpu_seconds,
        rss_before_bytes=rss_before_bytes,
        rss_after_bytes=rss_after_bytes,
        rss_peak_bytes=rss_peak_bytes,
        threads_before=threads_before,
        threads_after=threads_after,
        handles_before=handles_before,
        handles_after=handles_after,
    )


async def benchmark_scenario(
    branch: LoadedBranch,
    scenario: Scenario,
    args: argparse.Namespace,
    *,
    cache_mode: str,
    execution_mode: str,
    run_number: int,
) -> ScenarioStats:
    total_iterations = max(0, int(args.warmup)) + max(1, int(args.iterations))
    materialized = [
        _materialize_iteration(
            scenario,
            index,
            cache_mode=cache_mode,
            run_number=run_number,
        )
        for index in range(total_iterations)
    ]
    quoted_messages = {
        quote_id: payload
        for _raw, quotes in materialized
        for quote_id, payload in quotes.items()
    }
    process = psutil.Process(os.getpid())
    with tempfile.TemporaryDirectory(
        prefix=f"rocketcat_bench_{branch.name}_{scenario.name}_{cache_mode}_{execution_mode}_"
    ) as temp_dir:
        translator, cleanup = create_translator(
            branch,
            Path(temp_dir),
            args,
            quoted_messages,
        )

        async def translate_one(raw_msg: dict[str, Any]) -> float:
            started_at = time.perf_counter()
            event = await translator.translate(copy.deepcopy(raw_msg))
            elapsed_ms = (time.perf_counter() - started_at) * 1000.0
            if event is None:
                raise RuntimeError(
                    f"{branch.name}:{scenario.name}:{cache_mode}:{execution_mode} returned no event"
                )
            return elapsed_ms

        async def translate_many(payloads: list[dict[str, Any]]) -> list[float]:
            if execution_mode == "serial":
                return [await translate_one(payload) for payload in payloads]
            results: list[float] = []
            concurrency = max(2, int(args.concurrency))
            for offset in range(0, len(payloads), concurrency):
                results.extend(
                    await asyncio.gather(
                        *(translate_one(payload) for payload in payloads[offset : offset + concurrency])
                    )
                )
            return results

        try:
            warmup_payloads = [raw for raw, _quotes in materialized[: int(args.warmup)]]
            if warmup_payloads:
                await translate_many(warmup_payloads)
            measured_payloads = [raw for raw, _quotes in materialized[int(args.warmup) :]]
            cpu_before = process.cpu_times()
            rss_before = process.memory_info().rss
            threads_before = process.num_threads()
            handles_before = _handle_count(process)
            started_at = time.perf_counter()
            timings_ms = await translate_many(measured_payloads)
            elapsed_seconds = time.perf_counter() - started_at
            cpu_after = process.cpu_times()
            rss_after = process.memory_info().rss
            threads_after = process.num_threads()
            handles_after = _handle_count(process)
        finally:
            maybe_awaitable = cleanup()
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        return summarize_timings(
            timings_ms,
            elapsed_seconds=elapsed_seconds,
            cpu_seconds=(cpu_after.user + cpu_after.system) - (cpu_before.user + cpu_before.system),
            rss_before_bytes=rss_before,
            rss_after_bytes=rss_after,
            rss_peak_bytes=max(rss_before, rss_after),
            threads_before=threads_before,
            threads_after=threads_after,
            handles_before=handles_before,
            handles_after=handles_after,
        )


async def run_branch(
    branch: LoadedBranch,
    scenarios: list[Scenario],
    args: argparse.Namespace,
    *,
    run_number: int,
) -> dict[str, ScenarioStats]:
    results: dict[str, ScenarioStats] = {}
    cache_modes = ["warm", "cold"] if args.cache_mode == "both" else [args.cache_mode]
    execution_modes = ["serial", "concurrent"] if args.execution_mode == "both" else [args.execution_mode]
    for scenario in scenarios:
        for cache_mode in cache_modes:
            for execution_mode in execution_modes:
                key = f"{scenario.name}:{cache_mode}:{execution_mode}"
                results[key] = await benchmark_scenario(
                    branch,
                    scenario,
                    args,
                    cache_mode=cache_mode,
                    execution_mode=execution_mode,
                    run_number=run_number,
                )
    return results


def _median_stats(runs: list[dict[str, ScenarioStats]]) -> dict[str, ScenarioStats]:
    result: dict[str, ScenarioStats] = {}
    for key in runs[0]:
        values: dict[str, Any] = {}
        for field in fields(ScenarioStats):
            samples = [getattr(run[key], field.name) for run in runs]
            median_value = statistics.median(samples)
            if field.type is int or field.name.endswith(("_bytes", "_before", "_after")):
                median_value = int(median_value)
            values[field.name] = median_value
        result[key] = ScenarioStats(**values)
    return result


def format_ratio(control_value: float, rebuild_value: float) -> str:
    if control_value <= 0:
        return "n/a"
    delta_percent = ((control_value - rebuild_value) / control_value) * 100.0
    sign = "+" if delta_percent >= 0 else ""
    return f"{sign}{delta_percent:.1f}%"


def print_summary(
    control_results: dict[str, ScenarioStats],
    rebuild_results: dict[str, ScenarioStats],
    args: argparse.Namespace,
) -> None:
    print(
        "settings: profile={profile} iterations={iterations} warmup={warmup} repeat={repeat} concurrency={concurrency}".format(
            profile=args.profile,
            iterations=args.iterations,
            warmup=args.warmup,
            repeat=args.repeat,
            concurrency=args.concurrency,
        )
    )
    print("scenario:cache:mode                    control_p95  rebuild_p95  latency_delta  throughput_delta")
    print("-------------------------------------  -----------  -----------  -------------  ----------------")
    for key in control_results:
        control = control_results[key]
        rebuild = rebuild_results[key]
        throughput_delta = (
            ((rebuild.throughput_per_second - control.throughput_per_second) / control.throughput_per_second) * 100.0
            if control.throughput_per_second > 0
            else 0.0
        )
        print(
            f"{key:<37}  {control.p95_ms:>11.3f}  {rebuild.p95_ms:>11.3f}  "
            f"{format_ratio(control.p95_ms, rebuild.p95_ms):>13}  {throughput_delta:>+15.1f}%"
        )


def parse_args() -> argparse.Namespace:
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Compare unique inbound translation workloads between an explicit control checkout and candidate."
    )
    parser.add_argument("--profile", choices=["micro", "realistic"], default="micro")
    parser.add_argument(
        "--control-root",
        type=Path,
        required=True,
        help="Explicit path to the frozen control checkout",
    )
    parser.add_argument("--rebuild-root", type=Path, default=default_root)
    parser.add_argument("--iterations", type=int, default=None)
    parser.add_argument("--warmup", type=int, default=None)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--cache-mode", choices=["warm", "cold", "both"], default="both")
    parser.add_argument(
        "--execution-mode",
        choices=["serial", "concurrent", "both"],
        default="both",
    )
    parser.add_argument("--message-window-size", type=int, default=1000)
    parser.add_argument("--room-info-delay-ms", type=float, default=None)
    parser.add_argument("--quote-fetch-delay-ms", type=float, default=None)
    parser.add_argument("--media-delay-ms", type=float, default=None)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenario",
        choices=["text", "quote", "thread", "image", "quote_image", "media_mix"],
    )
    args = apply_profile_defaults(parser.parse_args())
    args.repeat = max(1, int(args.repeat))
    args.concurrency = max(2, int(args.concurrency))
    return args


async def async_main(args: argparse.Namespace) -> int:
    if not args.control_root.resolve().is_dir():
        raise FileNotFoundError(f"Control checkout not found: {args.control_root}")
    control_branch = load_branch("control", args.control_root.resolve())
    rebuild_branch = load_branch("rebuild", args.rebuild_root.resolve())
    scenario_map = make_scenarios()
    scenarios = [scenario_map[name] for name in args.scenario]

    control_runs: list[dict[str, ScenarioStats]] = []
    rebuild_runs: list[dict[str, ScenarioStats]] = []
    for run_number in range(1, args.repeat + 1):
        control_runs.append(await run_branch(control_branch, scenarios, args, run_number=run_number))
        rebuild_runs.append(await run_branch(rebuild_branch, scenarios, args, run_number=run_number))
    control_median = _median_stats(control_runs)
    rebuild_median = _median_stats(rebuild_runs)
    print_summary(control_median, rebuild_median, args)

    if args.json_output:
        payload = {
            "schema_version": 1,
            "generated_at": time.time(),
            "settings": {
                "profile": args.profile,
                "iterations": args.iterations,
                "warmup": args.warmup,
                "repeat": args.repeat,
                "concurrency": args.concurrency,
                "cache_mode": args.cache_mode,
                "execution_mode": args.execution_mode,
                "control_root": str(args.control_root.resolve()),
                "candidate_root": str(args.rebuild_root.resolve()),
            },
            "runs": {
                "control": [{key: asdict(value) for key, value in run.items()} for run in control_runs],
                "candidate": [{key: asdict(value) for key, value in run.items()} for run in rebuild_runs],
            },
            "median": {
                "control": {key: asdict(value) for key, value in control_median.items()},
                "candidate": {key: asdict(value) for key, value in rebuild_median.items()},
            },
        }
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())

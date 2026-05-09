from __future__ import annotations

import argparse
import asyncio
import copy
import importlib.util
import inspect
import math
import statistics
import sys
import tempfile
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


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
    minimum_ms: float
    maximum_ms: float


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
    }


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


def summarize_timings(timings_ms: list[float]) -> ScenarioStats:
    if not timings_ms:
        raise ValueError("timings_ms cannot be empty")
    ordered = sorted(timings_ms)
    p95_index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1))
    return ScenarioStats(
        mean_ms=statistics.fmean(ordered),
        median_ms=statistics.median(ordered),
        p95_ms=ordered[p95_index],
        minimum_ms=ordered[0],
        maximum_ms=ordered[-1],
    )


async def benchmark_scenario(
    branch: LoadedBranch,
    scenario: Scenario,
    args: argparse.Namespace,
) -> ScenarioStats:
    with tempfile.TemporaryDirectory(prefix=f"rocketcat_bench_{branch.name}_{scenario.name}_") as temp_dir:
        translator, cleanup = create_translator(
            branch,
            Path(temp_dir),
            args,
            scenario.quoted_messages,
        )
        timings_ms: list[float] = []
        try:
            total_iterations = max(0, int(args.warmup)) + max(1, int(args.iterations))
            for index in range(total_iterations):
                raw_msg = copy.deepcopy(scenario.raw_msg)
                started_at = time.perf_counter()
                event = await translator.translate(raw_msg)
                elapsed_ms = (time.perf_counter() - started_at) * 1000.0
                if event is None:
                    raise RuntimeError(f"{branch.name}:{scenario.name} returned no event")
                if index >= int(args.warmup):
                    timings_ms.append(elapsed_ms)
        finally:
            maybe_awaitable = cleanup()
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        return summarize_timings(timings_ms)


async def run_branch(
    branch: LoadedBranch,
    scenarios: list[Scenario],
    args: argparse.Namespace,
) -> dict[str, ScenarioStats]:
    results: dict[str, ScenarioStats] = {}
    for scenario in scenarios:
        results[scenario.name] = await benchmark_scenario(branch, scenario, args)
    return results


def format_ratio(control_ms: float, rebuild_ms: float) -> str:
    if control_ms <= 0:
        return "n/a"
    delta_percent = ((control_ms - rebuild_ms) / control_ms) * 100.0
    sign = "+" if delta_percent >= 0 else ""
    return f"{sign}{delta_percent:.1f}%"


def print_summary(
    scenarios: list[Scenario],
    control_results: dict[str, ScenarioStats],
    rebuild_results: dict[str, ScenarioStats],
    args: argparse.Namespace,
) -> None:
    print("settings:")
    print(
        "  iterations={iterations} warmup={warmup} message_window_size={window} room_info_delay_ms={room_delay:.3f} quote_fetch_delay_ms={quote_delay:.3f} media_delay_ms={media_delay:.3f}".format(
            iterations=args.iterations,
            warmup=args.warmup,
            window=args.message_window_size,
            room_delay=args.room_info_delay_ms,
            quote_delay=args.quote_fetch_delay_ms,
            media_delay=args.media_delay_ms,
        )
    )
    print()
    print("scenario           control_mean_ms   rebuild_mean_ms   delta_vs_control")
    print("-----------------  ----------------  ----------------  ----------------")
    for scenario in scenarios:
        control_stats = control_results[scenario.name]
        rebuild_stats = rebuild_results[scenario.name]
        print(
            f"{scenario.name:<17}  {control_stats.mean_ms:>16.3f}  {rebuild_stats.mean_ms:>16.3f}  {format_ratio(control_stats.mean_ms, rebuild_stats.mean_ms):>16}"
        )
        print(
            f"{'':<17}  control p95={control_stats.p95_ms:.3f} ms  rebuild p95={rebuild_stats.p95_ms:.3f} ms"
        )


def parse_args() -> argparse.Namespace:
    default_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Compare inbound translation latency between control and rebuild branches.")
    parser.add_argument(
        "--control-root",
        type=Path,
        default=default_root.parent / "rocketcat_shell",
        help="Path to the control branch root",
    )
    parser.add_argument(
        "--rebuild-root",
        type=Path,
        default=default_root,
        help="Path to the rebuild branch root",
    )
    parser.add_argument("--iterations", type=int, default=200, help="Measured iterations per scenario")
    parser.add_argument("--warmup", type=int, default=20, help="Warmup iterations per scenario")
    parser.add_argument("--message-window-size", type=int, default=1000, help="Message window size for translator dependencies")
    parser.add_argument("--room-info-delay-ms", type=float, default=0.0, help="Artificial delay for get_room_info")
    parser.add_argument("--quote-fetch-delay-ms", type=float, default=0.0, help="Artificial delay for fetch_message_by_id")
    parser.add_argument("--media-delay-ms", type=float, default=0.0, help="Artificial delay for media materialization")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=["text", "quote", "thread", "image"],
        help="Restrict benchmark to one or more named scenarios",
    )
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    control_branch = load_branch("control", args.control_root)
    rebuild_branch = load_branch("rebuild", args.rebuild_root)

    scenario_map = make_scenarios()
    scenario_names = args.scenario or ["text", "quote", "thread", "image"]
    scenarios = [scenario_map[name] for name in scenario_names]

    control_results = await run_branch(control_branch, scenarios, args)
    rebuild_results = await run_branch(rebuild_branch, scenarios, args)
    print_summary(scenarios, control_results, rebuild_results, args)
    return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
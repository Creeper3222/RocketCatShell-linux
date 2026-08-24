from __future__ import annotations

import argparse
import copy
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rocketcat_shell.bridge.json_codec import json_dumps, json_dumps_compact
from rocketcat_shell.bridge.rocketchat_client import RocketChatClient
from rocketcat_shell.bridge.transports.codec import OneBotMessageCodec
from rocketcat_shell.bridge.transports import normalize_transport
from rocketcat_shell.models import BotRecord, ShellSettings
from rocketcat_shell.performance import RuntimeMetrics
from rocketcat_shell.shell.manager import ShellManager


def _payload() -> dict[str, Any]:
    return {
        "time": 1787510000,
        "self_id": 41818411195,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 3000000123,
        "group_id": "stress-room-8",
        "user_id": "stress-user-119",
        "raw_message": "RocketCat 性能测试 " * 16,
        "message": [
            {"type": "text", "data": {"text": "RocketCat 性能测试 " * 16}},
            {"type": "image", "data": {"file": "https://example.invalid/media/demo.png"}},
        ],
        "sender": {
            "user_id": "stress-user-119",
            "nickname": "stress-user",
            "card": "stress-card",
            "role": "member",
        },
    }


def _measure(operation: Callable[[], None], iterations: int, repeats: int) -> dict[str, Any]:
    rates: list[float] = []
    for _ in range(repeats):
        started = time.perf_counter()
        for _index in range(iterations):
            operation()
        elapsed = time.perf_counter() - started
        rates.append(iterations / elapsed)
    return {
        "iterations": iterations,
        "repeats": repeats,
        "runs_per_second": [round(rate, 3) for rate in rates],
        "median_per_second": round(statistics.median(rates), 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="RocketCatShell v0.2.3 hot-path benchmark")
    parser.add_argument("--iterations", type=int, default=20_000)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    payload = _payload()
    inbound_payload = {
        "_id": "message-3000000123",
        "rid": "stress-room-8",
        "msg": "RocketCat 性能测试 " * 64,
        "tmid": "thread-42",
        "groupable": True,
        "editedAt": "2026-08-24T03:00:00.000Z",
        "attachments": [
            {
                "title": "benchmark.png",
                "image_url": "https://example.invalid/media/benchmark.png",
            }
        ],
    }
    codec = OneBotMessageCodec("array")
    metrics = RuntimeMetrics()
    room_id = "stress-room-8"
    shell_defaults = ShellSettings()
    bot = BotRecord.from_mapping(
        {
            "id": "benchmark-bot",
            "name": "Benchmark Bot",
            "enabled": False,
            "server_url": "http://localhost:3000",
            "username": "benchmark",
            "password": "secret",
            "e2ee_password": "e2ee-secret",
        },
        defaults=shell_defaults,
    )
    bot.apply_onebot_transport(
        normalize_transport(
            {
                "type": "http-client",
                "settings": {
                    "url": "http://localhost:8080",
                    "access_token": "onebot-secret",
                },
            },
            shell_defaults=shell_defaults,
        )
    )
    manager = object.__new__(ShellManager)
    manager.layout = SimpleNamespace(bots_dir=Path("data/bots"))
    manager.runtimes = {}
    manager._read_persisted_self_id = lambda _bot_id: 0
    rocket_client = object.__new__(RocketChatClient)

    def legacy_four_peer_fanout() -> None:
        for _peer in range(4):
            encoded = copy.deepcopy(payload)
            json_dumps(encoded)

    def shared_four_peer_fanout() -> None:
        frame = codec.serialize_event(payload)
        for _peer in range(4):
            _ = frame.text

    def metric_triplet() -> None:
        metrics.increment("processed")
        metrics.set_gauge("depth", 4, high_water=True)
        metrics.observe("wait_ms", 0.125)

    def legacy_room_bucket() -> None:
        digest = hashlib.blake2b(room_id.encode("utf-8"), digest_size=2).digest()
        _ = int.from_bytes(digest, "big") % 4

    def room_bucket() -> None:
        _ = hash(room_id) % 4

    def full_bot_list_item() -> None:
        json_dumps(manager._serialize_bot(bot, mask_secrets=False))

    def compact_bot_list_item() -> None:
        json_dumps(manager._serialize_bot_compact(bot, mask_secrets=False))

    def legacy_inbound_signature() -> str:
        parts: list[str] = []
        for key in RocketChatClient._INBOUND_SIGNATURE_FIELD_NAMES:
            value = inbound_payload.get(key)
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                serialized = json_dumps_compact(value, sort_keys=True, default=str)
                value = hashlib.blake2b(
                    serialized.encode("utf-8"),
                    digest_size=16,
                ).hexdigest()
            parts.append(f"{key}={value}")
        for key in ("attachments", "files", "urls", "mentions"):
            value = inbound_payload.get(key)
            if value:
                serialized = json_dumps_compact(value, sort_keys=True, default=str)
                hashed = hashlib.blake2b(
                    serialized.encode("utf-8"),
                    digest_size=16,
                ).hexdigest()
                parts.append(f"{key}={hashed}")
        return "\x1f".join(parts)

    def compact_inbound_signature() -> str:
        return rocket_client._build_inbound_message_signature(inbound_payload)

    full_bot_payload = manager._serialize_bot(bot, mask_secrets=False)
    compact_bot_payload = manager._serialize_bot_compact(bot, mask_secrets=False)

    result = {
        "schema_version": 1,
        "payload_json_bytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        "four_peer_legacy_deepcopy_and_serialize": _measure(
            legacy_four_peer_fanout, args.iterations, args.repeats
        ),
        "four_peer_shared_serialized_frame": _measure(
            shared_four_peer_fanout, args.iterations, args.repeats
        ),
        "runtime_metrics_triplet": _measure(metric_triplet, args.iterations * 10, args.repeats),
        "room_bucket_legacy_blake2b": _measure(
            legacy_room_bucket, args.iterations * 10, args.repeats
        ),
        "room_bucket_process_hash": _measure(room_bucket, args.iterations * 10, args.repeats),
        "bot_list_full_item": _measure(full_bot_list_item, args.iterations, args.repeats),
        "bot_list_compact_item": _measure(compact_bot_list_item, args.iterations, args.repeats),
        "inbound_signature_legacy_string": _measure(
            legacy_inbound_signature,
            args.iterations,
            args.repeats,
        ),
        "inbound_signature_compact_long_text": _measure(
            compact_inbound_signature,
            args.iterations,
            args.repeats,
        ),
    }
    old_rate = result["four_peer_legacy_deepcopy_and_serialize"]["median_per_second"]
    new_rate = result["four_peer_shared_serialized_frame"]["median_per_second"]
    old_route = result["room_bucket_legacy_blake2b"]["median_per_second"]
    new_route = result["room_bucket_process_hash"]["median_per_second"]
    full_bot_rate = result["bot_list_full_item"]["median_per_second"]
    compact_bot_rate = result["bot_list_compact_item"]["median_per_second"]
    full_bot_bytes = len(json_dumps(full_bot_payload).encode("utf-8"))
    compact_bot_bytes = len(json_dumps(compact_bot_payload).encode("utf-8"))
    legacy_signature_rate = result["inbound_signature_legacy_string"]["median_per_second"]
    compact_signature_rate = result["inbound_signature_compact_long_text"]["median_per_second"]
    legacy_signature_bytes = sys.getsizeof(legacy_inbound_signature())
    compact_signature_bytes = sys.getsizeof(compact_inbound_signature())
    result["ratios"] = {
        "four_peer_fanout_speedup": round(new_rate / old_rate, 3),
        "room_bucket_speedup": round(new_route / old_route, 3),
        "compact_bot_serialization_speedup": round(compact_bot_rate / full_bot_rate, 3),
        "compact_bot_size_reduction_percent": round(
            (1.0 - compact_bot_bytes / full_bot_bytes) * 100.0,
            3,
        ),
        "inbound_signature_throughput_ratio": round(
            compact_signature_rate / legacy_signature_rate,
            3,
        ),
        "inbound_signature_value_size_reduction_percent": round(
            (1.0 - compact_signature_bytes / legacy_signature_bytes) * 100.0,
            3,
        ),
    }
    result["bot_list_payload_bytes"] = {
        "full": full_bot_bytes,
        "compact": compact_bot_bytes,
    }
    result["inbound_signature_value_bytes"] = {
        "legacy": legacy_signature_bytes,
        "compact_long_text": compact_signature_bytes,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

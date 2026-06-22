from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import psutil

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rocketcat_shell.bridge.hot_storage import RuntimeStateEngine
from rocketcat_shell.bridge.media import RocketChatMediaBridge
from rocketcat_shell.bridge.media_publication import MediaPublicationService
from rocketcat_shell.bridge.user_identity import UserIdentityRegistry
from rocketcat_shell.shell.webui import BridgeLogBuffer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RocketCatShell v0.2.0 bounded-resource soak")
    parser.add_argument("--duration-seconds", type=int, default=1800)
    parser.add_argument("--output", type=Path, required=True)
    return parser


async def run_soak(duration_seconds: int) -> dict[str, object]:
    process = psutil.Process(os.getpid())
    started_at = time.time()
    deadline = time.monotonic() + max(1, duration_seconds)
    start_rss = process.memory_info().rss
    max_rss = start_rss
    max_threads = process.num_threads()
    max_handles = process.num_handles() if hasattr(process, "num_handles") else 0
    iterations = 0

    with tempfile.TemporaryDirectory(prefix="rocketcat-v020-soak-") as temp_dir:
        root = Path(temp_dir)
        state = RuntimeStateEngine(message_window_size=5000)
        identity = UserIdentityRegistry(
            root / "identity.sqlite3",
            scope_key="soak",
            bot_id="soak-bot",
            cache_max_entries=4096,
        )
        publication = MediaPublicationService(max_entries=512)
        publication.configure_webui(port=5751)
        client = SimpleNamespace(
            config=SimpleNamespace(
                bot_id="soak-bot",
                onebot_ws_url="ws://127.0.0.1:6199/ws/",
                remote_media_max_size=4 * 1024 * 1024,
            )
        )
        media = RocketChatMediaBridge(
            client,
            temp_dir=root / "temp",
            media_publication_service=publication,
            cache_max_bytes=2 * 1024 * 1024,
            cache_max_age_hours=1,
        )
        logs = BridgeLogBuffer(max_entries=2000, max_bytes=4 * 1024 * 1024)
        await media.start()
        try:
            while time.monotonic() < deadline:
                batch_start = iterations
                identity_rows = []
                for offset in range(128):
                    index = batch_start + offset
                    state.allocate_mapping("message", f"message-{index}")
                    identity_rows.append(
                        {
                            "user_id": f"user-{index % 6000}",
                            "username": f"user{index % 6000}",
                        }
                    )
                    record = logging.LogRecord(
                        "soak",
                        logging.INFO,
                        __file__,
                        1,
                        f"[RocketCatShell] soak message {index} " + ("x" * 512),
                        (),
                        None,
                    )
                    logs.append_record(record)
                await identity.ensure_mappings(identity_rows, bot_id="soak-bot")
                for row in identity_rows[:32]:
                    await identity.ensure_mapping(
                        row["user_id"],
                        username=row["username"],
                        bot_id="soak-bot",
                    )

                media_path = root / "temp" / f"e2ee_{(iterations // 128) % 64:02d}.png"
                media_path.parent.mkdir(parents=True, exist_ok=True)
                if not media_path.exists():
                    media_path.write_bytes(b"\x89PNG\r\n\x1a\n" + os.urandom(16 * 1024))
                media.publish_local_media_file(str(media_path))

                iterations += 128
                if iterations % 2048 == 0:
                    await asyncio.to_thread(media._cleanup_media_cache)
                    memory = process.memory_info()
                    max_rss = max(max_rss, memory.rss)
                    max_threads = max(max_threads, process.num_threads())
                    if hasattr(process, "num_handles"):
                        max_handles = max(max_handles, process.num_handles())
                await asyncio.sleep(0.05)
        finally:
            await media.stop()

        final_rss = process.memory_info().rss
        max_rss = max(max_rss, final_rss)
        message_snapshot = state.rebuild_message_window()
        result = {
            "duration_seconds": round(time.time() - started_at, 3),
            "iterations": iterations,
            "start_rss_bytes": start_rss,
            "final_rss_bytes": final_rss,
            "max_rss_bytes": max_rss,
            "rss_growth_bytes": final_rss - start_rss,
            "max_threads": max_threads,
            "max_handles": max_handles,
            "message_active_count": message_snapshot.active_count,
            "identity_cache": identity.cache_summary(),
            "media_cache": media.cache_summary(),
            "log_buffer": logs.summary(),
        }
        result["bounded"] = bool(
            message_snapshot.active_count <= 5000
            and identity.cache_summary()["by_user_entries"] <= 4096
            and identity.cache_summary()["by_onebot_entries"] <= 4096
            and identity.cache_summary()["bot_user_seen_entries"] <= 4096
            and media.cache_summary()["total_bytes"] <= 2 * 1024 * 1024
            and logs.summary()["bytes"] <= 4 * 1024 * 1024
        )
        return result


async def async_main() -> int:
    args = build_parser().parse_args()
    result = await run_soak(args.duration_seconds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if result.get("bounded") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))

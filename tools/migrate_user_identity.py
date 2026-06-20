from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import ssl
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rocketcat_shell.bridge.hot_storage import (  # noqa: E402
    JournalPersistenceWorker,
    build_runtime_hot_stores,
)
from rocketcat_shell.bridge.user_identity import (  # noqa: E402
    UserIdentityRegistry,
)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def find_bot(config_path: Path, bot_id: str) -> dict[str, Any]:
    payload = read_json(config_path, {"bots": []})
    for item in payload.get("bots", []):
        if isinstance(item, dict) and str(item.get("id") or item.get("bot_id")) == bot_id:
            return item
    raise RuntimeError(f"找不到 bot 配置: {bot_id}")


def request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    body = None
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers=request_headers,
        method=method,
    )
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def discover_server_and_bot_identity(bot: dict[str, Any]) -> dict[str, Any]:
    server_url = str(bot.get("server_url") or "").rstrip("/")
    info: dict[str, Any] = {}
    try:
        info = request_json("GET", f"{server_url}/api/info")
    except Exception as exc:
        print(f"[WARN] /api/info 读取失败，将使用规范化 URL 作为服务器范围: {exc!r}")

    login = request_json(
        "POST",
        f"{server_url}/api/v1/login",
        payload={
            "user": str(bot.get("username") or ""),
            "password": str(bot.get("password") or ""),
        },
    )
    if login.get("status") != "success":
        raise RuntimeError(f"Rocket.Chat 登录失败: {login}")
    login_data = login["data"]
    profile = login_data.get("me") if isinstance(login_data.get("me"), dict) else {}
    return {
        "server_url": server_url,
        "cloud_workspace_id": str(info.get("cloudWorkspaceId") or ""),
        "bot_user_id": str(login_data.get("userId") or ""),
        "bot_username": str(profile.get("username") or bot.get("username") or ""),
        "bot_nickname": str(
            profile.get("name")
            or profile.get("nickname")
            or profile.get("username")
            or bot.get("username")
            or ""
        ),
    }


def build_offline_identity(
    bot: dict[str, Any],
    *,
    bot_user_id: str,
    bot_username: str,
    bot_nickname: str,
    cloud_workspace_id: str,
) -> dict[str, Any]:
    normalized_user_id = str(bot_user_id or "").strip()
    if not normalized_user_id:
        raise RuntimeError("离线迁移必须提供 --bot-user-id")
    username = str(bot_username or bot.get("username") or "").strip()
    return {
        "server_url": str(bot.get("server_url") or "").rstrip("/"),
        "cloud_workspace_id": str(cloud_workspace_id or "").strip(),
        "bot_user_id": normalized_user_id,
        "bot_username": username,
        "bot_nickname": str(bot_nickname or username).strip(),
    }


def extract_legacy_state(bot_data_dir: Path) -> tuple[dict[str, int], dict[str, dict[str, str]]]:
    snapshot_path = bot_data_dir / "runtime.snapshot.bin"
    journal_path = bot_data_dir / "runtime.journal.bin"
    payload = JournalPersistenceWorker.load_snapshot_payload(snapshot_path) or {}
    state = payload.get("state") if isinstance(payload.get("state"), dict) else payload
    state = state if isinstance(state, dict) else {}

    mappings = {
        str(user_id): int(onebot_id)
        for user_id, onebot_id in (
            ((state.get("forward") or {}).get("user", {}) or {}).items()
        )
    }
    profiles: dict[str, dict[str, str]] = {}
    for entry in (state.get("messages_by_source") or {}).values():
        if not isinstance(entry, dict):
            continue
        user_id = str(entry.get("sender_source_id") or "").strip()
        if not user_id:
            continue
        sender_name = str(entry.get("sender_name") or "").strip()
        profiles.setdefault(
            user_id,
            {
                "username": "",
                "nickname": sender_name,
            },
        )

    for record in JournalPersistenceWorker.iter_records(journal_path) or []:
        records = (
            record.get("mutations") or []
            if str(record.get("op") or "") == "batch"
            else [record]
        )
        for item in records:
            if not isinstance(item, dict):
                continue
            if str(item.get("op") or "") == "id_put" and str(item.get("namespace") or "") == "user":
                user_id = str(item.get("source_id") or "").strip()
                if user_id:
                    mappings[user_id] = int(item.get("surrogate_id") or 0)
            if str(item.get("op") or "") == "message_put":
                entry = item.get("entry")
                if not isinstance(entry, dict):
                    continue
                user_id = str(entry.get("sender_source_id") or "").strip()
                if user_id:
                    profiles[user_id] = {
                        "username": "",
                        "nickname": str(entry.get("sender_name") or "").strip(),
                    }
    return mappings, profiles


def backup_files(bot_data_dir: Path, config_dir: Path) -> Path:
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = bot_data_dir / f"user_identity_migration_backup_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    for path in (
        bot_data_dir / "runtime.snapshot.bin",
        bot_data_dir / "runtime.journal.bin",
        bot_data_dir / "runtime_state.json",
        bot_data_dir / "re_waring.json",
        bot_data_dir / "identity_scope.json",
        config_dir / "bots.json",
        config_dir / "shell.json",
    ):
        if path.exists():
            destination = backup_dir / path.name
            if destination.exists():
                destination = backup_dir / f"{path.parent.name}_{path.name}"
            shutil.copy2(path, destination)
    return backup_dir


def remove_legacy_config_fields(config_dir: Path) -> None:
    bots_path = config_dir / "bots.json"
    bots_payload = read_json(bots_path, {"bots": []})
    for item in bots_payload.get("bots", []):
        if isinstance(item, dict):
            item.pop("onebot_self_id", None)
    write_json(bots_path, bots_payload)

    shell_path = config_dir / "shell.json"
    shell_payload = read_json(shell_path, {})
    if isinstance(shell_payload, dict):
        shell_payload.pop("next_onebot_self_id", None)
        write_json(shell_path, shell_payload)


def update_astrbot_admin_ids(
    config_path: Path,
    legacy_to_new: dict[str, int],
) -> dict[str, Any]:
    payload = read_json(config_path, {})
    if not isinstance(payload, dict):
        raise RuntimeError("AstrBot 配置格式无效")
    admins = payload.get("admins_id")
    if not isinstance(admins, list):
        raise RuntimeError("AstrBot 配置缺少 admins_id 数组")
    updated: list[str] = []
    replacements: dict[str, str] = {}
    for item in admins:
        old_value = str(item)
        new_value = str(legacy_to_new.get(old_value, old_value))
        updated.append(new_value)
        if new_value != old_value:
            replacements[old_value] = new_value
    payload["admins_id"] = updated
    write_json(config_path, payload)
    return {
        "replacements": replacements,
        "admins_id": updated,
    }


async def migrate(args: argparse.Namespace) -> None:
    project_root = Path(args.project_root).resolve()
    config_dir = project_root / "config"
    data_root = project_root / "data"
    bot_data_dir = data_root / "bots" / args.bot_id
    if not bot_data_dir.exists():
        raise RuntimeError(f"bot 数据目录不存在: {bot_data_dir}")

    bot = find_bot(config_dir / "bots.json", args.bot_id)
    if args.astrbot_config:
        astrbot_payload = read_json(Path(args.astrbot_config).resolve(), {})
        if not isinstance(astrbot_payload, dict) or not isinstance(
            astrbot_payload.get("admins_id"),
            list,
        ):
            raise RuntimeError("AstrBot 配置缺少有效的 admins_id 数组")
    if args.bot_user_id:
        identity = build_offline_identity(
            bot,
            bot_user_id=args.bot_user_id,
            bot_username=args.bot_username,
            bot_nickname=args.bot_nickname,
            cloud_workspace_id=args.cloud_workspace_id,
        )
    else:
        identity = discover_server_and_bot_identity(bot)
    legacy_mappings, profiles = extract_legacy_state(bot_data_dir)
    print(f"[INFO] 检测到旧 user 映射 {len(legacy_mappings)} 条")

    backup_dir = backup_files(bot_data_dir, config_dir)
    print(f"[INFO] 备份完成: {backup_dir}")

    registry = UserIdentityRegistry.for_server(
        data_root,
        server_url=identity["server_url"],
        cloud_workspace_id=identity["cloud_workspace_id"],
        bot_id=args.bot_id,
        warning_path=bot_data_dir / "re_waring.json",
    )

    ordered_users = sorted(legacy_mappings, key=lambda item: legacy_mappings[item])
    legacy_to_new: dict[str, int] = {}
    for user_id in ordered_users:
        profile = profiles.get(user_id, {})
        mapping = await registry.ensure_mapping(
            user_id,
            username=str(profile.get("username") or ""),
            nickname=str(profile.get("nickname") or ""),
            is_bot=user_id == identity["bot_user_id"],
            bot_id=args.bot_id,
        )
        legacy_to_new[str(legacy_mappings[user_id])] = mapping.onebot_id

    self_mapping = await registry.ensure_mapping(
        identity["bot_user_id"],
        username=identity["bot_username"],
        nickname=identity["bot_nickname"],
        is_bot=True,
        bot_id=args.bot_id,
    )

    stores = build_runtime_hot_stores(bot_data_dir)
    try:
        stores.state_engine.purge_legacy_user_dependent_state()
        private_bindings = stores.state_engine.get_private_room_source_bindings()
        rebuilt: dict[int, str] = {}
        for user_id, room_id in private_bindings.items():
            mapping = await registry.ensure_mapping(user_id, bot_id=args.bot_id)
            rebuilt[mapping.onebot_id] = room_id
        stores.state_engine.replace_private_room_surrogate_bindings(rebuilt)
    finally:
        stores.close()

    write_json(
        bot_data_dir / "identity_scope.json",
        {
            "scope_key": registry.scope_key,
            "database_path": str(registry.database_path),
            "onebot_self_id": self_mapping.onebot_id,
        },
    )
    migration_manifest = {
        "version": 1,
        "algorithm": "sha256-linear-v1",
        "bot_id": args.bot_id,
        "created_at": time.time(),
        "legacy_to_new": legacy_to_new,
        "bot_user_id": identity["bot_user_id"],
        "onebot_self_id": self_mapping.onebot_id,
        "database_path": str(registry.database_path),
    }
    write_json(
        bot_data_dir / "user_identity_migration.json",
        migration_manifest,
    )
    remove_legacy_config_fields(config_dir)

    if args.inject_synthetic:
        synthetic = await registry.inject_synthetic_collision(
            anchor_user_id=args.anchor_user_id,
            synthetic_user_id=(
                f"synthetic:collision:{args.bot_id}:{args.anchor_user_id}"
            ),
            username="rocketcat_collision_fixture",
            nickname="哈萨维冲突测试（请保留）",
            bot_id=args.bot_id,
        )
        print(
            "[INFO] synthetic 冲突映射已保留: "
            f"userId={synthetic.user_id} onebot_id={synthetic.onebot_id} "
            f"offset={synthetic.probe_offset}"
        )

    await registry.sync_warning_file(bot_id=args.bot_id)
    if args.astrbot_config:
        astrbot_result = update_astrbot_admin_ids(
            Path(args.astrbot_config).resolve(),
            legacy_to_new,
        )
        print(
            "[INFO] AstrBot 管理员 ID 已更新: "
            f"{astrbot_result['replacements']}"
        )
    print(
        "[OK] 迁移完成: "
        f"bot_user_id={identity['bot_user_id']} "
        f"onebot_self_id={self_mapping.onebot_id} "
        f"database={registry.database_path}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="迁移 RocketCatShell 旧 user 映射到 sha256-linear-v1",
    )
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--bot-id", required=True)
    parser.add_argument("--inject-synthetic", action="store_true")
    parser.add_argument("--bot-user-id", default="")
    parser.add_argument("--bot-username", default="")
    parser.add_argument("--bot-nickname", default="")
    parser.add_argument("--cloud-workspace-id", default="")
    parser.add_argument("--astrbot-config", default="")
    parser.add_argument(
        "--anchor-user-id",
        default="6TZ4YPRbmhYwgFZuM",
    )
    return parser


if __name__ == "__main__":
    asyncio.run(migrate(build_parser().parse_args()))

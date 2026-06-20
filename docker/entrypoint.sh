#!/usr/bin/env sh
set -eu

APP_DIR="/app"
CONFIG_DIR="$APP_DIR/config"
PLUGINS_DIR="$APP_DIR/data/plugins"
BUILTIN_PLUGINS_DIR="/opt/rocketcat/builtin_plugins"
EXAMPLES_DIR="/opt/rocketcat/examples"

mkdir -p \
    "$CONFIG_DIR/plugins_config" \
    "$APP_DIR/data/bots" \
    "$APP_DIR/data/user_identity" \
    "$PLUGINS_DIR" \
    "$APP_DIR/data/plugin_data" \
    "$APP_DIR/logs"

if [ ! -f "$CONFIG_DIR/shell.json" ]; then
    python - <<'PY'
import json
import os
from pathlib import Path

config_path = Path("/app/config/shell.json")
payload = {
    "webui_host": os.environ.get("ROCKETCAT_WEBUI_HOST", "0.0.0.0"),
    "webui_port": int(os.environ.get("ROCKETCAT_WEBUI_PORT", "5751")),
    "webui_access_password": os.environ.get("ROCKETCAT_WEBUI_PASSWORD", "123456"),
    "message_index_max_entries": int(os.environ.get("ROCKETCAT_MESSAGE_INDEX_MAX_ENTRIES", "1000")),
    "enable_base64_media_transport": os.environ.get("ROCKETCAT_DEFAULT_ENABLE_BASE64_MEDIA_TRANSPORT", "false").strip().lower() in {"1", "true", "yes", "on"},
    "log_level": os.environ.get("ROCKETCAT_LOG_LEVEL", "INFO"),
    "auto_open_browser": os.environ.get("ROCKETCAT_AUTO_OPEN_BROWSER", "false").strip().lower() in {"1", "true", "yes", "on"},
    "default_onebot_ws_url": os.environ.get("ROCKETCAT_DEFAULT_ONEBOT_WS_URL", "ws://host.docker.internal:6200/ws/"),
    "default_onebot_access_token": os.environ.get("ROCKETCAT_DEFAULT_ONEBOT_ACCESS_TOKEN", ""),
    "default_reconnect_delay": float(os.environ.get("ROCKETCAT_DEFAULT_RECONNECT_DELAY", "5.0")),
    "default_max_reconnect_attempts": int(os.environ.get("ROCKETCAT_DEFAULT_MAX_RECONNECT_ATTEMPTS", "10")),
    "default_enable_subchannel_session_isolation": os.environ.get("ROCKETCAT_DEFAULT_ENABLE_SUBCHANNEL_SESSION_ISOLATION", "true").strip().lower() in {"1", "true", "yes", "on"},
    "default_remote_media_max_size": int(os.environ.get("ROCKETCAT_DEFAULT_REMOTE_MEDIA_MAX_SIZE", "20971520")),
    "default_skip_own_messages": os.environ.get("ROCKETCAT_DEFAULT_SKIP_OWN_MESSAGES", "true").strip().lower() in {"1", "true", "yes", "on"},
    "default_debug": os.environ.get("ROCKETCAT_DEFAULT_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"},
}
config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
fi

python - <<'PY'
import hashlib
import shutil
from pathlib import Path

builtin_root = Path("/opt/rocketcat/builtin_plugins")
plugins_root = Path("/app/data/plugins")


def digest_dir(path: Path) -> str:
    if not path.exists() or not path.is_dir():
        return ""

    digest = hashlib.sha256()
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(file_path.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


for plugin_dir in builtin_root.iterdir():
    if not plugin_dir.is_dir():
        continue

    target_dir = plugins_root / plugin_dir.name
    if not target_dir.exists():
        shutil.copytree(plugin_dir, target_dir)
        print(f"[entrypoint] seeded builtin plugin: {plugin_dir.name}")
        continue

    if digest_dir(plugin_dir) == digest_dir(target_dir):
        continue

    if target_dir.is_dir():
        shutil.rmtree(target_dir)
    else:
        target_dir.unlink()
    shutil.copytree(plugin_dir, target_dir)
    print(f"[entrypoint] refreshed builtin plugin: {plugin_dir.name}")
PY

if [ "$#" -eq 0 ]; then
    set -- python -m rocketcat_shell --no-browser
fi

exec "$@"

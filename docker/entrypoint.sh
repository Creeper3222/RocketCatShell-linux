#!/usr/bin/env sh
set -eu

APP_DIR="/app"
CONFIG_DIR="$APP_DIR/config"
PLUGINS_DIR="$APP_DIR/data/plugins"
BUILTIN_PLUGINS_DIR="/opt/rocketcat/builtin_plugins"
EXAMPLES_DIR="/opt/rocketcat/examples"
FROZEN_UPDATE_HELPER="/opt/rocketcat/update_helper.py"
UPDATE_DIR="$APP_DIR/data/update"
IMAGE_VERSION="${ROCKETCAT_IMAGE_VERSION:-v0.2.2}"

mkdir -p \
    "$CONFIG_DIR/plugins_config" \
    "$APP_DIR/data/temp" \
    "$APP_DIR/data/bots" \
    "$APP_DIR/data/user_identity" \
    "$PLUGINS_DIR" \
    "$APP_DIR/data/plugin_data" \
    "$UPDATE_DIR" \
    "$APP_DIR/logs"

export ROCKETCAT_IMAGE_VERSION="$IMAGE_VERSION"
export ROCKETCAT_CONTAINER_RUNTIME_GENERATION="1"

# The helper is copied into the immutable image layer. It is deliberately run
# before plugin seeding so an interrupted replacement is restored before any
# image-owned files can be refreshed.
if ! python "$FROZEN_UPDATE_HELPER" recover "$APP_DIR"; then
    echo "[entrypoint] update transaction recovery failed; refusing to start" >&2
    exit 70
fi

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
    "performance_profile": os.environ.get("ROCKETCAT_PERFORMANCE_PROFILE", "balanced"),
    "inbound_worker_count": int(os.environ.get("ROCKETCAT_INBOUND_WORKER_COUNT", "0")),
    "onebot_outgoing_queue_max_entries": int(os.environ.get("ROCKETCAT_ONEBOT_OUTGOING_QUEUE_MAX_ENTRIES", "512")),
    "identity_cache_max_entries": int(os.environ.get("ROCKETCAT_IDENTITY_CACHE_MAX_ENTRIES", "4096")),
    "media_cache_max_bytes": int(os.environ.get("ROCKETCAT_MEDIA_CACHE_MAX_BYTES", "1073741824")),
    "media_cache_max_age_hours": int(os.environ.get("ROCKETCAT_MEDIA_CACHE_MAX_AGE_HOURS", "168")),
    "log_file_max_bytes": int(os.environ.get("ROCKETCAT_LOG_FILE_MAX_BYTES", "10485760")),
    "log_file_backup_count": int(os.environ.get("ROCKETCAT_LOG_FILE_BACKUP_COUNT", "3")),
    "terminal_max_sessions": int(os.environ.get("ROCKETCAT_TERMINAL_MAX_SESSIONS", "6")),
    "terminal_idle_timeout_seconds": int(os.environ.get("ROCKETCAT_TERMINAL_IDLE_TIMEOUT_SECONDS", "0")),
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
import json
import os
import shutil
from pathlib import Path

builtin_root = Path("/opt/rocketcat/builtin_plugins")
plugins_root = Path("/app/data/plugins")
runtime_path = Path("/app/data/update/runtime.json")
image_version = os.environ.get("ROCKETCAT_IMAGE_VERSION", "v0.2.2")

runtime_version = image_version
if runtime_path.is_file() and not runtime_path.is_symlink():
    try:
        payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if isinstance(payload, dict):
        runtime_version = str(payload.get("runtime_version") or image_version)

# A writable-layer update owns its built-in plugin copies until the container
# is recreated. Recreating the container removes runtime.json and re-enables
# the normal image seed, returning code and built-ins to the image version.
refresh_from_image = runtime_version == image_version


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

    if not refresh_from_image:
        print(
            f"[entrypoint] preserving writable-layer builtin plugin: {plugin_dir.name} "
            f"(runtime={runtime_version}, image={image_version})"
        )
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
    set -- python "$FROZEN_UPDATE_HELPER" run "$APP_DIR"
fi

exec "$@"

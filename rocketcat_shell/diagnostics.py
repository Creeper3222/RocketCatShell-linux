from __future__ import annotations

import os
import platform
import socket
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import winreg
except ImportError:
    winreg = None

try:
    import psutil
except ImportError:
    psutil = None


DEFAULT_CPU_SAMPLE_SECONDS = 0.2
DEFAULT_HOST_DIAGNOSTICS_CACHE_TTL_SECONDS = 3.0


_HOST_DIAGNOSTICS_CACHE_LOCK = threading.Lock()
_HOST_DIAGNOSTICS_CACHE_ENTRY: dict[str, Any] | None = None


def _build_host_diagnostics_cache_meta(
    *,
    cache_enabled: bool,
    cache_hit: bool,
    cache_ttl_seconds: float,
    captured_at: float | None,
) -> dict[str, Any]:
    snapshot_age_seconds: float | None = None
    if captured_at is not None and captured_at > 0:
        snapshot_age_seconds = max(0.0, time.time() - captured_at)

    return {
        "cache_enabled": bool(cache_enabled),
        "cache_hit": bool(cache_hit) if cache_enabled else False,
        "cache_status": "disabled" if not cache_enabled else ("hit" if cache_hit else "miss"),
        "cache_ttl_seconds": max(0.0, float(cache_ttl_seconds)),
        "captured_at": captured_at,
        "snapshot_age_seconds": snapshot_age_seconds,
    }


def collect_host_diagnostics(
    *,
    product_version: str,
    cpu_sample_seconds: float = DEFAULT_CPU_SAMPLE_SECONDS,
) -> dict[str, Any]:
    if psutil is None:
        raise RuntimeError("缺少依赖 psutil，请先安装 requirements.txt 后再使用诊断快照。")

    process = psutil.Process(os.getpid())
    process.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None)
    time.sleep(max(0.0, float(cpu_sample_seconds)))

    system_cpu_percent = psutil.cpu_percent(interval=None)
    process_cpu_percent = process.cpu_percent(interval=None)
    virtual_memory = psutil.virtual_memory()
    process_memory = process.memory_info().rss
    memory_usage_percent = float(getattr(virtual_memory, "percent", 0.0) or 0.0)
    process_memory_percent = 0.0
    if float(virtual_memory.total or 0.0) > 0:
        process_memory_percent = (float(process_memory) / float(virtual_memory.total)) * 100.0
    cpu_frequency = psutil.cpu_freq()
    current_frequency_mhz = 0.0
    if cpu_frequency is not None:
        current_frequency_mhz = float(
            getattr(cpu_frequency, "current", 0.0)
            or getattr(cpu_frequency, "max", 0.0)
            or 0.0
        )

    physical_cores = psutil.cpu_count(logical=False)
    logical_cores = psutil.cpu_count(logical=True) or os.cpu_count()

    return {
        "product_version": str(product_version or "").strip() or "-",
        "python_version": platform.python_version(),
        "hostname": socket.gethostname() or "-",
        "system_label": build_system_label(),
        "snapshot_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot_timestamp": time.time(),
        "cpu_model": resolve_cpu_model(),
        "cpu_cores": format_cpu_cores(physical_cores, logical_cores),
        "cpu_frequency_mhz": current_frequency_mhz,
        "cpu_frequency": format_frequency(current_frequency_mhz),
        "cpu_usage_percent": float(system_cpu_percent),
        "cpu_usage": format_percent(system_cpu_percent),
        "process_cpu_usage_percent": float(process_cpu_percent),
        "process_cpu_usage": format_percent(process_cpu_percent),
        "memory_total_bytes": int(virtual_memory.total),
        "memory_total": format_bytes_as_gb(virtual_memory.total),
        "memory_used_bytes": int(virtual_memory.used),
        "memory_used": format_bytes_as_gb(virtual_memory.used),
        "memory_available_bytes": int(virtual_memory.available),
        "memory_available": format_bytes_as_gb(virtual_memory.available),
        "memory_usage_percent": memory_usage_percent,
        "process_memory_bytes": int(process_memory),
        "process_memory_percent": process_memory_percent,
        "process_memory": format_bytes_as_mb(process_memory),
    }


def collect_cached_host_diagnostics(
    *,
    product_version: str,
    cpu_sample_seconds: float = DEFAULT_CPU_SAMPLE_SECONDS,
    cache_ttl_seconds: float = DEFAULT_HOST_DIAGNOSTICS_CACHE_TTL_SECONDS,
    force_refresh: bool = False,
) -> dict[str, Any]:
    snapshot, _ = collect_cached_host_diagnostics_with_meta(
        product_version=product_version,
        cpu_sample_seconds=cpu_sample_seconds,
        cache_ttl_seconds=cache_ttl_seconds,
        force_refresh=force_refresh,
    )
    return snapshot


def collect_cached_host_diagnostics_with_meta(
    *,
    product_version: str,
    cpu_sample_seconds: float = DEFAULT_CPU_SAMPLE_SECONDS,
    cache_ttl_seconds: float = DEFAULT_HOST_DIAGNOSTICS_CACHE_TTL_SECONDS,
    force_refresh: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    global _HOST_DIAGNOSTICS_CACHE_ENTRY
    normalized_ttl_seconds = max(0.0, float(cache_ttl_seconds))
    cache_enabled = normalized_ttl_seconds > 0
    cache_key = (
        str(product_version or "").strip(),
        float(cpu_sample_seconds),
    )

    if not force_refresh and cache_enabled:
        with _HOST_DIAGNOSTICS_CACHE_LOCK:
            cache_entry = _HOST_DIAGNOSTICS_CACHE_ENTRY
            if isinstance(cache_entry, dict) and cache_entry.get("key") == cache_key:
                captured_at = float(cache_entry.get("captured_at") or 0.0)
                cached_payload = cache_entry.get("payload")
                if isinstance(cached_payload, dict) and (time.time() - captured_at) <= normalized_ttl_seconds:
                    return dict(cached_payload), _build_host_diagnostics_cache_meta(
                        cache_enabled=True,
                        cache_hit=True,
                        cache_ttl_seconds=normalized_ttl_seconds,
                        captured_at=captured_at,
                    )

            snapshot = collect_host_diagnostics(
                product_version=product_version,
                cpu_sample_seconds=cpu_sample_seconds,
            )
            captured_at = float(snapshot.get("snapshot_timestamp") or time.time())
            _HOST_DIAGNOSTICS_CACHE_ENTRY = {
                "key": cache_key,
                "payload": dict(snapshot),
                "captured_at": captured_at,
            }
            return dict(snapshot), _build_host_diagnostics_cache_meta(
                cache_enabled=True,
                cache_hit=False,
                cache_ttl_seconds=normalized_ttl_seconds,
                captured_at=captured_at,
            )

    snapshot = collect_host_diagnostics(
        product_version=product_version,
        cpu_sample_seconds=cpu_sample_seconds,
    )
    captured_at = float(snapshot.get("snapshot_timestamp") or time.time())
    if not cache_enabled:
        return dict(snapshot), _build_host_diagnostics_cache_meta(
            cache_enabled=False,
            cache_hit=False,
            cache_ttl_seconds=normalized_ttl_seconds,
            captured_at=captured_at,
        )

    with _HOST_DIAGNOSTICS_CACHE_LOCK:
        _HOST_DIAGNOSTICS_CACHE_ENTRY = {
            "key": cache_key,
            "payload": dict(snapshot),
            "captured_at": captured_at,
        }
    return dict(snapshot), _build_host_diagnostics_cache_meta(
        cache_enabled=True,
        cache_hit=False,
        cache_ttl_seconds=normalized_ttl_seconds,
        captured_at=captured_at,
    )


def build_runtime_diagnostic_item(
    *,
    instance_name: str,
    config: Any,
    rocketchat: Any | None,
    started: bool,
    data_dir: Path | None = None,
    message_index_max_entries: int | None = None,
) -> dict[str, Any]:
    client_name = _read_config_value(config, "display_name", "name") or str(instance_name or "").strip() or "-"
    bot_id = _read_config_value(config, "bot_id", "id") or client_name
    enabled = bool(_read_config_value(config, "enabled", default=False))
    server_url = _read_config_value(config, "server_url") or ""
    onebot_self_id = _coerce_optional_int(_read_config_value(config, "onebot_self_id"))

    client_snapshot: dict[str, Any] = {}
    if rocketchat is not None and hasattr(rocketchat, "build_diagnostic_snapshot"):
        try:
            raw_snapshot = rocketchat.build_diagnostic_snapshot()
        except Exception:
            raw_snapshot = {}
        if isinstance(raw_snapshot, dict):
            client_snapshot = dict(raw_snapshot)

    authenticated = bool(client_snapshot.get("authenticated"))
    if not enabled:
        status_code = "disabled"
        status_label = "已停用"
    elif started:
        status_code = "online" if authenticated else "starting"
        status_label = "已连接" if authenticated else "连接中"
    else:
        status_code = "pending"
        status_label = "未接入"

    snapshot_path = data_dir / "runtime.snapshot.bin" if data_dir is not None else None
    journal_path = data_dir / "runtime.journal.bin" if data_dir is not None else None
    state_path = data_dir / "runtime_state.json" if data_dir is not None else None

    return {
        "bot_id": bot_id,
        "client_name": client_name,
        "enabled": enabled,
        "started": bool(started),
        "authenticated": authenticated,
        "status_code": status_code,
        "status_label": status_label,
        "auth_state": str(client_snapshot.get("auth_state") or ("authenticated" if authenticated else "disconnected")),
        "server_url": str(server_url or "").strip() or "-",
        "server_version": str(client_snapshot.get("server_version") or "unknown"),
        "compatibility_status": str(client_snapshot.get("compatibility_status") or "unknown"),
        "upload_endpoint": str(client_snapshot.get("upload_endpoint") or "rooms.media"),
        "method_transport": str(client_snapshot.get("method_transport") or "rest"),
        "method_rest_fallbacks": int(client_snapshot.get("method_rest_fallbacks") or 0),
        "onebot_self_id": onebot_self_id,
        "reconnect_failures": int(client_snapshot.get("reconnect_failures") or 0),
        "last_rest_login_at": normalize_timestamp(client_snapshot.get("last_rest_login_at")),
        "last_websocket_activity_at": normalize_timestamp(client_snapshot.get("last_websocket_activity_at")),
        "last_inbound_message_at": normalize_timestamp(client_snapshot.get("last_inbound_message_at")),
        "last_outbound_message_at": normalize_timestamp(client_snapshot.get("last_outbound_message_at")),
        "last_disconnect_reason": str(client_snapshot.get("last_disconnect_reason") or "").strip(),
        "message_index_max_entries": _coerce_optional_int(message_index_max_entries),
        "data_dir": str(data_dir) if data_dir is not None else "",
        "runtime_state_path": str(state_path) if state_path is not None else "",
        "runtime_state_bytes": get_file_size_bytes(state_path),
        "runtime_snapshot_path": str(snapshot_path) if snapshot_path is not None else "",
        "runtime_snapshot_bytes": get_file_size_bytes(snapshot_path),
        "runtime_journal_path": str(journal_path) if journal_path is not None else "",
        "runtime_journal_bytes": get_file_size_bytes(journal_path),
    }


def format_host_diagnostics_text(
    host_snapshot: dict[str, Any],
    *,
    runtime_items: list[dict[str, Any]] | None = None,
) -> str:
    lines = [
        "【系统概览】",
        f"RocketCatShell 版本：{host_snapshot.get('product_version') or '-'}",
        f"Python 版本：{host_snapshot.get('python_version') or '-'}",
        f"主机名：{host_snapshot.get('hostname') or '-'}",
        f"系统：{host_snapshot.get('system_label') or '-'}",
        f"快照时间：{host_snapshot.get('snapshot_time') or '-'}",
        "",
        "【CPU】",
        f"型号：{host_snapshot.get('cpu_model') or '-'}",
        f"核心：{host_snapshot.get('cpu_cores') or '-'}",
        f"当前主频：{host_snapshot.get('cpu_frequency') or '-'}",
        f"系统占用：{host_snapshot.get('cpu_usage') or '-'}",
        f"Shell 进程占用：{host_snapshot.get('process_cpu_usage') or '-'}",
        "",
        "【内存】",
        f"总量：{host_snapshot.get('memory_total') or '-'}",
        f"已用：{host_snapshot.get('memory_used') or '-'}",
        f"可用：{host_snapshot.get('memory_available') or '-'}",
        f"Shell 进程占用：{host_snapshot.get('process_memory') or '-'}",
    ]

    normalized_runtime_items = [item for item in (runtime_items or []) if isinstance(item, dict)]
    if normalized_runtime_items:
        lines.extend(["", "【运行诊断】"])
        for index, item in enumerate(normalized_runtime_items):
            lines.extend(format_runtime_diagnostic_lines(item))
            if index + 1 < len(normalized_runtime_items):
                lines.append("")
    return "\n".join(lines)


def format_runtime_diagnostic_lines(item: dict[str, Any]) -> list[str]:
    lines = [
        f"Bot：{item.get('client_name') or '-'}",
        f"连接状态：{item.get('status_label') or '-'}",
        f"认证状态：{format_auth_state(item.get('auth_state'))}",
        f"Rocket.Chat 服务器：{item.get('server_url') or '-'}",
        f"Rocket.Chat 版本：{item.get('server_version') or 'unknown'}（{item.get('compatibility_status') or 'unknown'}）",
        f"媒体上传端点：{item.get('upload_endpoint') or '-'}",
        f"Method 传输：{item.get('method_transport') or '-'}（REST 回退 {int(item.get('method_rest_fallbacks') or 0)} 次）",
        f"OneBot self_id：{item.get('onebot_self_id') or '-'}",
        f"重连失败次数：{int(item.get('reconnect_failures') or 0)}",
        f"最近 REST 登录：{format_timestamp_label(item.get('last_rest_login_at'))}",
        f"最近 WebSocket 活动：{format_timestamp_label(item.get('last_websocket_activity_at'))}",
        f"最近入站消息：{format_timestamp_label(item.get('last_inbound_message_at'))}",
        f"最近出站消息：{format_timestamp_label(item.get('last_outbound_message_at'))}",
        f"消息映射窗口上限：{item.get('message_index_max_entries') or '-'}",
        f"快照文件：{format_size_bytes(item.get('runtime_snapshot_bytes'))}",
        f"Journal 文件：{format_size_bytes(item.get('runtime_journal_bytes'))}",
    ]
    last_disconnect_reason = str(item.get("last_disconnect_reason") or "").strip()
    if last_disconnect_reason:
        lines.append(f"最近断开原因：{last_disconnect_reason}")
    return lines


def format_auth_state(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "authenticated":
        return "已认证"
    if normalized == "partial":
        return "部分认证"
    if normalized == "disconnected":
        return "未认证"
    return str(value or "-").strip() or "-"


def format_timestamp_label(timestamp: Any) -> str:
    normalized = normalize_timestamp(timestamp)
    if normalized is None:
        return "-"
    now = time.time()
    elapsed = max(0.0, now - normalized)
    if elapsed < 60:
        age_label = f"{int(elapsed)}s 前"
    elif elapsed < 3600:
        age_label = f"{int(elapsed // 60)}m 前"
    elif elapsed < 86400:
        age_label = f"{int(elapsed // 3600)}h 前"
    else:
        age_label = f"{int(elapsed // 86400)}d 前"
    return f"{datetime.fromtimestamp(normalized).strftime('%Y-%m-%d %H:%M:%S')} ({age_label})"


def format_size_bytes(value: Any) -> str:
    if value is None:
        return "-"
    normalized = float(value)
    if normalized < 1024:
        return f"{int(normalized)} B"
    if normalized < 1024 ** 2:
        return f"{normalized / 1024:.2f} KB"
    if normalized < 1024 ** 3:
        return f"{normalized / (1024 ** 2):.2f} MB"
    return f"{normalized / (1024 ** 3):.2f} GB"


def normalize_timestamp(value: Any) -> float | None:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def get_file_size_bytes(path: Path | None) -> int | None:
    if path is None or not path.exists():
        return None
    try:
        return int(path.stat().st_size)
    except OSError:
        return None


def build_system_label() -> str:
    system_name = str(platform.system() or "Unknown").strip()
    release = str(platform.release() or "").strip()
    machine = str(platform.machine() or "").strip()
    version = str(platform.version() or "").strip()

    if system_name == "Linux":
        pretty_name = _resolve_linux_pretty_name()
        parts = [part for part in [pretty_name, machine] if part]
        base_label = " ".join(parts).strip() or "Linux"
        if release:
            return f"{base_label} (kernel {release})"
        return base_label

    parts = [part for part in [system_name, release, machine] if part]
    base_label = " ".join(parts).strip() or "Unknown"
    if version:
        return f"{base_label} ({version})"
    return base_label


def resolve_cpu_model() -> str:
    candidates = [
        _resolve_linux_cpu_model(),
        resolve_windows_cpu_product_name(),
        platform.processor(),
        os.environ.get("PROCESSOR_IDENTIFIER", ""),
        platform.uname().processor,
    ]
    for candidate in candidates:
        normalized_candidate = str(candidate or "").strip()
        if normalized_candidate:
            return normalized_candidate
    return "-"


def _resolve_linux_pretty_name() -> str:
    if platform.system() != "Linux":
        return ""

    freedesktop_os_release = getattr(platform, "freedesktop_os_release", None)
    if callable(freedesktop_os_release):
        try:
            data = freedesktop_os_release()
        except OSError:
            data = {}
        if isinstance(data, dict):
            pretty_name = str(data.get("PRETTY_NAME") or "").strip()
            if pretty_name:
                return pretty_name
            name = str(data.get("NAME") or "").strip()
            version_id = str(data.get("VERSION_ID") or "").strip()
            if name and version_id:
                return f"{name} {version_id}"
            if name:
                return name

    try:
        with open("/etc/os-release", "r", encoding="utf-8", errors="ignore") as fp:
            lines = fp.readlines()
    except OSError:
        return ""

    parsed: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip().strip('"')

    pretty_name = str(parsed.get("PRETTY_NAME") or "").strip()
    if pretty_name:
        return pretty_name
    name = str(parsed.get("NAME") or "").strip()
    version_id = str(parsed.get("VERSION_ID") or "").strip()
    if name and version_id:
        return f"{name} {version_id}"
    return name


def _resolve_linux_cpu_model() -> str:
    if platform.system() != "Linux":
        return ""

    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as fp:
            for raw_line in fp:
                line = raw_line.strip()
                if not line or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                normalized_key = key.strip().lower()
                if normalized_key in {"model name", "hardware", "processor", "model"}:
                    normalized_value = value.strip()
                    if normalized_value:
                        return normalized_value
    except OSError:
        return ""

    return ""


def resolve_windows_cpu_product_name() -> str:
    if os.name != "nt" or winreg is None:
        return ""

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as processor_key:
            value, _ = winreg.QueryValueEx(processor_key, "ProcessorNameString")
    except OSError:
        return ""

    return str(value or "").strip()


def format_cpu_cores(physical_cores: int | None, logical_cores: int | None) -> str:
    physical_text = str(physical_cores) if isinstance(physical_cores, int) and physical_cores > 0 else "-"
    logical_text = str(logical_cores) if isinstance(logical_cores, int) and logical_cores > 0 else "-"
    return f"{physical_text} 物理 / {logical_text} 逻辑"


def format_frequency(frequency_mhz: float | int | None) -> str:
    if frequency_mhz is None:
        return "-"
    normalized_frequency_mhz = float(frequency_mhz)
    if normalized_frequency_mhz <= 0:
        return "-"
    return f"{normalized_frequency_mhz / 1000:.2f} GHz"


def format_percent(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def format_bytes_as_gb(value: int | float | None) -> str:
    if value is None:
        return "-"
    normalized_value = float(value)
    if normalized_value <= 0:
        return "0.00 GB"
    return f"{normalized_value / (1024 ** 3):.2f} GB"


def format_bytes_as_mb(value: int | float | None) -> str:
    if value is None:
        return "-"
    normalized_value = float(value)
    if normalized_value <= 0:
        return "0.00 MB"
    return f"{normalized_value / (1024 ** 2):.2f} MB"


def _coerce_optional_int(value: Any) -> int | None:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 else None


def _read_config_value(config: Any, *names: str, default: Any = "") -> Any:
    for name in names:
        if hasattr(config, name):
            return getattr(config, name)
        if isinstance(config, dict) and name in config:
            return config[name]
    return default

from __future__ import annotations

import asyncio
import os
import platform
import socket
import time
from datetime import datetime
from typing import Any

try:
    import winreg
except ImportError:
    winreg = None

try:
    import psutil
except ImportError:
    psutil = None

from rocketcat_shell.logger import logger
from rocketcat_shell.plugin_system.base import PluginExecutionContext, RocketCatPlugin


_ROCKETCAT_COMMAND = "#rocketcat"
_SYSTEM_COMMAND = "#system"
_SYSTEM_CPU_SAMPLE_SECONDS = 0.2


class Plugin(RocketCatPlugin):
    def __init__(self, context, config: dict[str, Any]):
        super().__init__(context, config)
        self._suppressed_self_echo_message_ids: set[str] = set()

    async def on_load(self, runtime: PluginExecutionContext) -> None:
        logger.info(
            "[RocketCatShell][Plugin:%s] 已加载到运行时 %s。",
            self.context.plugin_id,
            runtime.instance_name,
        )

    async def on_unload(self, runtime: PluginExecutionContext) -> None:
        self._suppressed_self_echo_message_ids.clear()
        logger.info(
            "[RocketCatShell][Plugin:%s] 已从运行时 %s 卸载。",
            self.context.plugin_id,
            runtime.instance_name,
        )

    async def on_inbound_message(
        self,
        event: dict[str, Any],
        raw_msg: dict[str, Any],
        runtime: PluginExecutionContext,
    ) -> bool | None:
        if self._consume_suppressed_self_echo(raw_msg):
            logger.info(
                "[RocketCatShell][Plugin:%s] 已拦截内置指令回显上报。",
                self.context.plugin_id,
            )
            return False

        if not self.enabled:
            return None

        message_text = str(event.get("rocketchat_current_message_text") or "").strip()
        command_text = self._resolve_command(message_text)
        if command_text is None:
            return None

        room_id = str(event.get("rocketchat_room_source_id") or "").strip()
        if not room_id:
            logger.warning(
                "[RocketCatShell][Plugin:%s] 内置指令 %s 命中但缺少 room_id，已回退正常消息流。",
                self.context.plugin_id,
                command_text,
            )
            return None

        sent_messages = await self._dispatch_command(command_text, room_id, runtime)
        if not sent_messages:
            logger.warning(
                "[RocketCatShell][Plugin:%s] 内置指令 %s 回复发送失败，已回退正常消息流。",
                self.context.plugin_id,
                command_text,
            )
            return None

        self._remember_suppressed_self_echoes(sent_messages)

        logger.info(
            "[RocketCatShell][Plugin:%s] 已处理内置指令 %s | room_id=%s | reply_count=%s",
            self.context.plugin_id,
            command_text,
            room_id,
            len(sent_messages),
        )
        return False

    def _resolve_command(self, message_text: str) -> str | None:
        normalized_message_text = str(message_text or "").strip()
        if normalized_message_text == _ROCKETCAT_COMMAND:
            return _ROCKETCAT_COMMAND
        if normalized_message_text == _SYSTEM_COMMAND:
            return _SYSTEM_COMMAND
        return None

    async def _dispatch_command(
        self,
        command_text: str,
        room_id: str,
        runtime: PluginExecutionContext,
    ) -> list[dict[str, Any]]:
        if command_text == _ROCKETCAT_COMMAND:
            return await self._handle_rocketcat_command(room_id, runtime)
        if command_text == _SYSTEM_COMMAND:
            return await self._handle_system_command(room_id, runtime)
        return []

    async def _handle_rocketcat_command(
        self,
        room_id: str,
        runtime: PluginExecutionContext,
    ) -> list[dict[str, Any]]:
        reply_context = await self._build_reply_context(runtime)
        return await self._send_reply_sections(room_id, reply_context, runtime)

    async def _handle_system_command(
        self,
        room_id: str,
        runtime: PluginExecutionContext,
    ) -> list[dict[str, Any]]:
        try:
            system_snapshot_text = await self._build_system_snapshot_text()
        except RuntimeError as exc:
            logger.warning(
                "[RocketCatShell][Plugin:%s] 构建 #system 快照失败: %r",
                self.context.plugin_id,
                exc,
            )
            system_snapshot_text = self._build_system_unavailable_text(str(exc))
        except Exception as exc:
            logger.warning(
                "[RocketCatShell][Plugin:%s] 构建 #system 快照异常: %r",
                self.context.plugin_id,
                exc,
            )
            system_snapshot_text = self._build_system_unavailable_text("系统快照构建异常，请稍后重试。")

        try:
            sent_message = await runtime.rocketchat.send_text(room_id, system_snapshot_text)
        except Exception as exc:
            logger.warning(
                "[RocketCatShell][Plugin:%s] 发送 #system 文本消息失败: %r",
                self.context.plugin_id,
                exc,
            )
            return []
        return [sent_message] if sent_message is not None else []

    async def _build_reply_context(self, runtime: PluginExecutionContext) -> dict[str, str]:
        client_name = str(runtime.bridge_config.display_name or runtime.instance_name or "").strip() or "-"
        login_username = str(runtime.bridge_config.username or "").strip()
        nickname = login_username or client_name

        try:
            user_info = await runtime.rocketchat.get_current_user_info()
        except Exception as exc:
            logger.warning(
                "[RocketCatShell][Plugin:%s] 读取当前用户信息失败: %r",
                self.context.plugin_id,
                exc,
            )
            user_info = {}

        if user_info:
            login_username = str(
                user_info.get("username")
                or runtime.rocketchat.bot_username
                or login_username
            ).strip()
            nickname = str(
                user_info.get("name")
                or user_info.get("nickname")
                or login_username
                or client_name
            ).strip()
        else:
            login_username = str(runtime.rocketchat.bot_username or login_username).strip()

        normalized_login_username = login_username or "-"
        normalized_nickname = nickname or normalized_login_username or client_name or "-"
        normalized_self_id = str(runtime.bridge_config.onebot_self_id or "").strip() or "-"
        normalized_server_url = str(runtime.bridge_config.server_url or "").strip() or "-"
        bot_avatar_url = str(runtime.rocketchat.build_avatar_url(login_username) or "").strip()

        server_avatar_url = ""
        try:
            server_branding_summary = await runtime.rocketchat.get_server_branding_summary()
        except Exception as exc:
            logger.warning(
                "[RocketCatShell][Plugin:%s] 读取 Rocket.Chat 品牌信息失败: %r",
                self.context.plugin_id,
                exc,
            )
            server_branding_summary = None
        if server_branding_summary:
            server_avatar_url = str(server_branding_summary.get("avatar_url") or "").strip()
        if not server_avatar_url:
            server_avatar_url = str(runtime.rocketchat.build_server_logo_url() or "").strip()

        return {
            "client_name": client_name,
            "login_username": normalized_login_username,
            "nickname": normalized_nickname,
            "self_id": normalized_self_id,
            "server_url": normalized_server_url,
            "status_label": self._resolve_status_label(runtime),
            "bot_avatar_url": bot_avatar_url,
            "server_avatar_url": server_avatar_url,
        }

    async def _send_reply_sections(
        self,
        room_id: str,
        reply_context: dict[str, str],
        runtime: PluginExecutionContext,
    ) -> list[dict[str, Any]]:
        sent_messages: list[dict[str, Any]] = []

        bot_section_message = await self._send_section_message(
            room_id,
            self._build_bot_section_text(reply_context),
            reply_context.get("bot_avatar_url", ""),
            runtime,
            section_label="bot_avatar",
        )
        if bot_section_message is not None:
            sent_messages.append(bot_section_message)

        server_section_message = await self._send_section_message(
            room_id,
            self._build_server_section_text(reply_context),
            reply_context.get("server_avatar_url", ""),
            runtime,
            section_label="server_branding",
        )
        if server_section_message is not None:
            sent_messages.append(server_section_message)

        return sent_messages

    async def _build_system_snapshot_text(self) -> str:
        snapshot = await asyncio.to_thread(self._collect_system_snapshot)
        return self._format_system_snapshot(snapshot)

    def _collect_system_snapshot(self) -> dict[str, str]:
        if psutil is None:
            raise RuntimeError("缺少依赖 psutil，请先安装 requirements.txt 后再使用 #system。")

        process = psutil.Process(os.getpid())
        process.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None)
        time.sleep(_SYSTEM_CPU_SAMPLE_SECONDS)

        system_cpu_percent = psutil.cpu_percent(interval=None)
        process_cpu_percent = process.cpu_percent(interval=None)
        virtual_memory = psutil.virtual_memory()
        process_memory = process.memory_info().rss
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
            "shell_version": self._resolve_plugin_version(),
            "python_version": platform.python_version(),
            "hostname": socket.gethostname() or "-",
            "system_label": self._build_system_label(),
            "snapshot_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cpu_model": self._resolve_cpu_model(),
            "cpu_cores": self._format_cpu_cores(physical_cores, logical_cores),
            "cpu_frequency": self._format_frequency(current_frequency_mhz),
            "cpu_usage": self._format_percent(system_cpu_percent),
            "process_cpu_usage": self._format_percent(process_cpu_percent),
            "memory_total": self._format_bytes_as_gb(virtual_memory.total),
            "memory_used": self._format_bytes_as_gb(virtual_memory.used),
            "memory_available": self._format_bytes_as_gb(virtual_memory.available),
            "process_memory": self._format_bytes_as_mb(process_memory),
        }

    def _resolve_plugin_version(self) -> str:
        metadata_version = str(self.context.metadata.get("version") or "").strip()
        if metadata_version:
            return metadata_version
        return "-"

    def _format_system_snapshot(self, snapshot: dict[str, str]) -> str:
        return "\n".join(
            [
                "【系统概览】",
                f"RocketCatShell 版本：{snapshot.get('shell_version') or '-'}",
                f"Python 版本：{snapshot.get('python_version') or '-'}",
                f"主机名：{snapshot.get('hostname') or '-'}",
                f"系统：{snapshot.get('system_label') or '-'}",
                f"快照时间：{snapshot.get('snapshot_time') or '-'}",
                "",
                "【CPU】",
                f"型号：{snapshot.get('cpu_model') or '-'}",
                f"核心：{snapshot.get('cpu_cores') or '-'}",
                f"当前主频：{snapshot.get('cpu_frequency') or '-'}",
                f"系统占用：{snapshot.get('cpu_usage') or '-'}",
                f"Shell 进程占用：{snapshot.get('process_cpu_usage') or '-'}",
                "",
                "【内存】",
                f"总量：{snapshot.get('memory_total') or '-'}",
                f"已用：{snapshot.get('memory_used') or '-'}",
                f"可用：{snapshot.get('memory_available') or '-'}",
                f"Shell 进程占用：{snapshot.get('process_memory') or '-'}",
            ]
        )

    def _build_system_unavailable_text(self, reason: str) -> str:
        normalized_reason = str(reason or "系统快照暂时不可用。")
        return "\n".join(
            [
                "【系统概览】",
                "#system 当前暂时不可用。",
                f"原因：{normalized_reason}",
            ]
        )

    def _build_system_label(self) -> str:
        system_name = str(platform.system() or "Unknown").strip()
        release = str(platform.release() or "").strip()
        machine = str(platform.machine() or "").strip()
        version = str(platform.version() or "").strip()

        if system_name == "Linux":
            pretty_name = self._resolve_linux_pretty_name()
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

    def _resolve_cpu_model(self) -> str:
        candidates = [
            self._resolve_linux_cpu_model(),
            self._resolve_windows_cpu_product_name(),
            platform.processor(),
            os.environ.get("PROCESSOR_IDENTIFIER", ""),
            platform.uname().processor,
        ]
        for candidate in candidates:
            normalized_candidate = str(candidate or "").strip()
            if normalized_candidate:
                return normalized_candidate
        return "-"

    def _resolve_linux_pretty_name(self) -> str:
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

    def _resolve_linux_cpu_model(self) -> str:
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

    def _resolve_windows_cpu_product_name(self) -> str:
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

    def _format_cpu_cores(self, physical_cores: int | None, logical_cores: int | None) -> str:
        physical_text = str(physical_cores) if isinstance(physical_cores, int) and physical_cores > 0 else "-"
        logical_text = str(logical_cores) if isinstance(logical_cores, int) and logical_cores > 0 else "-"
        return f"{physical_text} 物理 / {logical_text} 逻辑"

    def _format_frequency(self, frequency_mhz: float | int | None) -> str:
        if frequency_mhz is None:
            return "-"
        normalized_frequency_mhz = float(frequency_mhz)
        if normalized_frequency_mhz <= 0:
            return "-"
        return f"{normalized_frequency_mhz / 1000:.2f} GHz"

    def _format_percent(self, value: float | int | None) -> str:
        if value is None:
            return "-"
        return f"{float(value):.2f}%"

    def _format_bytes_as_gb(self, value: int | float | None) -> str:
        if value is None:
            return "-"
        normalized_value = float(value)
        if normalized_value <= 0:
            return "0.00 GB"
        return f"{normalized_value / (1024 ** 3):.2f} GB"

    def _format_bytes_as_mb(self, value: int | float | None) -> str:
        if value is None:
            return "-"
        normalized_value = float(value)
        if normalized_value <= 0:
            return "0.00 MB"
        return f"{normalized_value / (1024 ** 2):.2f} MB"

    async def _send_section_message(
        self,
        room_id: str,
        text: str,
        image_url: str,
        runtime: PluginExecutionContext,
        *,
        section_label: str,
    ) -> dict[str, Any] | None:
        normalized_image_url = str(image_url or "").strip()
        if normalized_image_url:
            try:
                sent_message = await runtime.rocketchat.send_image_url(
                    room_id,
                    normalized_image_url,
                    text=text,
                )
            except Exception as exc:
                logger.warning(
                    "[RocketCatShell][Plugin:%s] 发送 %s 图文消息失败，准备降级为纯文本: %r",
                    self.context.plugin_id,
                    section_label,
                    exc,
                )
            else:
                if sent_message is not None:
                    return sent_message
                logger.warning(
                    "[RocketCatShell][Plugin:%s] 发送 %s 图文消息失败，准备降级为纯文本。",
                    self.context.plugin_id,
                    section_label,
                )

        try:
            return await runtime.rocketchat.send_text(room_id, text)
        except Exception as exc:
            logger.warning(
                "[RocketCatShell][Plugin:%s] 发送 %s 纯文本消息失败: %r",
                self.context.plugin_id,
                section_label,
                exc,
            )
            return None

    def _build_bot_section_text(self, reply_context: dict[str, str]) -> str:
        login_username = reply_context.get("login_username") or "-"
        mention_line = f"@{login_username}" if login_username != "-" else "-"
        status_label = reply_context.get("status_label") or "-"
        status_emoji = self._resolve_status_emoji(status_label)
        return "\n".join(
            [
                f"bot名字：{reply_context.get('client_name') or '-'}",
                mention_line,
                f"连接状态：{status_label} {status_emoji}" if status_emoji else f"连接状态：{status_label}",
                f"聊天显示昵称：{reply_context.get('nickname') or '-'}",
                f"Rocket.Chat 用户名：{login_username}",
                f"OneBot self_id：{reply_context.get('self_id') or '-'}",
                "头像：",
            ]
        )

    def _build_server_section_text(self, reply_context: dict[str, str]) -> str:
        return "\n".join(
            [
                f"Rocket.Chat 服务器：{reply_context.get('server_url') or '-'}",
                "频道头像：",
            ]
        )

    def _remember_suppressed_self_echoes(self, sent_messages: list[dict[str, Any]]) -> None:
        for sent_message in sent_messages:
            source_message_id = str(sent_message.get("_id") or "").strip()
            if source_message_id:
                self._suppressed_self_echo_message_ids.add(source_message_id)
                continue
            logger.warning(
                "[RocketCatShell][Plugin:%s] 内置指令回复缺少 source_id，无法抑制回显上报。",
                self.context.plugin_id,
            )

    def _resolve_status_label(self, runtime: PluginExecutionContext) -> str:
        if runtime.rocketchat.auth_token and runtime.rocketchat.user_id:
            return "已连接"
        if runtime.rocketchat.auth_token or runtime.rocketchat.user_id:
            return "连接中"
        return "未接入"

    def _resolve_status_emoji(self, status_label: str) -> str:
        normalized_status = str(status_label or "").strip()
        if not normalized_status or normalized_status == "-":
            return ""
        if normalized_status == "已连接":
            return "✅"
        return "❌"

    def _consume_suppressed_self_echo(self, raw_msg: dict[str, Any]) -> bool:
        source_message_id = str(raw_msg.get("_id") or "").strip()
        if not source_message_id:
            return False
        if source_message_id not in self._suppressed_self_echo_message_ids:
            return False
        self._suppressed_self_echo_message_ids.discard(source_message_id)
        return True
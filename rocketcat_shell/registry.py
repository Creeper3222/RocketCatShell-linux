from __future__ import annotations

import secrets
from pathlib import Path

from .bridge.transports import TransportValidationError, normalize_transport
from .logger import logger
from .models import BotRecord, ShellSettings
from .settings import read_json, write_json


class BotRegistry:
    FORMAT_VERSION = 1

    def __init__(self, path: Path, transport_path: Path | None = None):
        self.path = path
        self.transport_path = transport_path or path.with_name("onebot_transports.json")
        self.loaded_transport_format_supported = True
        self.loaded_transport_state_valid = True

    def load(self, *, defaults: ShellSettings) -> list[BotRecord]:
        self.loaded_transport_format_supported = True
        self.loaded_transport_state_valid = True
        payload = read_json(self.path, {"bots": []})
        items = payload.get("bots", []) if isinstance(payload, dict) else []
        transport_payload = read_json(
            self.transport_path,
            {"format_version": self.FORMAT_VERSION, "transports": {}},
        )
        transport_format_version = (
            transport_payload.get("format_version")
            if isinstance(transport_payload, dict)
            else None
        )
        if transport_format_version != self.FORMAT_VERSION:
            self.loaded_transport_format_supported = False
            self.loaded_transport_state_valid = False
            logger.error(
                "[RocketCatShell] 不支持的 OneBot 传输配置格式，将回退到 bots.json 兼容字段且保留原文件 | path=%s | format_version=%r",
                self.transport_path,
                transport_format_version,
            )
            transport_payload = {}
        stored_transports = (
            transport_payload.get("transports", {})
            if isinstance(transport_payload, dict)
            else {}
        )
        if not isinstance(stored_transports, dict):
            stored_transports = {}
        bots: list[BotRecord] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            bot = BotRecord.from_mapping(raw_item, defaults=defaults)
            if not bot.bot_id:
                bot.bot_id = self._generate_bot_id()
            if not bot.name:
                bot.name = bot.bot_id
            stored_transport = stored_transports.get(bot.bot_id)
            transport_source = (
                stored_transport if isinstance(stored_transport, dict) else None
            )
            try:
                normalized_transport = normalize_transport(
                    transport_source,
                    shell_defaults=defaults,
                    legacy_payload=raw_item,
                )
                if transport_source is None:
                    if self.loaded_transport_format_supported:
                        logger.info(
                            "[RocketCatShell] 已将旧版 OneBot 配置迁移为正式传输配置 | bot_id=%s | type=%s",
                            bot.bot_id,
                            normalized_transport.get("type") or "-",
                        )
                else:
                    try:
                        baseline_transport = normalize_transport(
                            transport_source,
                            shell_defaults=defaults,
                        )
                    except TransportValidationError:
                        baseline_transport = None
                    if (
                        baseline_transport is None
                        or baseline_transport != normalized_transport
                    ):
                        logger.info(
                            "[RocketCatShell] 已协调旧版修改与正式 OneBot 传输配置 | bot_id=%s | type=%s",
                            bot.bot_id,
                            normalized_transport.get("type") or "-",
                        )
            except TransportValidationError as exc:
                self.loaded_transport_state_valid = False
                logger.error(
                    "[RocketCatShell] bot OneBot 传输配置无效，将保留配置供 WebUI 修复 | bot_id=%s | error=%s",
                    bot.bot_id,
                    exc,
                )
                normalized_transport = (
                    transport_source
                    if isinstance(transport_source, dict)
                    else bot.onebot_transport_mapping()
                )
            bot.apply_onebot_transport(normalized_transport)
            bots.append(bot)
        if (
            not self.path.exists() or not self.transport_path.exists()
        ) and self.loaded_transport_state_valid:
            self.save(bots)
        return bots

    def save(self, bots: list[BotRecord]) -> None:
        previous_registry = self.path.read_bytes() if self.path.exists() else None
        previous_transports = (
            self.transport_path.read_bytes() if self.transport_path.exists() else None
        )
        try:
            write_json(
                self.path,
                {"bots": [bot.to_legacy_mapping() for bot in bots]},
            )
            write_json(
                self.transport_path,
                {
                    "format_version": self.FORMAT_VERSION,
                    "transports": {
                        bot.bot_id: bot.onebot_transport_mapping() for bot in bots
                    },
                },
            )
        except Exception:
            self._restore_snapshot(self.path, previous_registry)
            self._restore_snapshot(self.transport_path, previous_transports)
            raise

    def _generate_bot_id(self) -> str:
        return f"bot_{secrets.token_hex(4)}"

    @staticmethod
    def _restore_snapshot(path: Path, payload: bytes | None) -> None:
        if payload is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)

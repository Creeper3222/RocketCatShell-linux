from __future__ import annotations

import secrets
from pathlib import Path

from .models import DEFAULT_START_SELF_ID, BotRecord, ShellSettings
from .settings import read_json, write_json


class BotRegistry:
    def __init__(self, path: Path):
        self.path = path

    def load(self, *, defaults: ShellSettings) -> list[BotRecord]:
        payload = read_json(self.path, {"bots": []})
        items = payload.get("bots", []) if isinstance(payload, dict) else []
        bots: list[BotRecord] = []
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            bot = BotRecord.from_mapping(raw_item, defaults=defaults)
            if not bot.bot_id:
                bot.bot_id = self._generate_bot_id()
            if not bot.name:
                bot.name = bot.bot_id
            bots.append(bot)
        if not self.path.exists():
            self.save(bots)
        return bots

    def save(self, bots: list[BotRecord]) -> None:
        write_json(self.path, {"bots": [bot.to_mapping() for bot in bots]})

    def next_suggested_self_id(self, bots: list[BotRecord], *, floor: int = DEFAULT_START_SELF_ID) -> int:
        current_max = int(floor) - 1
        for bot in bots:
            if bot.onebot_self_id > current_max:
                current_max = bot.onebot_self_id
        return current_max + 1

    def _generate_bot_id(self) -> str:
        return f"bot_{secrets.token_hex(4)}"
from __future__ import annotations

from typing import Any

from rocketcat_shell.logger import logger
from rocketcat_shell.plugin_system.base import PluginExecutionContext, RocketCatPlugin


_DEFAULT_THINKING_EMOJI_ID = 66
_DEFAULT_DONE_EMOJI_ID = 74
_DEFAULT_THINKING_REACTION = ":heart:"
_DEFAULT_DONE_REACTION = ":sunny:"


class Plugin(RocketCatPlugin):
    def __init__(self, context, config: dict[str, Any]):
        super().__init__(context, config)
        self._numeric_reaction_states: dict[str, dict[str, Any]] = {}

    async def on_load(self, runtime: PluginExecutionContext) -> None:
        logger.info(
            "[RocketCatShell][Plugin:%s] 已加载到运行时 %s。",
            self.context.plugin_id,
            runtime.instance_name,
        )

    async def on_unload(self, runtime: PluginExecutionContext) -> None:
        self._numeric_reaction_states.clear()
        logger.info(
            "[RocketCatShell][Plugin:%s] 已从运行时 %s 卸载。",
            self.context.plugin_id,
            runtime.instance_name,
        )

    async def handle_onebot_action(
        self,
        action: str,
        params: dict[str, Any],
        runtime: PluginExecutionContext,
    ) -> dict[str, Any] | None:
        if not self.enabled or action != "set_msg_emoji_like":
            return None

        source_message_id = await runtime.resolve_message_source_id(params.get("message_id"))
        if not source_message_id:
            return runtime.failed(f"未知 message_id: {params.get('message_id')}", retcode=1404)

        should_react = self._coerce_bool(params.get("set", True))
        reaction = self._resolve_reaction_shortcode(
            params.get("emoji_id"),
            source_message_id=source_message_id,
            should_react=should_react,
        )
        if not reaction:
            return runtime.failed(f"未映射的 emoji_id: {params.get('emoji_id')}", retcode=1404)

        ok = await runtime.rocketchat.set_message_reaction(
            source_message_id,
            reaction,
            should_react=should_react,
        )
        if not ok:
            return runtime.failed("Rocket.Chat 贴表情失败", retcode=1500)

        return runtime.ok(
            {
                "message_id": params.get("message_id"),
                "reaction": reaction,
                "set": should_react,
            }
        )

    def _resolve_reaction_shortcode(
        self,
        emoji_id: Any,
        *,
        source_message_id: str,
        should_react: bool,
    ) -> str:
        if isinstance(emoji_id, str):
            normalized = emoji_id.strip()
            if normalized.startswith(":") and normalized.endswith(":") and len(normalized) > 2:
                return normalized
            try:
                emoji_id = int(normalized)
            except ValueError:
                return ""
        elif not isinstance(emoji_id, int):
            try:
                emoji_id = int(emoji_id)
            except (TypeError, ValueError):
                return ""

        return self._resolve_numeric_reaction_shortcode(
            int(emoji_id),
            source_message_id=source_message_id,
            should_react=should_react,
        )

    def _resolve_numeric_reaction_shortcode(
        self,
        emoji_id: int,
        *,
        source_message_id: str,
        should_react: bool,
    ) -> str:
        # `astrbot_plugin_iamthinking` only passes numeric QQ emoji ids. On Rocket.Chat we
        # normalize those numeric ids into a fixed processing/done pair instead of trying to
        # maintain a full QQ->Rocket.Chat emoji table.
        state = self._numeric_reaction_states.setdefault(
            source_message_id,
            {"phase": "initial", "thinking_emoji_id": None},
        )

        if not should_react:
            phase = str(state.get("phase") or "initial")
            thinking_emoji_id = state.get("thinking_emoji_id")
            if phase == "done" and (
                thinking_emoji_id is None or int(thinking_emoji_id) != emoji_id
            ):
                self._numeric_reaction_states.pop(source_message_id, None)
                return self._done_reaction()
            if state.get("phase") != "done":
                self._numeric_reaction_states.pop(source_message_id, None)
            return self._thinking_reaction()

        if emoji_id == _DEFAULT_DONE_EMOJI_ID:
            state["phase"] = "done"
            return self._done_reaction()
        if emoji_id == _DEFAULT_THINKING_EMOJI_ID:
            if state.get("phase") != "done":
                state["phase"] = "thinking"
                state["thinking_emoji_id"] = emoji_id
            return self._thinking_reaction()

        phase = str(state.get("phase") or "initial")
        thinking_emoji_id = state.get("thinking_emoji_id")
        if phase == "done":
            return self._done_reaction()
        if thinking_emoji_id is None:
            state["phase"] = "thinking"
            state["thinking_emoji_id"] = emoji_id
            return self._thinking_reaction()
        if int(thinking_emoji_id) == emoji_id:
            return self._thinking_reaction()

        state["phase"] = "done"
        return self._done_reaction()

    def _thinking_reaction(self) -> str:
        return self._coerce_shortcode(
            self.config.get("llm_thinking_reaction", _DEFAULT_THINKING_REACTION),
            _DEFAULT_THINKING_REACTION,
        )

    def _done_reaction(self) -> str:
        return self._coerce_shortcode(
            self.config.get("llm_done_reaction", _DEFAULT_DONE_REACTION),
            _DEFAULT_DONE_REACTION,
        )

    def _coerce_shortcode(self, value: Any, fallback: str) -> str:
        normalized = str(value or "").strip()
        if normalized.startswith(":") and normalized.endswith(":") and len(normalized) > 2:
            return normalized
        return fallback

    def _coerce_bool(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        normalized = str(value or "").strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
        return bool(value)

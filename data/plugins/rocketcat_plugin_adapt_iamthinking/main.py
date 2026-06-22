from __future__ import annotations

import asyncio
import time
from typing import Any

from rocketcat_shell.logger import logger
from rocketcat_shell.plugin_system.base import PluginExecutionContext, RocketCatPlugin


_DEFAULT_THINKING_EMOJI_ID = 66
_DEFAULT_DONE_EMOJI_ID = 74
_DEFAULT_THINKING_REACTION = ":heart:"
_DEFAULT_DONE_REACTION = ":sunny:"
_DEFAULT_ENABLE_REACTIONS = True
_DEFAULT_ENABLE_TYPING_INDICATOR = True
_TYPING_RENEW_INTERVAL_SECONDS = 4.0
_REACTION_STATE_TTL_SECONDS = 3600.0
_REACTION_STATE_MAX_ENTRIES = 2048
_TYPING_MAX_DURATION_SECONDS = 600.0


class _ResolvedActionEffects:
    __slots__ = ("reaction", "typing_transition")

    def __init__(self, reaction: str, typing_transition: str | None = None):
        self.reaction = reaction
        self.typing_transition = typing_transition


class Plugin(RocketCatPlugin):
    handled_actions = frozenset({"set_msg_emoji_like"})

    def __init__(self, context, config: dict[str, Any]):
        super().__init__(context, config)
        self._numeric_reaction_states: dict[str, dict[str, Any]] = {}
        self._typing_room_members: dict[str, set[str]] = {}
        self._typing_room_tasks: dict[str, asyncio.Task[None]] = {}

    async def on_load(self, runtime: PluginExecutionContext) -> None:
        logger.info(
            "[RocketCatShell][Plugin:%s] 已加载到运行时 %s。",
            self.context.plugin_id,
            runtime.instance_name,
        )

    async def on_unload(self, runtime: PluginExecutionContext) -> None:
        await self._stop_all_typing(runtime)
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

        effects_enabled = self._reactions_enabled() or self._typing_indicator_enabled()
        if not effects_enabled:
            return runtime.ok(
                {
                    "message_id": params.get("message_id"),
                    "ignored": True,
                    "reason": "all_effects_disabled",
                }
            )

        source_message_id = await runtime.resolve_message_source_id(params.get("message_id"))
        if not source_message_id:
            return runtime.failed(f"未知 message_id: {params.get('message_id')}", retcode=1404)

        should_react = self._coerce_bool(params.get("set", True))
        effects = self._resolve_action_effects(
            params.get("emoji_id"),
            source_message_id=source_message_id,
            should_react=should_react,
        )
        if not effects.reaction:
            return runtime.failed(f"未映射的 emoji_id: {params.get('emoji_id')}", retcode=1404)

        reaction_applied: bool | None = None
        if self._reactions_enabled():
            reaction_applied = await runtime.rocketchat.set_message_reaction(
                source_message_id,
                effects.reaction,
                should_react=should_react,
            )

        typing_applied: bool | None = None
        if self._typing_indicator_enabled() and effects.typing_transition:
            room_source_id = await self._resolve_room_source_id(params.get("message_id"), runtime)
            if not room_source_id:
                logger.warning(
                    "[RocketCatShell][Plugin:%s] typing 处理跳过：未找到 message_id=%s 对应房间。",
                    self.context.plugin_id,
                    params.get("message_id"),
                )
                typing_applied = False
            else:
                typing_applied = await self._apply_typing_transition(
                    runtime,
                    source_message_id=source_message_id,
                    room_source_id=room_source_id,
                    transition=effects.typing_transition,
                )

        if self._should_fail_action(reaction_applied, typing_applied, effects.typing_transition):
            reasons: list[str] = []
            if reaction_applied is False:
                reasons.append("Rocket.Chat 贴表情失败")
            if typing_applied is False:
                reasons.append("Rocket.Chat typing 指示器更新失败")
            return runtime.failed("；".join(reasons) or "Rocket.Chat 适配失败", retcode=1500)

        return runtime.ok(
            {
                "message_id": params.get("message_id"),
                "reaction": effects.reaction,
                "set": should_react,
                "reaction_applied": reaction_applied,
                "typing_transition": effects.typing_transition,
                "typing_applied": typing_applied,
            }
        )

    def _resolve_action_effects(
        self,
        emoji_id: Any,
        *,
        source_message_id: str,
        should_react: bool,
    ) -> _ResolvedActionEffects:
        if isinstance(emoji_id, str):
            normalized = emoji_id.strip()
            if normalized.startswith(":") and normalized.endswith(":") and len(normalized) > 2:
                return _ResolvedActionEffects(reaction=normalized)
            try:
                emoji_id = int(normalized)
            except ValueError:
                return _ResolvedActionEffects(reaction="")
        elif not isinstance(emoji_id, int):
            try:
                emoji_id = int(emoji_id)
            except (TypeError, ValueError):
                return _ResolvedActionEffects(reaction="")

        return self._resolve_numeric_action_effects(
            int(emoji_id),
            source_message_id=source_message_id,
            should_react=should_react,
        )

    def _resolve_numeric_action_effects(
        self,
        emoji_id: int,
        *,
        source_message_id: str,
        should_react: bool,
    ) -> _ResolvedActionEffects:
        # `astrbot_plugin_iamthinking` only passes numeric QQ emoji ids. On Rocket.Chat we
        # normalize those numeric ids into a fixed processing/done pair instead of trying to
        # maintain a full QQ->Rocket.Chat emoji table.
        self._prune_reaction_states()
        state = self._numeric_reaction_states.setdefault(
            source_message_id,
            {
                "phase": "initial",
                "thinking_emoji_id": None,
                "updated_at": time.monotonic(),
            },
        )
        state["updated_at"] = time.monotonic()

        if not should_react:
            phase = str(state.get("phase") or "initial")
            thinking_emoji_id = state.get("thinking_emoji_id")
            if phase == "done" and (
                thinking_emoji_id is None or int(thinking_emoji_id) != emoji_id
            ):
                self._numeric_reaction_states.pop(source_message_id, None)
                return _ResolvedActionEffects(reaction=self._done_reaction())
            if state.get("phase") != "done":
                self._numeric_reaction_states.pop(source_message_id, None)
                return _ResolvedActionEffects(
                    reaction=self._thinking_reaction(),
                    typing_transition="stop",
                )
            return _ResolvedActionEffects(reaction=self._thinking_reaction())

        if emoji_id == _DEFAULT_DONE_EMOJI_ID:
            state["phase"] = "done"
            return _ResolvedActionEffects(
                reaction=self._done_reaction(),
                typing_transition="stop",
            )
        if emoji_id == _DEFAULT_THINKING_EMOJI_ID:
            if state.get("phase") != "done":
                phase = str(state.get("phase") or "initial")
                state["phase"] = "thinking"
                state["thinking_emoji_id"] = emoji_id
                return _ResolvedActionEffects(
                    reaction=self._thinking_reaction(),
                    typing_transition="start" if phase != "thinking" else None,
                )
            return _ResolvedActionEffects(reaction=self._thinking_reaction())

        phase = str(state.get("phase") or "initial")
        thinking_emoji_id = state.get("thinking_emoji_id")
        if phase == "done":
            return _ResolvedActionEffects(reaction=self._done_reaction())
        if thinking_emoji_id is None:
            state["phase"] = "thinking"
            state["thinking_emoji_id"] = emoji_id
            return _ResolvedActionEffects(
                reaction=self._thinking_reaction(),
                typing_transition="start",
            )
        if int(thinking_emoji_id) == emoji_id:
            return _ResolvedActionEffects(reaction=self._thinking_reaction())

        state["phase"] = "done"
        return _ResolvedActionEffects(
            reaction=self._done_reaction(),
            typing_transition="stop",
        )

    def _prune_reaction_states(self) -> None:
        cutoff = time.monotonic() - _REACTION_STATE_TTL_SECONDS
        expired = [
            source_id
            for source_id, state in self._numeric_reaction_states.items()
            if float(state.get("updated_at") or 0.0) < cutoff
        ]
        for source_id in expired:
            self._numeric_reaction_states.pop(source_id, None)
        overflow = len(self._numeric_reaction_states) - _REACTION_STATE_MAX_ENTRIES
        if overflow > 0:
            oldest = sorted(
                self._numeric_reaction_states.items(),
                key=lambda item: float(item[1].get("updated_at") or 0.0),
            )[:overflow]
            for source_id, _ in oldest:
                self._numeric_reaction_states.pop(source_id, None)

    async def _resolve_room_source_id(
        self,
        message_id: int | str | None,
        runtime: PluginExecutionContext,
    ) -> str | None:
        if message_id is None:
            return None
        entry = await runtime.messages.get_by_surrogate(message_id)
        if not isinstance(entry, dict):
            return None
        room_source_id = str(entry.get("room_source_id") or "").strip()
        return room_source_id or None

    async def _apply_typing_transition(
        self,
        runtime: PluginExecutionContext,
        *,
        source_message_id: str,
        room_source_id: str,
        transition: str,
    ) -> bool:
        if transition == "start":
            return await self._start_room_typing(
                runtime,
                source_message_id=source_message_id,
                room_source_id=room_source_id,
            )
        if transition == "stop":
            return await self._stop_room_typing(
                runtime,
                source_message_id=source_message_id,
                room_source_id=room_source_id,
            )
        return True

    async def _start_room_typing(
        self,
        runtime: PluginExecutionContext,
        *,
        source_message_id: str,
        room_source_id: str,
    ) -> bool:
        members = self._typing_room_members.setdefault(room_source_id, set())
        if source_message_id in members:
            return True

        members.add(source_message_id)
        task = self._typing_room_tasks.get(room_source_id)
        if task is not None and not task.done():
            return True

        ok = await runtime.rocketchat.set_room_typing(room_source_id, is_typing=True)
        if not ok:
            members.discard(source_message_id)
            if not members:
                self._typing_room_members.pop(room_source_id, None)
            return False

        task = asyncio.create_task(
            self._typing_heartbeat_loop(runtime, room_source_id),
            name=f"RocketCatIAmThinkingTyping:{room_source_id}",
        )
        self._typing_room_tasks[room_source_id] = task
        task.add_done_callback(lambda finished, room_id=room_source_id: self._on_typing_task_done(room_id, finished))
        return True

    async def _stop_room_typing(
        self,
        runtime: PluginExecutionContext,
        *,
        source_message_id: str,
        room_source_id: str,
    ) -> bool:
        members = self._typing_room_members.get(room_source_id)
        if not members or source_message_id not in members:
            return True

        members.discard(source_message_id)
        if members:
            return True

        self._typing_room_members.pop(room_source_id, None)
        task = self._typing_room_tasks.pop(room_source_id, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return await runtime.rocketchat.set_room_typing(room_source_id, is_typing=False)

    async def _stop_all_typing(self, runtime: PluginExecutionContext) -> None:
        active_rooms = list(self._typing_room_members.keys())
        self._typing_room_members.clear()

        tasks = list(self._typing_room_tasks.values())
        self._typing_room_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        for room_source_id in active_rooms:
            ok = await runtime.rocketchat.set_room_typing(room_source_id, is_typing=False)
            if not ok:
                logger.warning(
                    "[RocketCatShell][Plugin:%s] typing 停止通知失败: room_id=%s",
                    self.context.plugin_id,
                    room_source_id,
                )

    async def _typing_heartbeat_loop(
        self,
        runtime: PluginExecutionContext,
        room_source_id: str,
    ) -> None:
        started_at = time.monotonic()
        try:
            while self._typing_room_members.get(room_source_id):
                await asyncio.sleep(_TYPING_RENEW_INTERVAL_SECONDS)
                if not self._typing_room_members.get(room_source_id):
                    break
                if time.monotonic() - started_at >= _TYPING_MAX_DURATION_SECONDS:
                    self._typing_room_members.pop(room_source_id, None)
                    await runtime.rocketchat.set_room_typing(
                        room_source_id,
                        is_typing=False,
                    )
                    logger.warning(
                        "[RocketCatShell][Plugin:%s] typing 已达到最长持续时间并自动停止: room_id=%s",
                        self.context.plugin_id,
                        room_source_id,
                    )
                    break
                ok = await runtime.rocketchat.set_room_typing(room_source_id, is_typing=True)
                if not ok:
                    logger.warning(
                        "[RocketCatShell][Plugin:%s] typing 心跳续期失败: room_id=%s",
                        self.context.plugin_id,
                        room_source_id,
                    )
        except asyncio.CancelledError:
            raise

    def _on_typing_task_done(self, room_source_id: str, task: asyncio.Task[None]) -> None:
        current = self._typing_room_tasks.get(room_source_id)
        if current is task:
            self._typing_room_tasks.pop(room_source_id, None)

    def _should_fail_action(
        self,
        reaction_applied: bool | None,
        typing_applied: bool | None,
        typing_transition: str | None,
    ) -> bool:
        requested: list[bool | None] = []
        if self._reactions_enabled():
            requested.append(reaction_applied)
        if self._typing_indicator_enabled() and typing_transition:
            requested.append(typing_applied)
        return bool(requested) and all(result is False for result in requested)

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

    def _reactions_enabled(self) -> bool:
        return self._coerce_bool(
            self.config.get("enable_reactions", _DEFAULT_ENABLE_REACTIONS),
        )

    def _typing_indicator_enabled(self) -> bool:
        return self._coerce_bool(
            self.config.get("enable_typing_indicator", _DEFAULT_ENABLE_TYPING_INDICATOR),
        )

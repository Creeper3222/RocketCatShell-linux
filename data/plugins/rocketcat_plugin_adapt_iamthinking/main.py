from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from rocketcat_shell.logger import logger
from rocketcat_shell.plugin_system.base import PluginExecutionContext, RocketCatPlugin


_STATE_THINKING = "thinking"
_STATE_USING_TOOL = "using_tool"
_STATE_ERROR = "error"
_STATE_DONE = "done"
_PROCESSING_STATES = frozenset({_STATE_THINKING, _STATE_USING_TOOL})
_TERMINAL_STATES = frozenset({_STATE_ERROR, _STATE_DONE})
_STATE_EMOJI_DEFAULTS: tuple[tuple[str, str, tuple[int, ...]], ...] = (
    (_STATE_THINKING, "thinking_emoji_ids", (66,)),
    (_STATE_USING_TOOL, "using_tool_emoji_ids", (270,)),
    (_STATE_ERROR, "error_emoji_ids", (264,)),
    (_STATE_DONE, "done_emoji_ids", (74,)),
)
_DEFAULT_THINKING_REACTION = ":heart:"
_DEFAULT_USING_TOOL_REACTION = ":tools:"
_DEFAULT_ERROR_REACTION = ":octagonal_sign:"
_DEFAULT_DONE_REACTION = ":sunny:"
_DEFAULT_ENABLE_REACTIONS = True
_DEFAULT_ENABLE_TYPING_INDICATOR = True
_TYPING_RENEW_INTERVAL_SECONDS = 4.0
_TYPING_STOP_GRACE_SECONDS = 1.0
_REACTION_STATE_TTL_SECONDS = 3600.0
_REACTION_STATE_MAX_ENTRIES = 2048
_TYPING_MAX_DURATION_SECONDS = 600.0
_UNKNOWN_EMOJI_WARNING_MAX_ENTRIES = 256
_ROCKETCHAT_SHORTCODE_PATTERN = re.compile(r"^:[A-Za-z0-9_+.-]+:$")


class _ResolvedActionEffects:
    __slots__ = (
        "valid",
        "state_name",
        "reaction",
        "reaction_transition",
        "typing_transition",
    )

    def __init__(
        self,
        *,
        valid: bool = True,
        state_name: str | None = None,
        reaction: str = "",
        reaction_transition: bool | None = None,
        typing_transition: str | None = None,
    ):
        self.valid = valid
        self.state_name = state_name
        self.reaction = reaction
        self.reaction_transition = reaction_transition
        self.typing_transition = typing_transition


class Plugin(RocketCatPlugin):
    handled_actions = frozenset({"set_msg_emoji_like"})

    def __init__(self, context, config: dict[str, Any]):
        super().__init__(context, config)
        self._state_emoji_ids, self._emoji_state_by_id = self._build_emoji_state_map()
        self._numeric_reaction_states: dict[tuple[str, str], dict[str, Any]] = {}
        self._unknown_emoji_warnings: set[int] = set()
        self._typing_room_members: dict[tuple[str, str], set[str]] = {}
        self._typing_room_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._typing_stop_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def on_load(self, runtime: PluginExecutionContext) -> None:
        logger.info(
            "[RocketCatShell][Plugin:%s] 已加载到运行时 %s。",
            self.context.plugin_id,
            runtime.instance_name,
        )

    async def on_unload(self, runtime: PluginExecutionContext) -> None:
        await self._cancel_runtime_typing_stops(runtime)
        await self._stop_all_typing(runtime)
        runtime_scope = self._runtime_scope(runtime)
        self._numeric_reaction_states = {
            key: state
            for key, state in self._numeric_reaction_states.items()
            if key[0] != runtime_scope
        }
        logger.info(
            "[RocketCatShell][Plugin:%s] 已从运行时 %s 卸载。",
            self.context.plugin_id,
            runtime.instance_name,
        )

    async def on_terminate(self) -> None:
        tasks = [
            *self._typing_room_tasks.values(),
            *self._typing_stop_tasks.values(),
        ]
        self._typing_room_tasks.clear()
        self._typing_stop_tasks.clear()
        self._typing_room_members.clear()
        self._numeric_reaction_states.clear()
        self._unknown_emoji_warnings.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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
            runtime_scope=self._runtime_scope(runtime),
        )
        if not effects.valid:
            return runtime.failed(f"未映射的 emoji_id: {params.get('emoji_id')}", retcode=1404)

        reaction_applied: bool | None = None
        reaction_requested = bool(
            self._reactions_enabled()
            and effects.reaction
            and effects.reaction_transition is not None
        )
        if reaction_requested:
            reaction_applied = await runtime.rocketchat.set_message_reaction(
                source_message_id,
                effects.reaction,
                should_react=bool(effects.reaction_transition),
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

        if self._should_fail_action(
            reaction_applied,
            typing_applied,
            reaction_requested=reaction_requested,
            typing_transition=effects.typing_transition,
        ):
            reasons: list[str] = []
            if reaction_applied is False:
                reasons.append("Rocket.Chat 贴表情失败")
            if typing_applied is False:
                reasons.append("Rocket.Chat typing 指示器更新失败")
            return runtime.failed("；".join(reasons) or "Rocket.Chat 适配失败", retcode=1500)

        return runtime.ok(
            {
                "message_id": params.get("message_id"),
                "state": effects.state_name,
                "reaction": effects.reaction,
                "set": effects.reaction_transition,
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
        runtime_scope: str,
    ) -> _ResolvedActionEffects:
        if isinstance(emoji_id, bool):
            return _ResolvedActionEffects(valid=False)
        if isinstance(emoji_id, str):
            normalized = emoji_id.strip()
            if _ROCKETCHAT_SHORTCODE_PATTERN.fullmatch(normalized):
                return _ResolvedActionEffects(
                    reaction=normalized,
                    reaction_transition=should_react,
                )
            if not normalized or not normalized.lstrip("-").isdigit():
                return _ResolvedActionEffects(valid=False)
            emoji_id = int(normalized)
        elif not isinstance(emoji_id, int):
            return _ResolvedActionEffects(valid=False)

        return self._resolve_numeric_action_effects(
            int(emoji_id),
            source_message_id=source_message_id,
            should_react=should_react,
            runtime_scope=runtime_scope,
        )

    def _resolve_numeric_action_effects(
        self,
        emoji_id: int,
        *,
        source_message_id: str,
        should_react: bool,
        runtime_scope: str,
    ) -> _ResolvedActionEffects:
        state_name = self._emoji_state_by_id.get(emoji_id)
        if state_name is None:
            self._warn_unknown_emoji_id(emoji_id)
            return _ResolvedActionEffects(valid=False)

        self._prune_reaction_states()
        state_key = (runtime_scope, source_message_id)
        state = self._numeric_reaction_states.setdefault(
            state_key,
            {
                "active_ids": {
                    semantic_state: set()
                    for semantic_state, _, _ in _STATE_EMOJI_DEFAULTS
                },
                "updated_at": time.monotonic(),
            },
        )
        state["updated_at"] = time.monotonic()
        active_by_state = state["active_ids"]
        active_ids: set[int] = active_by_state[state_name]
        was_state_active = bool(active_ids)
        was_present = emoji_id in active_ids
        reaction_transition: bool | None = None
        typing_transition: str | None = None

        if should_react:
            active_ids.add(emoji_id)
            if not was_state_active:
                reaction_transition = True
            if state_name in _PROCESSING_STATES:
                typing_transition = "start"
            elif state_name in _TERMINAL_STATES:
                typing_transition = "stop"
        else:
            active_ids.discard(emoji_id)
            if was_present and not active_ids:
                reaction_transition = False
            if (
                was_present
                and state_name in _PROCESSING_STATES
                and not any(active_by_state[item] for item in _PROCESSING_STATES)
            ):
                typing_transition = "stop_delayed"
            if state_name in _TERMINAL_STATES:
                typing_transition = "stop"

        return _ResolvedActionEffects(
            state_name=state_name,
            reaction=self._reaction_for_state(state_name),
            reaction_transition=reaction_transition,
            typing_transition=typing_transition,
        )

    def _build_emoji_state_map(
        self,
    ) -> tuple[dict[str, frozenset[int]], dict[int, str]]:
        state_ids: dict[str, frozenset[int]] = {}
        state_by_id: dict[int, str] = {}
        conflicts: list[str] = []
        for state_name, config_key, defaults in _STATE_EMOJI_DEFAULTS:
            ids = frozenset(
                self._coerce_emoji_id_list(
                    self.config.get(config_key, list(defaults)),
                    field_name=config_key,
                )
            )
            state_ids[state_name] = ids
            for emoji_id in ids:
                previous = state_by_id.get(emoji_id)
                if previous is not None and previous != state_name:
                    conflicts.append(f"{emoji_id} ({previous}/{state_name})")
                else:
                    state_by_id[emoji_id] = state_name
        if conflicts:
            raise ValueError(
                "I Am Thinking 四态表情 ID 不能交叉重复：" + "，".join(conflicts)
            )
        return state_ids, state_by_id

    def _coerce_emoji_id_list(self, value: Any, *, field_name: str) -> tuple[int, ...]:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError(f"{field_name} 必须是整数列表")
        normalized: set[int] = set()
        for item in value:
            if isinstance(item, bool):
                raise ValueError(f"{field_name} 只能包含整数表情 ID")
            if isinstance(item, int):
                emoji_id = item
            elif isinstance(item, str) and item.strip().lstrip("-").isdigit():
                emoji_id = int(item.strip())
            else:
                raise ValueError(f"{field_name} 只能包含整数表情 ID")
            if emoji_id < 0:
                raise ValueError(f"{field_name} 不能包含负数表情 ID")
            normalized.add(emoji_id)
        return tuple(sorted(normalized))

    def _reaction_for_state(self, state_name: str) -> str:
        if state_name == _STATE_THINKING:
            return self._thinking_reaction()
        if state_name == _STATE_USING_TOOL:
            return self._using_tool_reaction()
        if state_name == _STATE_ERROR:
            return self._error_reaction()
        if state_name == _STATE_DONE:
            return self._done_reaction()
        return ""

    def _warn_unknown_emoji_id(self, emoji_id: int) -> None:
        if emoji_id in self._unknown_emoji_warnings:
            return
        if len(self._unknown_emoji_warnings) >= _UNKNOWN_EMOJI_WARNING_MAX_ENTRIES:
            return
        self._unknown_emoji_warnings.add(emoji_id)
        logger.warning(
            "[RocketCatShell][Plugin:%s] 未映射 I Am Thinking emoji_id=%s；"
            "请同步 thinking/using_tool/error/done 四组上游 ID 配置。",
            self.context.plugin_id,
            emoji_id,
        )

    def _prune_reaction_states(self) -> None:
        cutoff = time.monotonic() - _REACTION_STATE_TTL_SECONDS
        expired = [
            state_key
            for state_key, state in self._numeric_reaction_states.items()
            if float(state.get("updated_at") or 0.0) < cutoff
        ]
        for state_key in expired:
            self._numeric_reaction_states.pop(state_key, None)
            task = self._typing_stop_tasks.pop(state_key, None)
            if task is not None and not task.done():
                task.cancel()
        overflow = len(self._numeric_reaction_states) - _REACTION_STATE_MAX_ENTRIES
        if overflow > 0:
            oldest = sorted(
                self._numeric_reaction_states.items(),
                key=lambda item: float(item[1].get("updated_at") or 0.0),
            )[:overflow]
            for state_key, _ in oldest:
                self._numeric_reaction_states.pop(state_key, None)
                task = self._typing_stop_tasks.pop(state_key, None)
                if task is not None and not task.done():
                    task.cancel()

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
        state_key = (self._runtime_scope(runtime), source_message_id)
        if transition == "start":
            await self._cancel_delayed_typing_stop(state_key)
            return await self._start_room_typing(
                runtime,
                source_message_id=source_message_id,
                room_source_id=room_source_id,
            )
        if transition == "stop":
            await self._cancel_delayed_typing_stop(state_key)
            return await self._stop_room_typing(
                runtime,
                source_message_id=source_message_id,
                room_source_id=room_source_id,
            )
        if transition == "stop_delayed":
            return self._schedule_delayed_typing_stop(
                runtime,
                state_key=state_key,
                source_message_id=source_message_id,
                room_source_id=room_source_id,
            )
        return True

    def _schedule_delayed_typing_stop(
        self,
        runtime: PluginExecutionContext,
        *,
        state_key: tuple[str, str],
        source_message_id: str,
        room_source_id: str,
    ) -> bool:
        current = self._typing_stop_tasks.get(state_key)
        if current is not None and not current.done():
            return True
        task = asyncio.create_task(
            self._delayed_typing_stop(
                runtime,
                source_message_id=source_message_id,
                room_source_id=room_source_id,
            ),
            name=f"RocketCatIAmThinkingStop:{state_key[0]}:{source_message_id}",
        )
        self._typing_stop_tasks[state_key] = task
        task.add_done_callback(
            lambda finished, key=state_key: self._on_typing_stop_task_done(key, finished)
        )
        return True

    async def _delayed_typing_stop(
        self,
        runtime: PluginExecutionContext,
        *,
        source_message_id: str,
        room_source_id: str,
    ) -> None:
        try:
            await asyncio.sleep(_TYPING_STOP_GRACE_SECONDS)
            ok = await self._stop_room_typing(
                runtime,
                source_message_id=source_message_id,
                room_source_id=room_source_id,
            )
            if not ok:
                logger.warning(
                    "[RocketCatShell][Plugin:%s] 延迟停止 typing 失败: room_id=%s",
                    self.context.plugin_id,
                    room_source_id,
                )
        except asyncio.CancelledError:
            raise

    async def _cancel_delayed_typing_stop(self, state_key: tuple[str, str]) -> None:
        task = self._typing_stop_tasks.pop(state_key, None)
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _cancel_runtime_typing_stops(
        self,
        runtime: PluginExecutionContext,
    ) -> None:
        runtime_scope = self._runtime_scope(runtime)
        keys = [key for key in self._typing_stop_tasks if key[0] == runtime_scope]
        tasks = [self._typing_stop_tasks.pop(key) for key in keys]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _on_typing_stop_task_done(
        self,
        state_key: tuple[str, str],
        task: asyncio.Task[None],
    ) -> None:
        current = self._typing_stop_tasks.get(state_key)
        if current is task:
            self._typing_stop_tasks.pop(state_key, None)

    async def _start_room_typing(
        self,
        runtime: PluginExecutionContext,
        *,
        source_message_id: str,
        room_source_id: str,
    ) -> bool:
        room_key = (self._runtime_scope(runtime), room_source_id)
        members = self._typing_room_members.setdefault(room_key, set())
        if source_message_id in members:
            return True

        members.add(source_message_id)
        task = self._typing_room_tasks.get(room_key)
        if task is not None and not task.done():
            return True

        ok = await runtime.rocketchat.set_room_typing(room_source_id, is_typing=True)
        if not ok:
            members.discard(source_message_id)
            if not members:
                self._typing_room_members.pop(room_key, None)
            return False

        task = asyncio.create_task(
            self._typing_heartbeat_loop(runtime, room_key),
            name=f"RocketCatIAmThinkingTyping:{room_key[0]}:{room_source_id}",
        )
        self._typing_room_tasks[room_key] = task
        task.add_done_callback(
            lambda finished, key=room_key: self._on_typing_task_done(key, finished)
        )
        return True

    async def _stop_room_typing(
        self,
        runtime: PluginExecutionContext,
        *,
        source_message_id: str,
        room_source_id: str,
    ) -> bool:
        room_key = (self._runtime_scope(runtime), room_source_id)
        members = self._typing_room_members.get(room_key)
        if not members or source_message_id not in members:
            return True

        members.discard(source_message_id)
        if members:
            return True

        self._typing_room_members.pop(room_key, None)
        task = self._typing_room_tasks.pop(room_key, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return await runtime.rocketchat.set_room_typing(room_source_id, is_typing=False)

    async def _stop_all_typing(self, runtime: PluginExecutionContext) -> None:
        runtime_scope = self._runtime_scope(runtime)
        active_room_keys = [
            key for key in self._typing_room_members if key[0] == runtime_scope
        ]
        for key in active_room_keys:
            self._typing_room_members.pop(key, None)

        tasks = [
            self._typing_room_tasks.pop(key)
            for key in active_room_keys
            if key in self._typing_room_tasks
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        for _, room_source_id in active_room_keys:
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
        room_key: tuple[str, str],
    ) -> None:
        room_source_id = room_key[1]
        started_at = time.monotonic()
        try:
            while self._typing_room_members.get(room_key):
                await asyncio.sleep(_TYPING_RENEW_INTERVAL_SECONDS)
                if not self._typing_room_members.get(room_key):
                    break
                if time.monotonic() - started_at >= _TYPING_MAX_DURATION_SECONDS:
                    self._typing_room_members.pop(room_key, None)
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

    def _on_typing_task_done(
        self,
        room_key: tuple[str, str],
        task: asyncio.Task[None],
    ) -> None:
        current = self._typing_room_tasks.get(room_key)
        if current is task:
            self._typing_room_tasks.pop(room_key, None)

    def _runtime_scope(self, runtime: PluginExecutionContext) -> str:
        explicit = str(getattr(runtime, "runtime_key", "") or "").strip()
        if explicit:
            return explicit
        bridge_config = getattr(runtime, "bridge_config", None)
        bot_id = str(getattr(bridge_config, "bot_id", "") or "").strip()
        return bot_id or str(getattr(runtime, "instance_name", "") or "").strip() or f"runtime-{id(runtime)}"

    def _should_fail_action(
        self,
        reaction_applied: bool | None,
        typing_applied: bool | None,
        *,
        reaction_requested: bool,
        typing_transition: str | None,
    ) -> bool:
        requested: list[bool | None] = []
        if reaction_requested:
            requested.append(reaction_applied)
        if self._typing_indicator_enabled() and typing_transition:
            requested.append(typing_applied)
        return bool(requested) and all(result is False for result in requested)

    def _thinking_reaction(self) -> str:
        return self._coerce_shortcode(
            self.config.get("llm_thinking_reaction", _DEFAULT_THINKING_REACTION),
            _DEFAULT_THINKING_REACTION,
        )

    def _using_tool_reaction(self) -> str:
        return self._coerce_shortcode(
            self.config.get("llm_using_tool_reaction", _DEFAULT_USING_TOOL_REACTION),
            _DEFAULT_USING_TOOL_REACTION,
        )

    def _error_reaction(self) -> str:
        return self._coerce_shortcode(
            self.config.get("llm_error_reaction", _DEFAULT_ERROR_REACTION),
            _DEFAULT_ERROR_REACTION,
        )

    def _done_reaction(self) -> str:
        return self._coerce_shortcode(
            self.config.get("llm_done_reaction", _DEFAULT_DONE_REACTION),
            _DEFAULT_DONE_REACTION,
        )

    def _coerce_shortcode(self, value: Any, fallback: str) -> str:
        normalized = str(value or "").strip()
        if _ROCKETCHAT_SHORTCODE_PATTERN.fullmatch(normalized):
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

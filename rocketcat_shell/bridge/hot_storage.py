from __future__ import annotations

import pickle
import queue
import struct
import threading
import time
from collections import deque
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from rocketcat_shell.logger import logger

from .id_map import DurableIdMap, IdMapping, MessageWindowSnapshot
from .storage import ContextRoomStore, MessageStore, PrivateRoomStore


@dataclass(slots=True)
class RuntimeHotStoreBundle:
    state_engine: "RuntimeStateEngine"
    writer: "JournalPersistenceWorker"
    id_map: DurableIdMap
    message_store: MessageStore
    private_room_store: PrivateRoomStore
    context_room_store: ContextRoomStore

    def close(self) -> None:
        self.writer.close()

    def flush(self) -> None:
        self.writer.flush()


@dataclass(slots=True)
class _FlushCommand:
    done: threading.Event


@dataclass(slots=True)
class _StopCommand:
    done: threading.Event


class RuntimeStateMutationBatch:
    def __init__(self, state_engine: "RuntimeStateEngine"):
        self._state_engine = state_engine
        self._mutations: list[dict[str, Any]] = []
        self._committed = False

    def has_pending(self) -> bool:
        return bool(self._mutations) and not self._committed

    def mutation_count(self) -> int:
        return len(self._mutations)

    def get_or_create_mapping(self, namespace: str, source_id: str) -> IdMapping:
        source_key = str(source_id)
        with self._state_engine._lock:
            mapping, _snapshot, created = self._state_engine._allocate_mapping_locked(namespace, source_key)
        if created:
            self._mutations.append(
                {
                    "op": "id_put",
                    "namespace": namespace,
                    "source_id": source_key,
                    "surrogate_id": mapping.surrogate_id,
                }
            )
        return mapping

    def bind_private_room(self, user_source_id: str, user_surrogate_id: int, room_source_id: str) -> None:
        normalized_user_source_id = str(user_source_id)
        normalized_user_surrogate_id = int(user_surrogate_id)
        normalized_room_source_id = str(room_source_id)
        self._state_engine._bind_private_room_without_record(
            normalized_user_source_id,
            normalized_user_surrogate_id,
            normalized_room_source_id,
        )
        self._mutations.append(
            {
                "op": "private_bind",
                "user_source_id": normalized_user_source_id,
                "user_surrogate_id": normalized_user_surrogate_id,
                "room_source_id": normalized_room_source_id,
            }
        )

    def bind_context_room(
        self,
        context_source_id: str,
        context_surrogate_id: int | str,
        room_source_id: str,
        room_surrogate_id: int | str,
        room_name: str,
        room_slug: str,
        thread_source_id: str = "",
        timestamp: int | None = None,
    ) -> None:
        normalized_entry = {
            "context_source_id": str(context_source_id),
            "context_surrogate_id": int(context_surrogate_id),
            "room_source_id": str(room_source_id),
            "room_surrogate_id": int(room_surrogate_id),
            "room_name": str(room_name),
            "room_slug": str(room_slug),
            "thread_source_id": str(thread_source_id or "").strip(),
        }
        if timestamp is not None:
            normalized_entry["timestamp"] = int(timestamp)
        if not self._state_engine._bind_context_room_without_record(normalized_entry):
            return
        self._mutations.append({"op": "context_bind", "entry": normalized_entry})

    def put_message(self, entry: dict[str, Any]) -> None:
        self._state_engine._put_message_without_record(entry)
        self._mutations.append({"op": "message_put", "entry": entry})

    def commit(self) -> None:
        if self._committed:
            return
        if self._mutations:
            self._state_engine._record_batch(self._mutations)
        self._committed = True


class JournalPersistenceWorker:
    _SNAPSHOT_VERSION = 1

    def __init__(
        self,
        *,
        snapshot_path: Path,
        journal_path: Path,
        snapshot_provider: Callable[[], dict[str, Any]],
        flush_every_records: int = 32,
        snapshot_every_records: int = 512,
        idle_flush_interval: float = 0.5,
    ):
        self.snapshot_path = snapshot_path
        self.journal_path = journal_path
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._snapshot_provider = snapshot_provider
        self._flush_every_records = max(1, int(flush_every_records))
        self._snapshot_every_records = max(self._flush_every_records, int(snapshot_every_records))
        self._idle_flush_interval = max(0.1, float(idle_flush_interval))
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name=f"RocketCatStateWriter:{self.journal_path.parent.name}",
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, record: dict[str, Any]) -> None:
        self._queue.put(record)

    def enqueue_batch(self, mutations: list[dict[str, Any]]) -> None:
        self._queue.put({"op": "batch", "mutations": mutations})

    def flush(self) -> None:
        done = threading.Event()
        self._queue.put(_FlushCommand(done=done))
        done.wait()

    def close(self) -> None:
        done = threading.Event()
        self._queue.put(_StopCommand(done=done))
        done.wait()
        self._thread.join(timeout=5.0)

    @classmethod
    def load_snapshot_payload(cls, snapshot_path: Path) -> dict[str, Any] | None:
        if not snapshot_path.exists():
            return None

        try:
            snapshot_payload = pickle.loads(snapshot_path.read_bytes())
        except Exception as exc:
            logger.warning(
                "[RocketCatShell] failed to decode runtime snapshot | path=%s | error=%s",
                snapshot_path,
                exc,
            )
            return None

        if not isinstance(snapshot_payload, dict):
            logger.warning(
                "[RocketCatShell] ignored runtime snapshot with invalid payload type | path=%s | type=%s",
                snapshot_path,
                type(snapshot_payload).__name__,
            )
            return None

        snapshot_version = snapshot_payload.get("version")
        if snapshot_version not in (None, cls._SNAPSHOT_VERSION):
            logger.warning(
                "[RocketCatShell] ignored runtime snapshot with unsupported version | path=%s | version=%s | expected=%s",
                snapshot_path,
                snapshot_version,
                cls._SNAPSHOT_VERSION,
            )
            return None

        if snapshot_version is not None and not isinstance(snapshot_payload.get("state"), dict):
            logger.warning(
                "[RocketCatShell] ignored runtime snapshot with invalid state payload | path=%s",
                snapshot_path,
            )
            return None

        return snapshot_payload

    def _run(self) -> None:
        pending_since_flush = 0
        records_since_snapshot = 0
        journal_handle = self.journal_path.open("ab")
        try:
            while True:
                try:
                    item = self._queue.get(timeout=self._idle_flush_interval)
                except queue.Empty:
                    if pending_since_flush:
                        self._flush_handle(journal_handle)
                        pending_since_flush = 0
                    if records_since_snapshot >= self._snapshot_every_records:
                        journal_handle = self._write_snapshot_and_rotate(journal_handle)
                        records_since_snapshot = 0
                    continue

                if isinstance(item, _FlushCommand):
                    if pending_since_flush:
                        self._flush_handle(journal_handle)
                        pending_since_flush = 0
                    if records_since_snapshot >= self._snapshot_every_records:
                        journal_handle = self._write_snapshot_and_rotate(journal_handle)
                        records_since_snapshot = 0
                    item.done.set()
                    continue

                if isinstance(item, _StopCommand):
                    if pending_since_flush:
                        self._flush_handle(journal_handle)
                        pending_since_flush = 0
                    journal_handle = self._write_snapshot_and_rotate(journal_handle)
                    item.done.set()
                    break

                self._write_record(journal_handle, item)
                pending_since_flush += 1
                records_since_snapshot += 1
                if pending_since_flush >= self._flush_every_records:
                    self._flush_handle(journal_handle)
                    pending_since_flush = 0
                if records_since_snapshot >= self._snapshot_every_records:
                    journal_handle = self._write_snapshot_and_rotate(journal_handle)
                    records_since_snapshot = 0
        finally:
            journal_handle.close()

    def _write_snapshot_and_rotate(self, journal_handle):
        snapshot_payload = {
            "version": self._SNAPSHOT_VERSION,
            "state": self._snapshot_provider(),
        }
        tmp_path = self.snapshot_path.with_suffix(self.snapshot_path.suffix + ".tmp")
        tmp_path.write_bytes(pickle.dumps(snapshot_payload, protocol=pickle.HIGHEST_PROTOCOL))
        tmp_path.replace(self.snapshot_path)
        journal_handle.close()
        self.journal_path.write_bytes(b"")
        return self.journal_path.open("ab")

    @staticmethod
    def _flush_handle(journal_handle) -> None:
        journal_handle.flush()

    @staticmethod
    def _write_record(journal_handle, record: dict[str, Any]) -> None:
        payload = pickle.dumps(record, protocol=pickle.HIGHEST_PROTOCOL)
        journal_handle.write(struct.pack("<I", len(payload)))
        journal_handle.write(payload)

    @staticmethod
    def iter_records(journal_path: Path):
        if not journal_path.exists():
            return
        with journal_path.open("rb") as handle:
            record_index = 0
            while True:
                header = handle.read(4)
                if not header:
                    break
                if len(header) < 4:
                    logger.warning(
                        "[RocketCatShell] detected truncated runtime journal header | path=%s | record_index=%s",
                        journal_path,
                        record_index,
                    )
                    break
                (payload_size,) = struct.unpack("<I", header)
                if payload_size <= 0:
                    logger.warning(
                        "[RocketCatShell] detected invalid runtime journal payload size | path=%s | record_index=%s | payload_size=%s",
                        journal_path,
                        record_index,
                        payload_size,
                    )
                    break
                payload = handle.read(payload_size)
                if len(payload) < payload_size:
                    logger.warning(
                        "[RocketCatShell] detected truncated runtime journal payload | path=%s | record_index=%s | expected=%s | actual=%s",
                        journal_path,
                        record_index,
                        payload_size,
                        len(payload),
                    )
                    break
                try:
                    record = pickle.loads(payload)
                except Exception as exc:
                    logger.warning(
                        "[RocketCatShell] failed to decode runtime journal record | path=%s | record_index=%s | error=%s",
                        journal_path,
                        record_index,
                        exc,
                    )
                    break
                if not isinstance(record, dict):
                    logger.warning(
                        "[RocketCatShell] ignored runtime journal record with invalid payload type | path=%s | record_index=%s | type=%s",
                        journal_path,
                        record_index,
                        type(record).__name__,
                    )
                    break
                record_index += 1
                yield record


class RuntimeStateEngine:
    def __init__(self, *, message_window_size: int):
        self._lock = threading.RLock()
        self._writer: JournalPersistenceWorker | None = None
        self._message_window_size = DurableIdMap.normalize_message_window_size(message_window_size)
        self._counters = {namespace: 0 for namespace in DurableIdMap._BASES}
        self._forward = {namespace: {} for namespace in DurableIdMap._BASES}
        self._reverse = {namespace: {} for namespace in DurableIdMap._BASES}
        self._message_order: deque[str] = deque()
        self._messages_by_source: dict[str, dict[str, Any]] = {}
        self._messages_by_surrogate: dict[str, dict[str, Any]] = {}
        self._latest_by_context_sender: dict[tuple[str, str], dict[str, Any]] = {}
        self._context_sender_message_order: dict[tuple[str, str], deque[str]] = {}
        self._private_room_by_user_source: dict[str, str] = {}
        self._private_room_by_user_surrogate: dict[str, str] = {}
        self._context_room_by_source: dict[str, dict[str, Any]] = {}
        self._context_room_by_surrogate: dict[str, dict[str, Any]] = {}
        self._legacy_user_state_detected = False

    def bind_writer(self, writer: JournalPersistenceWorker) -> None:
        self._writer = writer

    def begin_batch(self) -> RuntimeStateMutationBatch:
        return RuntimeStateMutationBatch(self)

    def export_snapshot_payload(self) -> dict[str, Any]:
        with self._lock:
            messages_by_source: dict[str, dict[str, Any]] = {}
            messages_by_surrogate: dict[str, dict[str, Any]] = {}
            for source_id, entry in self._messages_by_source.items():
                if not isinstance(entry, dict):
                    continue
                cloned_entry = self._clone_message_entry(entry)
                messages_by_source[str(source_id)] = cloned_entry
                surrogate_id = str(cloned_entry.get("surrogate_id") or "").strip()
                if surrogate_id:
                    messages_by_surrogate[surrogate_id] = cloned_entry

            context_room_by_source: dict[str, dict[str, Any]] = {}
            context_room_by_surrogate: dict[str, dict[str, Any]] = {}
            for context_source_id, entry in self._context_room_by_source.items():
                if not isinstance(entry, dict):
                    continue
                cloned_entry = deepcopy(entry)
                context_room_by_source[str(context_source_id)] = cloned_entry
                context_surrogate_id = str(cloned_entry.get("context_surrogate_id") or "").strip()
                if context_surrogate_id:
                    context_room_by_surrogate[context_surrogate_id] = cloned_entry

            return {
                "message_window_size": self._message_window_size,
                "counters": deepcopy(self._counters),
                "forward": deepcopy(self._forward),
                "reverse": deepcopy(self._reverse),
                "message_order": list(self._message_order),
                "messages_by_source": messages_by_source,
                "messages_by_surrogate": messages_by_surrogate,
                "latest_by_context_sender": deepcopy(self._latest_by_context_sender),
                "context_sender_message_order": {
                    key: list(value)
                    for key, value in self._context_sender_message_order.items()
                },
                "private_room_by_user_source": deepcopy(self._private_room_by_user_source),
                "private_room_by_user_surrogate": deepcopy(self._private_room_by_user_surrogate),
                "context_room_by_source": context_room_by_source,
                "context_room_by_surrogate": context_room_by_surrogate,
            }

    def load_snapshot_payload(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        state = payload.get("state") if isinstance(payload.get("state"), dict) else payload
        if not isinstance(state, dict):
            return

        with self._lock:
            legacy_forward = (state.get("forward") or {}).get("user", {}) or {}
            legacy_reverse = (state.get("reverse") or {}).get("user", {}) or {}
            legacy_counter = int((state.get("counters") or {}).get("user") or 0)
            if legacy_forward or legacy_reverse or legacy_counter:
                self._legacy_user_state_detected = True
            self._message_window_size = DurableIdMap.normalize_message_window_size(
                state.get("message_window_size") or self._message_window_size
            )
            self._counters = {
                namespace: int((state.get("counters") or {}).get(namespace) or 0)
                for namespace in DurableIdMap._BASES
            }
            self._forward = {
                namespace: {
                    str(source_id): int(surrogate_id)
                    for source_id, surrogate_id in ((state.get("forward") or {}).get(namespace, {}) or {}).items()
                }
                for namespace in DurableIdMap._BASES
            }
            self._reverse = {
                namespace: {
                    str(surrogate_id): str(source_id)
                    for surrogate_id, source_id in ((state.get("reverse") or {}).get(namespace, {}) or {}).items()
                }
                for namespace in DurableIdMap._BASES
            }
            self._message_order = deque(str(source_id) for source_id in state.get("message_order", []))
            self._messages_by_source = {
                str(source_id): deepcopy(entry)
                for source_id, entry in (state.get("messages_by_source") or {}).items()
                if isinstance(entry, dict)
            }
            self._messages_by_surrogate = {}
            for entry in self._messages_by_source.values():
                surrogate_id = str(entry.get("surrogate_id") or "").strip()
                if surrogate_id:
                    self._messages_by_surrogate[surrogate_id] = entry
            if not self._messages_by_surrogate:
                self._messages_by_surrogate = {
                    str(surrogate_id): deepcopy(entry)
                    for surrogate_id, entry in (state.get("messages_by_surrogate") or {}).items()
                    if isinstance(entry, dict)
                }
            self._latest_by_context_sender = {
                self._normalize_pair_key(pair_key): deepcopy(entry)
                for pair_key, entry in (state.get("latest_by_context_sender") or {}).items()
                if isinstance(entry, dict)
            }
            self._context_sender_message_order = {
                self._normalize_pair_key(pair_key): deque(str(source_id) for source_id in source_ids)
                for pair_key, source_ids in (state.get("context_sender_message_order") or {}).items()
            }
            self._private_room_by_user_source = {
                str(user_source_id): str(room_source_id)
                for user_source_id, room_source_id in (state.get("private_room_by_user_source") or {}).items()
            }
            self._private_room_by_user_surrogate = {
                str(user_surrogate_id): str(room_source_id)
                for user_surrogate_id, room_source_id in (state.get("private_room_by_user_surrogate") or {}).items()
            }
            if self._private_room_by_user_surrogate:
                self._legacy_user_state_detected = True
            self._context_room_by_source = {}
            self._context_room_by_surrogate = {}
            for context_source_id, entry in (state.get("context_room_by_source") or {}).items():
                if not isinstance(entry, dict):
                    continue
                cloned_entry = deepcopy(entry)
                self._context_room_by_source[str(context_source_id)] = cloned_entry
                context_surrogate_id = str(cloned_entry.get("context_surrogate_id") or "").strip()
                if context_surrogate_id:
                    self._context_room_by_surrogate[context_surrogate_id] = cloned_entry
            if not self._context_room_by_surrogate:
                for context_surrogate_id, entry in (state.get("context_room_by_surrogate") or {}).items():
                    if not isinstance(entry, dict):
                        continue
                    self._context_room_by_surrogate[str(context_surrogate_id)] = deepcopy(entry)

    def replay_journal(self, journal_path: Path) -> None:
        for record in JournalPersistenceWorker.iter_records(journal_path) or []:
            self._apply_record(record, persist=False)

    @property
    def legacy_user_state_detected(self) -> bool:
        with self._lock:
            return self._legacy_user_state_detected

    def purge_legacy_user_dependent_state(self) -> None:
        with self._lock:
            self._messages_by_source = {}
            self._messages_by_surrogate = {}
            self._latest_by_context_sender = {}
            self._context_sender_message_order = {}
            self._private_room_by_user_surrogate = {}

    def get_private_room_source_bindings(self) -> dict[str, str]:
        with self._lock:
            return dict(self._private_room_by_user_source)

    def replace_private_room_surrogate_bindings(
        self,
        bindings: dict[int | str, str],
    ) -> None:
        with self._lock:
            self._private_room_by_user_surrogate = {
                str(user_surrogate_id): str(room_source_id)
                for user_surrogate_id, room_source_id in bindings.items()
            }

    def set_message_window_size(self, message_window_size: int) -> None:
        with self._lock:
            self._message_window_size = DurableIdMap.normalize_message_window_size(message_window_size)

    def allocate_mapping(self, namespace: str, source_id: str) -> tuple[IdMapping, MessageWindowSnapshot | None]:
        source_key = str(source_id)
        with self._lock:
            mapping, snapshot, created = self._allocate_mapping_locked(namespace, source_key)

        if created:
            self._record(
                {
                    "op": "id_put",
                    "namespace": namespace,
                    "source_id": source_key,
                    "surrogate_id": mapping.surrogate_id,
                }
            )
        return mapping, snapshot

    def _allocate_mapping_locked(
        self,
        namespace: str,
        source_key: str,
    ) -> tuple[IdMapping, MessageWindowSnapshot | None, bool]:
        existing_surrogate_id = self._forward[namespace].get(source_key)
        if existing_surrogate_id is not None:
            return IdMapping(namespace, source_key, int(existing_surrogate_id)), None, False

        self._counters[namespace] += 1
        surrogate_id = DurableIdMap._BASES[namespace] + int(self._counters[namespace])
        self._forward[namespace][source_key] = surrogate_id
        self._reverse[namespace][str(surrogate_id)] = source_key
        snapshot = None
        if namespace == "message":
            self._message_order.append(source_key)
            snapshot = self._trim_message_window_locked()
        return IdMapping(namespace, source_key, surrogate_id), snapshot, True

    def rebuild_message_window(self, *, force_compact: bool = False) -> MessageWindowSnapshot:
        with self._lock:
            snapshot = self._trim_message_window_locked(
                force_compact=force_compact,
                return_snapshot_when_unchanged=True,
            )
        if snapshot is None:
            snapshot = MessageWindowSnapshot(
                active_mappings={},
                changed=False,
                removed_count=0,
                compacted=False,
                active_count=0,
                max_entries=self._message_window_size,
            )
        if snapshot.changed:
            self._record(
                {
                    "op": "message_rebuild",
                    "active_mappings": dict(snapshot.active_mappings),
                }
            )
        return snapshot

    def get_source(self, namespace: str, surrogate_id: int | str) -> str | None:
        with self._lock:
            return self._reverse[namespace].get(str(surrogate_id))

    def get_surrogate(self, namespace: str, source_id: str) -> int | None:
        with self._lock:
            value = self._forward[namespace].get(str(source_id))
            return int(value) if value is not None else None

    def put_message(self, entry: dict[str, Any]) -> None:
        normalized_entry = self._normalize_message_entry(entry)
        source_id = str(normalized_entry["source_id"])
        with self._lock:
            previous_entry = self._messages_by_source.get(source_id)
            if isinstance(previous_entry, dict):
                self._remove_message_entry_locked(source_id, remove_order=False)

            surrogate_id = str(normalized_entry.get("surrogate_id") or "").strip()
            self._messages_by_source[source_id] = normalized_entry
            if surrogate_id:
                self._messages_by_surrogate[surrogate_id] = normalized_entry
            self._index_latest_context_sender_locked(normalized_entry)

        self._record({"op": "message_put", "entry": normalized_entry})

    def rebuild_messages_for_active_mappings(self, active_mappings: dict[str, int]) -> None:
        normalized_mappings = {
            str(source_id): int(surrogate_id)
            for source_id, surrogate_id in (active_mappings or {}).items()
        }
        with self._lock:
            retained_entries: list[dict[str, Any]] = []
            new_by_source: dict[str, dict[str, Any]] = {}
            new_by_surrogate: dict[str, dict[str, Any]] = {}

            for source_id, surrogate_id in sorted(normalized_mappings.items(), key=lambda item: item[1]):
                existing_entry = self._messages_by_source.get(source_id)
                if not isinstance(existing_entry, dict):
                    continue
                entry = self._clone_message_entry(existing_entry)
                MessageStore._rewrite_entry_surrogate(entry, surrogate_id, normalized_mappings)
                new_by_source[source_id] = entry
                new_by_surrogate[str(surrogate_id)] = entry
                retained_entries.append(entry)

            self._messages_by_source = new_by_source
            self._messages_by_surrogate = new_by_surrogate
            self._rebuild_latest_indexes_from_entries_locked(retained_entries)

        self._record({"op": "message_rebuild", "active_mappings": normalized_mappings})

    def get_message_by_source(self, source_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._messages_by_source.get(str(source_id))
            return self._clone_message_entry(entry) if isinstance(entry, dict) else None

    def get_message_by_surrogate(self, surrogate_id: int | str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._messages_by_surrogate.get(str(surrogate_id))
            return self._clone_message_entry(entry) if isinstance(entry, dict) else None

    def get_latest_room_by_context_sender(
        self,
        context_source_id: str,
        sender_source_id: str,
        *,
        max_age_seconds: int | float | None = None,
    ) -> str | None:
        latest_entry = self._get_latest_context_entry(
            context_source_id,
            sender_source_id,
            max_age_seconds=max_age_seconds,
        )
        if not isinstance(latest_entry, dict):
            return None
        room_source_id = str(latest_entry.get("room_source_id") or "").strip()
        return room_source_id or None

    def get_latest_thread_by_context_sender(
        self,
        context_source_id: str,
        sender_source_id: str,
        *,
        max_age_seconds: int | float | None = None,
    ) -> str | None:
        latest_entry = self._get_latest_context_entry(
            context_source_id,
            sender_source_id,
            max_age_seconds=max_age_seconds,
        )
        if not isinstance(latest_entry, dict):
            return None
        thread_source_id = str(latest_entry.get("thread_source_id") or "").strip()
        return thread_source_id or None

    def bind_private_room(self, user_source_id: str, user_surrogate_id: int, room_source_id: str) -> None:
        normalized_user_source_id = str(user_source_id)
        normalized_user_surrogate_id = int(user_surrogate_id)
        normalized_room_source_id = str(room_source_id)
        self._bind_private_room_without_record(
            normalized_user_source_id,
            normalized_user_surrogate_id,
            normalized_room_source_id,
        )
        self._record(
            {
                "op": "private_bind",
                "user_source_id": normalized_user_source_id,
                "user_surrogate_id": normalized_user_surrogate_id,
                "room_source_id": normalized_room_source_id,
            }
        )

    def _bind_private_room_without_record(
        self,
        user_source_id: str,
        user_surrogate_id: int,
        room_source_id: str,
    ) -> None:
        with self._lock:
            self._private_room_by_user_source[str(user_source_id)] = str(room_source_id)
            self._private_room_by_user_surrogate[str(user_surrogate_id)] = str(room_source_id)

    def get_room_by_user_source(self, user_source_id: str) -> str | None:
        with self._lock:
            return self._private_room_by_user_source.get(str(user_source_id))

    def get_room_by_user_surrogate(self, user_surrogate_id: int | str) -> str | None:
        with self._lock:
            return self._private_room_by_user_surrogate.get(str(user_surrogate_id))

    def bind_context_room(
        self,
        context_source_id: str,
        context_surrogate_id: int | str,
        room_source_id: str,
        room_surrogate_id: int | str,
        room_name: str,
        room_slug: str,
        thread_source_id: str = "",
        timestamp: int | None = None,
    ) -> None:
        normalized_entry = {
            "context_source_id": str(context_source_id),
            "context_surrogate_id": int(context_surrogate_id),
            "room_source_id": str(room_source_id),
            "room_surrogate_id": int(room_surrogate_id),
            "room_name": str(room_name),
            "room_slug": str(room_slug),
            "thread_source_id": str(thread_source_id or "").strip(),
        }
        if timestamp is not None:
            normalized_entry["timestamp"] = int(timestamp)

        if not self._bind_context_room_without_record(normalized_entry):
            return

        self._record({"op": "context_bind", "entry": normalized_entry})

    def _bind_context_room_without_record(self, entry: dict[str, Any]) -> bool:
        with self._lock:
            existing = self._context_room_by_source.get(entry["context_source_id"])
            timestamp = entry.get("timestamp")
            if timestamp is not None and isinstance(existing, dict):
                existing_timestamp = int(existing.get("timestamp") or 0)
                if existing_timestamp > int(timestamp):
                    return False
            normalized_entry = deepcopy(entry)
            self._context_room_by_source[str(normalized_entry["context_source_id"])] = normalized_entry
            self._context_room_by_surrogate[str(normalized_entry["context_surrogate_id"])] = normalized_entry
        return True

    def get_context_room_by_source(self, context_source_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._context_room_by_source.get(str(context_source_id))
            return deepcopy(entry) if isinstance(entry, dict) else None

    def get_context_room_by_surrogate(self, context_surrogate_id: int | str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._context_room_by_surrogate.get(str(context_surrogate_id))
            return deepcopy(entry) if isinstance(entry, dict) else None

    def _apply_record(self, record: dict[str, Any], *, persist: bool) -> None:
        if not isinstance(record, dict):
            return
        op = str(record.get("op") or "")
        if op == "batch":
            for item in record.get("mutations") or []:
                if isinstance(item, dict):
                    self._apply_record(item, persist=persist)
        elif op == "id_put":
            self._apply_id_put(record, persist=persist)
        elif op == "message_put":
            self._apply_message_put(record, persist=persist)
        elif op == "private_bind":
            self._apply_private_bind(record, persist=persist)
        elif op == "context_bind":
            self._apply_context_bind(record, persist=persist)
        elif op == "message_rebuild":
            self._apply_message_rebuild(record, persist=persist)

    def _apply_id_put(self, record: dict[str, Any], *, persist: bool) -> None:
        namespace = str(record.get("namespace") or "")
        if namespace == "user":
            self._legacy_user_state_detected = True
            return
        if namespace not in DurableIdMap._BASES:
            return
        source_id = str(record.get("source_id") or "")
        try:
            surrogate_id = int(record.get("surrogate_id") or 0)
        except (TypeError, ValueError):
            return
        if not source_id or surrogate_id <= 0:
            return

        with self._lock:
            existing_surrogate_id = self._forward[namespace].get(source_id)
            if existing_surrogate_id is None:
                self._counters[namespace] = max(
                    int(self._counters.get(namespace) or 0),
                    surrogate_id - DurableIdMap._BASES[namespace],
                )
                self._forward[namespace][source_id] = surrogate_id
                self._reverse[namespace][str(surrogate_id)] = source_id
                if namespace == "message":
                    self._message_order.append(source_id)
                    self._trim_message_window_locked()
        if persist:
            self._record(record)

    def _apply_message_put(self, record: dict[str, Any], *, persist: bool) -> None:
        entry = record.get("entry")
        if not isinstance(entry, dict):
            return
        sender_surrogate_id = entry.get("sender_surrogate_id")
        try:
            if 1_000_000_000 <= int(sender_surrogate_id) < 2_000_000_000:
                self._legacy_user_state_detected = True
        except (TypeError, ValueError):
            pass
        self.put_message(entry) if persist else self._put_message_without_record(entry)

    def _apply_private_bind(self, record: dict[str, Any], *, persist: bool) -> None:
        try:
            user_surrogate_id = int(record.get("user_surrogate_id") or 0)
        except (TypeError, ValueError):
            return
        if 1_000_000_000 <= user_surrogate_id < 2_000_000_000:
            self._legacy_user_state_detected = True
        if persist:
            self.bind_private_room(
                str(record.get("user_source_id") or ""),
                user_surrogate_id,
                str(record.get("room_source_id") or ""),
            )
            return
        self._bind_private_room_without_record(
            str(record.get("user_source_id") or ""),
            user_surrogate_id,
            str(record.get("room_source_id") or ""),
        )

    def _apply_context_bind(self, record: dict[str, Any], *, persist: bool) -> None:
        entry = record.get("entry")
        if not isinstance(entry, dict):
            return
        if persist:
            self.bind_context_room(
                context_source_id=str(entry.get("context_source_id") or ""),
                context_surrogate_id=int(entry.get("context_surrogate_id") or 0),
                room_source_id=str(entry.get("room_source_id") or ""),
                room_surrogate_id=int(entry.get("room_surrogate_id") or 0),
                room_name=str(entry.get("room_name") or ""),
                room_slug=str(entry.get("room_slug") or ""),
                thread_source_id=str(entry.get("thread_source_id") or ""),
                timestamp=entry.get("timestamp"),
            )
            return
        self._bind_context_room_without_record(entry)

    def _apply_message_rebuild(self, record: dict[str, Any], *, persist: bool) -> None:
        active_mappings = record.get("active_mappings")
        if not isinstance(active_mappings, dict):
            return
        if persist:
            self.rebuild_messages_for_active_mappings(active_mappings)
            return
        normalized_mappings = {
            str(source_id): int(surrogate_id)
            for source_id, surrogate_id in active_mappings.items()
        }
        with self._lock:
            retained_entries: list[dict[str, Any]] = []
            new_by_source: dict[str, dict[str, Any]] = {}
            new_by_surrogate: dict[str, dict[str, Any]] = {}
            for source_id, surrogate_id in sorted(normalized_mappings.items(), key=lambda item: item[1]):
                existing_entry = self._messages_by_source.get(source_id)
                if not isinstance(existing_entry, dict):
                    continue
                entry = self._clone_message_entry(existing_entry)
                MessageStore._rewrite_entry_surrogate(entry, surrogate_id, normalized_mappings)
                new_by_source[source_id] = entry
                new_by_surrogate[str(surrogate_id)] = entry
                retained_entries.append(entry)
            self._messages_by_source = new_by_source
            self._messages_by_surrogate = new_by_surrogate
            self._rebuild_latest_indexes_from_entries_locked(retained_entries)

    def _put_message_without_record(self, entry: dict[str, Any]) -> None:
        normalized_entry = self._normalize_message_entry(entry)
        source_id = str(normalized_entry["source_id"])
        with self._lock:
            previous_entry = self._messages_by_source.get(source_id)
            if isinstance(previous_entry, dict):
                self._remove_message_entry_locked(source_id, remove_order=False)

            surrogate_id = str(normalized_entry.get("surrogate_id") or "").strip()
            self._messages_by_source[source_id] = normalized_entry
            if surrogate_id:
                self._messages_by_surrogate[surrogate_id] = normalized_entry
            self._index_latest_context_sender_locked(normalized_entry)

    def _trim_message_window_locked(
        self,
        *,
        force_compact: bool = False,
        return_snapshot_when_unchanged: bool = False,
    ) -> MessageWindowSnapshot | None:
        removed_count = 0
        while len(self._message_order) > self._message_window_size:
            removed_source_id = self._message_order.popleft()
            surrogate_id = self._forward["message"].pop(removed_source_id, None)
            if surrogate_id is not None:
                self._reverse["message"].pop(str(surrogate_id), None)
            self._remove_message_entry_locked(removed_source_id, remove_order=False)
            removed_count += 1

        changed = removed_count > 0
        active_mappings = {
            str(source_id): int(surrogate_id)
            for source_id, surrogate_id in self._forward["message"].items()
        }

        if not changed and not return_snapshot_when_unchanged:
            return None

        return MessageWindowSnapshot(
            active_mappings=active_mappings,
            changed=changed,
            removed_count=removed_count,
            compacted=False,
            active_count=len(active_mappings),
            max_entries=self._message_window_size,
        )

    def _remove_message_entry_locked(self, source_id: str, *, remove_order: bool) -> None:
        existing_entry = self._messages_by_source.pop(str(source_id), None)
        if not isinstance(existing_entry, dict):
            return
        surrogate_id = str(existing_entry.get("surrogate_id") or "").strip()
        if surrogate_id:
            self._messages_by_surrogate.pop(surrogate_id, None)

        pair = self._message_context_pair(existing_entry)
        if pair is not None:
            order = self._context_sender_message_order.get(pair)
            if order is not None:
                try:
                    if order and order[0] == str(source_id):
                        order.popleft()
                    else:
                        order.remove(str(source_id))
                except ValueError:
                    pass
                self._refresh_latest_for_pair_locked(pair)

        if remove_order:
            try:
                self._message_order.remove(str(source_id))
            except ValueError:
                pass

    def _index_latest_context_sender_locked(self, entry: dict[str, Any]) -> None:
        pair = self._message_context_pair(entry)
        if pair is None:
            return
        source_id = str(entry.get("source_id") or "").strip()
        order = self._context_sender_message_order.setdefault(pair, deque())
        try:
            order.remove(source_id)
        except ValueError:
            pass
        order.append(source_id)
        self._latest_by_context_sender[pair] = self._build_latest_fragment(entry)

    def _rebuild_latest_indexes_from_entries_locked(self, entries: list[dict[str, Any]]) -> None:
        self._latest_by_context_sender = {}
        self._context_sender_message_order = {}
        for entry in entries:
            self._index_latest_context_sender_locked(entry)

    def _refresh_latest_for_pair_locked(self, pair: tuple[str, str]) -> None:
        order = self._context_sender_message_order.get(pair)
        if order is None:
            self._latest_by_context_sender.pop(pair, None)
            return

        while order:
            latest_source_id = order[-1]
            latest_entry = self._messages_by_source.get(latest_source_id)
            if isinstance(latest_entry, dict):
                self._latest_by_context_sender[pair] = self._build_latest_fragment(latest_entry)
                return
            order.pop()

        self._context_sender_message_order.pop(pair, None)
        self._latest_by_context_sender.pop(pair, None)

    def _get_latest_context_entry(
        self,
        context_source_id: str,
        sender_source_id: str,
        *,
        max_age_seconds: int | float | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            entry = self._latest_by_context_sender.get((str(context_source_id), str(sender_source_id)))
            if not isinstance(entry, dict):
                return None
            if max_age_seconds is not None:
                timestamp = int(entry.get("timestamp") or 0)
                if timestamp > 0 and (time.time() - timestamp) > float(max_age_seconds):
                    return None
            return deepcopy(entry)

    def _record(self, record: dict[str, Any]) -> None:
        if self._writer is not None:
            self._writer.enqueue(record)

    def _record_batch(self, mutations: list[dict[str, Any]]) -> None:
        if not mutations or self._writer is None:
            return
        self._writer.enqueue_batch(mutations)

    @staticmethod
    def _normalize_message_entry(entry: dict[str, Any]) -> dict[str, Any]:
        return RuntimeStateEngine._clone_json_like_mapping(entry)

    @staticmethod
    def _clone_message_entry(entry: dict[str, Any]) -> dict[str, Any]:
        return RuntimeStateEngine._clone_json_like_mapping(entry)

    @staticmethod
    def _clone_json_like_mapping(entry: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(entry, dict):
            return {}
        return {
            str(key): RuntimeStateEngine._clone_json_like_value(value)
            for key, value in entry.items()
        }

    @staticmethod
    def _clone_json_like_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): RuntimeStateEngine._clone_json_like_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [RuntimeStateEngine._clone_json_like_value(item) for item in value]
        return value

    @staticmethod
    def _normalize_pair_key(pair_key: Any) -> tuple[str, str]:
        if isinstance(pair_key, (tuple, list)) and len(pair_key) == 2:
            return str(pair_key[0]), str(pair_key[1])
        serialized = str(pair_key or "")
        context_source_id, _, sender_source_id = serialized.partition("\x1f")
        return context_source_id, sender_source_id

    @staticmethod
    def _build_latest_fragment(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_id": str(entry.get("source_id") or "").strip(),
            "room_source_id": str(entry.get("room_source_id") or "").strip(),
            "thread_source_id": str(entry.get("thread_source_id") or "").strip(),
            "timestamp": int(entry.get("timestamp") or 0),
        }

    @staticmethod
    def _message_context_pair(entry: dict[str, Any]) -> tuple[str, str] | None:
        context_source_id = str(entry.get("context_source_id") or "").strip()
        sender_source_id = str(entry.get("sender_source_id") or "").strip()
        room_source_id = str(entry.get("room_source_id") or "").strip()
        source_id = str(entry.get("source_id") or "").strip()
        if not context_source_id or not sender_source_id or not room_source_id or not source_id:
            return None
        return context_source_id, sender_source_id


class MemoryMessageStore(MessageStore):
    def __init__(self, state_engine: RuntimeStateEngine):
        self._state_engine = state_engine

    async def put(self, entry: dict[str, Any]) -> None:
        self._state_engine.put_message(entry)

    async def get_by_source(self, source_id: str) -> dict[str, Any] | None:
        return self._state_engine.get_message_by_source(source_id)

    async def get_by_surrogate(self, surrogate_id: int | str) -> dict[str, Any] | None:
        return self._state_engine.get_message_by_surrogate(surrogate_id)

    async def rebuild_for_active_mappings(self, active_mappings: dict[str, int]) -> None:
        self._state_engine.rebuild_messages_for_active_mappings(active_mappings)

    async def get_latest_room_by_context_sender(
        self,
        context_source_id: str,
        sender_source_id: str,
        *,
        max_age_seconds: int | float | None = None,
    ) -> str | None:
        return self._state_engine.get_latest_room_by_context_sender(
            context_source_id,
            sender_source_id,
            max_age_seconds=max_age_seconds,
        )

    async def get_latest_thread_by_context_sender(
        self,
        context_source_id: str,
        sender_source_id: str,
        *,
        max_age_seconds: int | float | None = None,
    ) -> str | None:
        return self._state_engine.get_latest_thread_by_context_sender(
            context_source_id,
            sender_source_id,
            max_age_seconds=max_age_seconds,
        )


class MemoryPrivateRoomStore(PrivateRoomStore):
    def __init__(self, state_engine: RuntimeStateEngine):
        self._state_engine = state_engine

    async def bind(self, user_source_id: str, user_surrogate_id: int, room_source_id: str) -> None:
        self._state_engine.bind_private_room(user_source_id, user_surrogate_id, room_source_id)

    async def get_room_by_user_source(self, user_source_id: str) -> str | None:
        return self._state_engine.get_room_by_user_source(user_source_id)

    async def get_room_by_user_surrogate(self, user_surrogate_id: int | str) -> str | None:
        return self._state_engine.get_room_by_user_surrogate(user_surrogate_id)


class MemoryContextRoomStore(ContextRoomStore):
    def __init__(self, state_engine: RuntimeStateEngine):
        self._state_engine = state_engine

    async def bind(
        self,
        context_source_id: str,
        context_surrogate_id: int | str,
        room_source_id: str,
        room_surrogate_id: int | str,
        room_name: str,
        room_slug: str,
        thread_source_id: str = "",
        timestamp: int | None = None,
    ) -> None:
        self._state_engine.bind_context_room(
            context_source_id=context_source_id,
            context_surrogate_id=context_surrogate_id,
            room_source_id=room_source_id,
            room_surrogate_id=room_surrogate_id,
            room_name=room_name,
            room_slug=room_slug,
            thread_source_id=thread_source_id,
            timestamp=timestamp,
        )

    async def get_by_context_source(self, context_source_id: str) -> dict[str, Any] | None:
        return self._state_engine.get_context_room_by_source(context_source_id)

    async def get_by_context_surrogate(self, context_surrogate_id: int | str) -> dict[str, Any] | None:
        return self._state_engine.get_context_room_by_surrogate(context_surrogate_id)


class MemoryDurableIdMap(DurableIdMap):
    def __init__(
        self,
        state_engine: RuntimeStateEngine,
        *,
        message_window_size: int = DurableIdMap._DEFAULT_MESSAGE_WINDOW_SIZE,
        on_message_window_changed=None,
    ):
        self._state_engine = state_engine
        self._message_window_size = self.normalize_message_window_size(message_window_size)
        self._on_message_window_changed = on_message_window_changed
        self._fallback_user_forward: dict[str, int] = {}
        self._fallback_user_reverse: dict[str, str] = {}

    def set_message_window_size(self, message_window_size: Any) -> None:
        self._message_window_size = self.normalize_message_window_size(message_window_size)
        self._state_engine.set_message_window_size(self._message_window_size)

    async def get_or_create(self, namespace: str, source_id: str) -> IdMapping:
        if namespace == "user":
            from .user_identity import (
                USER_ID_MAX,
                USER_ID_MIN,
                compute_primary_onebot_id,
            )

            source_key = str(source_id)
            existing = self._fallback_user_forward.get(source_key)
            if existing is not None:
                return IdMapping("user", source_key, existing)
            surrogate_id = compute_primary_onebot_id(source_key)
            while str(surrogate_id) in self._fallback_user_reverse:
                surrogate_id = (
                    USER_ID_MIN
                    if surrogate_id >= USER_ID_MAX
                    else surrogate_id + 1
                )
            self._fallback_user_forward[source_key] = surrogate_id
            self._fallback_user_reverse[str(surrogate_id)] = source_key
            return IdMapping("user", source_key, surrogate_id)
        mapping, _snapshot = self._state_engine.allocate_mapping(namespace, source_id)
        return mapping

    def begin_batch(self) -> RuntimeStateMutationBatch:
        return self._state_engine.begin_batch()

    async def rebuild_message_window(self, *, force_compact: bool = False) -> dict[str, Any]:
        snapshot = self._state_engine.rebuild_message_window(force_compact=force_compact)
        highest_surrogate_id = max(snapshot.active_mappings.values(), default=None)
        return {
            "changed": snapshot.changed,
            "removed_count": snapshot.removed_count,
            "compacted": False,
            "active_count": snapshot.active_count,
            "max_entries": snapshot.max_entries,
            "highest_surrogate_id": highest_surrogate_id,
            "reset_surrogate_id": None,
        }

    async def get_source(self, namespace: str, surrogate_id: int | str) -> str | None:
        if namespace == "user":
            return self._fallback_user_reverse.get(str(surrogate_id))
        return self._state_engine.get_source(namespace, surrogate_id)

    async def get_surrogate(self, namespace: str, source_id: str) -> int | None:
        if namespace == "user":
            return self._fallback_user_forward.get(str(source_id))
        return self._state_engine.get_surrogate(namespace, source_id)


def build_runtime_hot_stores(
    data_dir: Path,
    *,
    message_window_size: int = DurableIdMap._DEFAULT_MESSAGE_WINDOW_SIZE,
) -> RuntimeHotStoreBundle:
    data_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = data_dir / "runtime.snapshot.bin"
    journal_path = data_dir / "runtime.journal.bin"

    state_engine = RuntimeStateEngine(message_window_size=message_window_size)
    snapshot_payload = JournalPersistenceWorker.load_snapshot_payload(snapshot_path)
    if snapshot_payload is not None:
        try:
            state_engine.load_snapshot_payload(snapshot_payload)
        except Exception as exc:
            logger.warning(
                "[RocketCatShell] failed to load runtime snapshot | path=%s | error=%s",
                snapshot_path,
                exc,
            )
    try:
        state_engine.replay_journal(journal_path)
    except Exception as exc:
        logger.warning(
            "[RocketCatShell] failed to replay runtime journal | path=%s | error=%s",
            journal_path,
            exc,
        )
    if state_engine.legacy_user_state_detected:
        state_engine.purge_legacy_user_dependent_state()
        logger.warning(
            "[RocketCatShell][UserIdentity] 检测到旧版递增 user 映射，"
            "已清理旧用户反向索引和含旧用户 ID 的消息缓存；"
            "room/message/thread/context 映射保持不变。"
        )

    writer = JournalPersistenceWorker(
        snapshot_path=snapshot_path,
        journal_path=journal_path,
        snapshot_provider=state_engine.export_snapshot_payload,
    )
    state_engine.bind_writer(writer)
    state_engine.set_message_window_size(message_window_size)
    state_engine.rebuild_message_window(force_compact=False)

    message_store = MemoryMessageStore(state_engine)
    id_map = MemoryDurableIdMap(
        state_engine,
        message_window_size=message_window_size,
    )
    private_room_store = MemoryPrivateRoomStore(state_engine)
    context_room_store = MemoryContextRoomStore(state_engine)
    return RuntimeHotStoreBundle(
        state_engine=state_engine,
        writer=writer,
        id_map=id_map,
        message_store=message_store,
        private_room_store=private_room_store,
        context_room_store=context_room_store,
    )

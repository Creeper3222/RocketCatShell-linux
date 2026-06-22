from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from rocketcat_shell.logger import logger

from .id_map import DurableIdMap, IdMapping


USER_ID_ALGORITHM = "sha256-linear-v1"
USER_ID_MIN = 10_000_000_000
USER_ID_MAX = 99_999_999_999
USER_ID_TABLE_SIZE = 90_000_000_000
USER_ID_DOMAIN = b"rocketcat-user-id-v1\x00"
IDENTITY_SCHEMA_VERSION = 1
DEFAULT_IDENTITY_CACHE_MAX_ENTRIES = 4096


class UserIdentityError(RuntimeError):
    pass


class UserIdentityConflictError(UserIdentityError):
    def __init__(self, message: str, *, occupant: dict[str, Any] | None = None):
        super().__init__(message)
        self.occupant = occupant or {}


class UserIdentityRevisionError(UserIdentityError):
    pass


@dataclass(frozen=True, slots=True)
class UserIdentityMapping:
    user_id: str
    username: str
    nickname: str
    onebot_id: int
    primary_onebot_id: int
    probe_offset: int
    manual_override: bool
    is_bot: bool
    synthetic: bool
    first_seen_at: float
    last_seen_at: float
    revision: int

    def to_id_mapping(self) -> IdMapping:
        return IdMapping("user", self.user_id, self.onebot_id)


@dataclass(slots=True)
class _UserIdentitySharedState:
    connection: sqlite3.Connection | None
    lock: threading.RLock
    by_user: OrderedDict[str, UserIdentityMapping]
    by_onebot: OrderedDict[int, str]
    bot_user_seen: OrderedDict[tuple[str, str], None]
    cache_max_entries: int
    initialized: bool = False
    cache_hits: int = 0
    cache_misses: int = 0


def compute_primary_onebot_id(user_id: str) -> int:
    normalized_user_id = str(user_id or "").strip()
    if not normalized_user_id:
        raise ValueError("Rocket.Chat userId 不能为空")
    digest = hashlib.sha256(USER_ID_DOMAIN + normalized_user_id.encode("utf-8")).digest()
    return USER_ID_MIN + (int.from_bytes(digest, "big") % USER_ID_TABLE_SIZE)


def compute_probe_offset(primary_onebot_id: int, onebot_id: int) -> int:
    primary_index = int(primary_onebot_id) - USER_ID_MIN
    actual_index = int(onebot_id) - USER_ID_MIN
    return (actual_index - primary_index) % USER_ID_TABLE_SIZE


def validate_onebot_user_id(onebot_id: int | str) -> int:
    try:
        normalized = int(onebot_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("OneBot ID 必须是整数") from exc
    if normalized < USER_ID_MIN or normalized > USER_ID_MAX:
        raise ValueError(
            f"OneBot ID 必须位于 {USER_ID_MIN}–{USER_ID_MAX} 的 11 位范围内"
        )
    return normalized


def normalize_server_url(server_url: str) -> str:
    raw = str(server_url or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    if port is None:
        netloc = hostname
    else:
        netloc = f"{hostname}:{port}"
    return urlunsplit((scheme, netloc, parsed.path.rstrip("/"), "", ""))


def build_server_scope_key(server_url: str, cloud_workspace_id: str = "") -> str:
    workspace_id = str(cloud_workspace_id or "").strip()
    if workspace_id:
        return f"cloud:{workspace_id}"
    normalized_url = normalize_server_url(server_url)
    if not normalized_url:
        raise ValueError("无法为用户映射建立服务器范围：server_url 为空")
    return f"url:{normalized_url}"


def build_server_scope_name(server_url: str, cloud_workspace_id: str = "") -> str:
    scope_key = build_server_scope_key(server_url, cloud_workspace_id)
    return hashlib.sha256(scope_key.encode("utf-8")).hexdigest()[:24]


class UserIdentityRegistry:
    _shared_states_lock = threading.Lock()
    _shared_states: dict[str, _UserIdentitySharedState] = {}

    def __init__(
        self,
        database_path: Path,
        *,
        scope_key: str,
        bot_id: str = "",
        warning_path: Path | None = None,
        cache_max_entries: int = DEFAULT_IDENTITY_CACHE_MAX_ENTRIES,
    ):
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.scope_key = str(scope_key)
        self.bot_id = str(bot_id or "").strip()
        self.warning_path = Path(warning_path) if warning_path is not None else None
        normalized_cache_max = max(128, int(cache_max_entries or DEFAULT_IDENTITY_CACHE_MAX_ENTRIES))
        state_key = str(self.database_path)
        try:
            is_temporary_database = self.database_path.is_relative_to(
                Path(tempfile.gettempdir()).resolve()
            )
        except (OSError, ValueError):
            is_temporary_database = False
        self._persistent_connection = not is_temporary_database
        with self._shared_states_lock:
            state = self._shared_states.get(state_key)
            if state is None:
                connection = (
                    self._create_connection()
                    if self._persistent_connection
                    else None
                )
                state = _UserIdentitySharedState(
                    connection=connection,
                    lock=threading.RLock(),
                    by_user=OrderedDict(),
                    by_onebot=OrderedDict(),
                    bot_user_seen=OrderedDict(),
                    cache_max_entries=normalized_cache_max,
                )
                self._shared_states[state_key] = state
            else:
                state.cache_max_entries = normalized_cache_max
        self._state = state
        self._lock = state.lock
        self._by_user = state.by_user
        self._by_onebot = state.by_onebot
        self._bot_user_seen = state.bot_user_seen
        with self._lock:
            self._evict_caches_locked()
        self._initialize()

    @classmethod
    def for_server(
        cls,
        data_root: Path,
        *,
        server_url: str,
        cloud_workspace_id: str = "",
        bot_id: str = "",
        warning_path: Path | None = None,
        cache_max_entries: int = DEFAULT_IDENTITY_CACHE_MAX_ENTRIES,
    ) -> "UserIdentityRegistry":
        scope_key = build_server_scope_key(server_url, cloud_workspace_id)
        scope_name = hashlib.sha256(scope_key.encode("utf-8")).hexdigest()[:24]
        database_path = Path(data_root) / "user_identity" / f"{scope_name}.sqlite3"
        return cls(
            database_path,
            scope_key=scope_key,
            bot_id=bot_id,
            warning_path=warning_path,
            cache_max_entries=cache_max_entries,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = self._state.connection
        if connection is not None:
            return connection
        return self._create_connection()

    def _create_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def cache_summary(self) -> dict[str, int]:
        with self._lock:
            return {
                "by_user_entries": len(self._by_user),
                "by_onebot_entries": len(self._by_onebot),
                "bot_user_seen_entries": len(self._bot_user_seen),
                "max_entries": self._state.cache_max_entries,
                "hits": self._state.cache_hits,
                "misses": self._state.cache_misses,
            }

    @contextmanager
    def _open_connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            if self._state.connection is None:
                connection.close()

    def _initialize(self) -> None:
        with self._lock, self._open_connection() as connection:
            if self._state.initialized:
                return
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_mappings (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL DEFAULT '',
                    onebot_id INTEGER NOT NULL UNIQUE,
                    primary_onebot_id INTEGER NOT NULL,
                    probe_offset INTEGER NOT NULL DEFAULT 0,
                    manual_override INTEGER NOT NULL DEFAULT 0,
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    synthetic INTEGER NOT NULL DEFAULT 0,
                    first_seen_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1
                );

                CREATE INDEX IF NOT EXISTS idx_user_mappings_primary
                ON user_mappings(primary_onebot_id);

                CREATE TABLE IF NOT EXISTS bot_users (
                    bot_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    last_seen_at REAL NOT NULL,
                    PRIMARY KEY(bot_id, user_id),
                    FOREIGN KEY(user_id) REFERENCES user_mappings(user_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_bot_users_user
                ON bot_users(user_id);

                CREATE TABLE IF NOT EXISTS identity_conflicts (
                    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    primary_onebot_id INTEGER NOT NULL,
                    incumbent_user_id TEXT NOT NULL,
                    displaced_user_id TEXT NOT NULL,
                    displaced_onebot_id INTEGER NOT NULL,
                    probe_offset INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT 'hash_collision',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at REAL NOT NULL,
                    resolved_at REAL
                );

                CREATE INDEX IF NOT EXISTS idx_identity_conflicts_active
                ON identity_conflicts(status, displaced_user_id);
                """
            )
            metadata = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM metadata")
            }
            existing_algorithm = metadata.get("algorithm")
            existing_scope = metadata.get("scope_key")
            existing_schema = metadata.get("schema_version")
            if existing_algorithm not in (None, USER_ID_ALGORITHM):
                raise UserIdentityError(
                    f"用户映射算法不兼容: {existing_algorithm} != {USER_ID_ALGORITHM}"
                )
            if existing_scope not in (None, self.scope_key):
                raise UserIdentityError("用户映射数据库服务器范围不匹配")
            if existing_schema not in (None, str(IDENTITY_SCHEMA_VERSION)):
                raise UserIdentityError(
                    f"用户映射数据库版本不兼容: {existing_schema}"
                )
            connection.executemany(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                (
                    ("algorithm", USER_ID_ALGORITHM),
                    ("scope_key", self.scope_key),
                    ("schema_version", str(IDENTITY_SCHEMA_VERSION)),
                ),
            )
            self._state.initialized = True

    async def ensure_mapping(
        self,
        user_id: str,
        *,
        username: str = "",
        nickname: str = "",
        is_bot: bool = False,
        synthetic: bool = False,
        primary_override: int | None = None,
        bot_id: str | None = None,
    ) -> UserIdentityMapping:
        cached = self._get_matching_cached_mapping(
            user_id,
            username=username,
            nickname=nickname,
            is_bot=is_bot,
            synthetic=synthetic,
            bot_id=bot_id,
        )
        if cached is not None:
            self._record_cache_result(hit=True)
            return cached
        self._record_cache_result(hit=False)
        return await asyncio.to_thread(
            self.ensure_mapping_sync,
            user_id,
            username=username,
            nickname=nickname,
            is_bot=is_bot,
            synthetic=synthetic,
            primary_override=primary_override,
            bot_id=bot_id,
        )

    def ensure_mapping_sync(
        self,
        user_id: str,
        *,
        username: str = "",
        nickname: str = "",
        is_bot: bool = False,
        synthetic: bool = False,
        primary_override: int | None = None,
        bot_id: str | None = None,
    ) -> UserIdentityMapping:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("Rocket.Chat userId 不能为空")
        normalized_username = str(username or "").strip()
        normalized_nickname = str(nickname or "").strip()
        normalized_bot_id = str(bot_id if bot_id is not None else self.bot_id).strip()

        cached = self._by_user.get(normalized_user_id)
        if (
            cached is not None
            and (not normalized_username or normalized_username == cached.username)
            and (not normalized_nickname or normalized_nickname == cached.nickname)
            and bool(is_bot) == cached.is_bot
            and bool(synthetic) == cached.synthetic
        ):
            if normalized_bot_id and (
                normalized_bot_id,
                normalized_user_id,
            ) not in self._bot_user_seen:
                self._touch_bot_user(normalized_bot_id, normalized_user_id)
            self._touch_cached_mapping(cached)
            return cached

        now = time.time()
        conflict_payload: dict[str, Any] | None = None
        with self._lock, self._open_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_row = connection.execute(
                    "SELECT * FROM user_mappings WHERE user_id=?",
                    (normalized_user_id,),
                ).fetchone()
                if existing_row is not None:
                    updated_username = normalized_username or str(existing_row["username"] or "")
                    updated_nickname = normalized_nickname or str(existing_row["nickname"] or "")
                    profile_changed = (
                        updated_username != str(existing_row["username"] or "")
                        or updated_nickname != str(existing_row["nickname"] or "")
                        or bool(is_bot) != bool(existing_row["is_bot"])
                        or bool(synthetic) != bool(existing_row["synthetic"])
                    )
                    revision = int(existing_row["revision"])
                    if profile_changed:
                        revision += 1
                    connection.execute(
                        """
                        UPDATE user_mappings
                        SET username=?, nickname=?, is_bot=?, synthetic=?,
                            last_seen_at=?, revision=?
                        WHERE user_id=?
                        """,
                        (
                            updated_username,
                            updated_nickname,
                            int(bool(is_bot)),
                            int(bool(synthetic)),
                            now,
                            revision,
                            normalized_user_id,
                        ),
                    )
                    if normalized_bot_id:
                        self._upsert_bot_user(
                            connection,
                            normalized_bot_id,
                            normalized_user_id,
                            now,
                        )
                    row = connection.execute(
                        "SELECT * FROM user_mappings WHERE user_id=?",
                        (normalized_user_id,),
                    ).fetchone()
                    connection.execute("COMMIT")
                    mapping = self._row_to_mapping(row)
                    self._cache(mapping)
                    if normalized_bot_id:
                        self._remember_bot_user(normalized_bot_id, normalized_user_id)
                    return mapping

                primary_onebot_id = (
                    validate_onebot_user_id(primary_override)
                    if primary_override is not None
                    else compute_primary_onebot_id(normalized_user_id)
                )
                onebot_id = primary_onebot_id
                probe_offset = 0
                incumbent_row: sqlite3.Row | None = None
                while True:
                    occupied = connection.execute(
                        "SELECT * FROM user_mappings WHERE onebot_id=?",
                        (onebot_id,),
                    ).fetchone()
                    if occupied is None:
                        break
                    if probe_offset == 0:
                        incumbent_row = occupied
                    probe_offset += 1
                    if probe_offset >= USER_ID_TABLE_SIZE:
                        raise UserIdentityError("11 位 OneBot 用户 ID 空间已耗尽")
                    onebot_id = USER_ID_MIN + (
                        (primary_onebot_id - USER_ID_MIN + probe_offset)
                        % USER_ID_TABLE_SIZE
                    )

                connection.execute(
                    """
                    INSERT INTO user_mappings(
                        user_id, username, nickname, onebot_id,
                        primary_onebot_id, probe_offset, manual_override,
                        is_bot, synthetic, first_seen_at, last_seen_at, revision
                    ) VALUES(?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 1)
                    """,
                    (
                        normalized_user_id,
                        normalized_username,
                        normalized_nickname,
                        onebot_id,
                        primary_onebot_id,
                        probe_offset,
                        int(bool(is_bot)),
                        int(bool(synthetic)),
                        now,
                        now,
                    ),
                )
                if normalized_bot_id:
                    self._upsert_bot_user(
                        connection,
                        normalized_bot_id,
                        normalized_user_id,
                        now,
                    )
                if incumbent_row is not None:
                    cursor = connection.execute(
                        """
                        INSERT INTO identity_conflicts(
                            primary_onebot_id, incumbent_user_id,
                            displaced_user_id, displaced_onebot_id,
                            probe_offset, reason, status, created_at
                        ) VALUES(?, ?, ?, ?, ?, 'hash_collision', 'active', ?)
                        """,
                        (
                            primary_onebot_id,
                            str(incumbent_row["user_id"]),
                            normalized_user_id,
                            onebot_id,
                            probe_offset,
                            now,
                        ),
                    )
                    conflict_payload = {
                        "conflict_id": int(cursor.lastrowid),
                        "primary_onebot_id": primary_onebot_id,
                        "incumbent_user_id": str(incumbent_row["user_id"]),
                        "incumbent_username": str(incumbent_row["username"] or ""),
                        "incumbent_nickname": str(incumbent_row["nickname"] or ""),
                        "incumbent_onebot_id": int(incumbent_row["onebot_id"]),
                        "displaced_user_id": normalized_user_id,
                        "displaced_username": normalized_username,
                        "displaced_nickname": normalized_nickname,
                        "displaced_onebot_id": onebot_id,
                        "probe_offset": probe_offset,
                        "reason": "hash_collision",
                        "status": "active",
                        "created_at": now,
                    }
                row = connection.execute(
                    "SELECT * FROM user_mappings WHERE user_id=?",
                    (normalized_user_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        mapping = self._row_to_mapping(row)
        self._cache(mapping)
        if normalized_bot_id:
            self._remember_bot_user(normalized_bot_id, normalized_user_id)
        if conflict_payload is not None:
            self._record_warning(conflict_payload)
            self._log_conflict(conflict_payload, repeated=False)
        return mapping

    async def ensure_mappings(
        self,
        items: list[dict[str, Any]],
        *,
        bot_id: str | None = None,
    ) -> list[UserIdentityMapping]:
        if not items:
            return []
        cached_results: list[UserIdentityMapping] = []
        valid_count = 0
        all_cached = True
        for item in items:
            normalized_item = item or {}
            user_id = str(normalized_item.get("user_id") or "").strip()
            if not user_id:
                continue
            valid_count += 1
            cached = self._get_matching_cached_mapping(
                user_id,
                username=str(normalized_item.get("username") or ""),
                nickname=str(normalized_item.get("nickname") or ""),
                is_bot=bool(normalized_item.get("is_bot", False)),
                synthetic=bool(normalized_item.get("synthetic", False)),
                bot_id=bot_id,
            )
            if cached is None:
                all_cached = False
                break
            cached_results.append(cached)
        if all_cached and valid_count == len(cached_results):
            with self._lock:
                self._state.cache_hits += len(cached_results)
            return cached_results
        return await asyncio.to_thread(
            self.ensure_mappings_sync,
            items,
            bot_id=bot_id,
        )

    def ensure_mappings_sync(
        self,
        items: list[dict[str, Any]],
        *,
        bot_id: str | None = None,
    ) -> list[UserIdentityMapping]:
        normalized_bot_id = str(bot_id if bot_id is not None else self.bot_id).strip()
        normalized_items: list[dict[str, Any]] = []
        for item in items:
            user_id = str((item or {}).get("user_id") or "").strip()
            if not user_id:
                continue
            normalized_items.append(
                {
                    "user_id": user_id,
                    "username": str((item or {}).get("username") or "").strip(),
                    "nickname": str((item or {}).get("nickname") or "").strip(),
                    "is_bot": bool((item or {}).get("is_bot", False)),
                    "synthetic": bool((item or {}).get("synthetic", False)),
                }
            )
        if not normalized_items:
            return []

        results: list[UserIdentityMapping] = []
        conflicts: list[dict[str, Any]] = []
        now = time.time()
        with self._lock, self._open_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for item in normalized_items:
                    user_id = item["user_id"]
                    row = connection.execute(
                        "SELECT * FROM user_mappings WHERE user_id=?",
                        (user_id,),
                    ).fetchone()
                    if row is not None:
                        username = item["username"] or str(row["username"] or "")
                        nickname = item["nickname"] or str(row["nickname"] or "")
                        profile_changed = (
                            username != str(row["username"] or "")
                            or nickname != str(row["nickname"] or "")
                            or item["is_bot"] != bool(row["is_bot"])
                            or item["synthetic"] != bool(row["synthetic"])
                        )
                        revision = int(row["revision"]) + int(profile_changed)
                        connection.execute(
                            """
                            UPDATE user_mappings
                            SET username=?, nickname=?, is_bot=?, synthetic=?,
                                last_seen_at=?, revision=?
                            WHERE user_id=?
                            """,
                            (
                                username,
                                nickname,
                                int(item["is_bot"]),
                                int(item["synthetic"]),
                                now,
                                revision,
                                user_id,
                            ),
                        )
                    else:
                        primary_onebot_id = compute_primary_onebot_id(user_id)
                        onebot_id = primary_onebot_id
                        probe_offset = 0
                        incumbent_row: sqlite3.Row | None = None
                        while True:
                            occupied = connection.execute(
                                "SELECT * FROM user_mappings WHERE onebot_id=?",
                                (onebot_id,),
                            ).fetchone()
                            if occupied is None:
                                break
                            if probe_offset == 0:
                                incumbent_row = occupied
                            probe_offset += 1
                            if probe_offset >= USER_ID_TABLE_SIZE:
                                raise UserIdentityError("11 位 OneBot 用户 ID 空间已耗尽")
                            onebot_id = USER_ID_MIN + (
                                (primary_onebot_id - USER_ID_MIN + probe_offset)
                                % USER_ID_TABLE_SIZE
                            )
                        connection.execute(
                            """
                            INSERT INTO user_mappings(
                                user_id, username, nickname, onebot_id,
                                primary_onebot_id, probe_offset, manual_override,
                                is_bot, synthetic, first_seen_at, last_seen_at, revision
                            ) VALUES(?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 1)
                            """,
                            (
                                user_id,
                                item["username"],
                                item["nickname"],
                                onebot_id,
                                primary_onebot_id,
                                probe_offset,
                                int(item["is_bot"]),
                                int(item["synthetic"]),
                                now,
                                now,
                            ),
                        )
                        if incumbent_row is not None:
                            cursor = connection.execute(
                                """
                                INSERT INTO identity_conflicts(
                                    primary_onebot_id, incumbent_user_id,
                                    displaced_user_id, displaced_onebot_id,
                                    probe_offset, reason, status, created_at
                                ) VALUES(?, ?, ?, ?, ?, 'hash_collision', 'active', ?)
                                """,
                                (
                                    primary_onebot_id,
                                    str(incumbent_row["user_id"]),
                                    user_id,
                                    onebot_id,
                                    probe_offset,
                                    now,
                                ),
                            )
                            conflicts.append(
                                {
                                    "conflict_id": int(cursor.lastrowid),
                                    "primary_onebot_id": primary_onebot_id,
                                    "incumbent_user_id": str(incumbent_row["user_id"]),
                                    "incumbent_username": str(incumbent_row["username"] or ""),
                                    "incumbent_nickname": str(incumbent_row["nickname"] or ""),
                                    "incumbent_onebot_id": int(incumbent_row["onebot_id"]),
                                    "displaced_user_id": user_id,
                                    "displaced_username": item["username"],
                                    "displaced_nickname": item["nickname"],
                                    "displaced_onebot_id": onebot_id,
                                    "probe_offset": probe_offset,
                                    "reason": "hash_collision",
                                    "status": "active",
                                    "created_at": now,
                                }
                            )
                    if normalized_bot_id:
                        self._upsert_bot_user(
                            connection,
                            normalized_bot_id,
                            user_id,
                            now,
                        )
                    final_row = connection.execute(
                        "SELECT * FROM user_mappings WHERE user_id=?",
                        (user_id,),
                    ).fetchone()
                    results.append(self._row_to_mapping(final_row))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        for mapping in results:
            self._cache(mapping)
            if normalized_bot_id:
                self._remember_bot_user(normalized_bot_id, mapping.user_id)
        for conflict in conflicts:
            self._log_conflict(conflict, repeated=False)
        if conflicts:
            self._record_warning(conflicts[-1])
        return results

    async def get_by_user_id(self, user_id: str) -> UserIdentityMapping | None:
        normalized = str(user_id or "").strip()
        with self._lock:
            cached = self._by_user.get(normalized)
        if cached is not None:
            self._record_cache_result(hit=True)
            self._touch_cached_mapping(cached)
            return cached
        self._record_cache_result(hit=False)
        return await asyncio.to_thread(self.get_by_user_id_sync, user_id)

    def get_by_user_id_sync(self, user_id: str) -> UserIdentityMapping | None:
        normalized = str(user_id or "").strip()
        if not normalized:
            return None
        cached = self._by_user.get(normalized)
        if cached is not None:
            self._touch_cached_mapping(cached)
            return cached
        with self._lock, self._open_connection() as connection:
            row = connection.execute(
                "SELECT * FROM user_mappings WHERE user_id=?",
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        mapping = self._row_to_mapping(row)
        self._cache(mapping)
        return mapping

    async def get_by_onebot_id(self, onebot_id: int | str) -> UserIdentityMapping | None:
        try:
            normalized = validate_onebot_user_id(onebot_id)
        except ValueError:
            return None
        with self._lock:
            cached_user_id = self._by_onebot.get(normalized)
            cached = (
                self._by_user.get(cached_user_id)
                if cached_user_id is not None
                else None
            )
        if cached_user_id is not None:
            if cached is not None:
                self._record_cache_result(hit=True)
                self._touch_cached_mapping(cached)
                return cached
        self._record_cache_result(hit=False)
        return await asyncio.to_thread(self.get_by_onebot_id_sync, onebot_id)

    def invalidate_cache(self, user_id: str | None = None) -> None:
        with self._lock:
            if user_id is None:
                self._by_user.clear()
                self._by_onebot.clear()
                return
            normalized = str(user_id)
            previous = self._by_user.pop(normalized, None)
            if previous is not None:
                self._by_onebot.pop(previous.onebot_id, None)

    def get_by_onebot_id_sync(self, onebot_id: int | str) -> UserIdentityMapping | None:
        try:
            normalized = int(onebot_id)
        except (TypeError, ValueError):
            return None
        cached_user_id = self._by_onebot.get(normalized)
        if cached_user_id:
            cached = self._by_user.get(cached_user_id)
            if cached is not None:
                self._touch_cached_mapping(cached)
            return cached
        with self._lock, self._open_connection() as connection:
            row = connection.execute(
                "SELECT * FROM user_mappings WHERE onebot_id=?",
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        mapping = self._row_to_mapping(row)
        self._cache(mapping)
        return mapping

    async def list_mappings(
        self,
        *,
        bot_id: str,
        search: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.list_mappings_sync,
            bot_id=bot_id,
            search=search,
            offset=offset,
            limit=limit,
        )

    def list_mappings_sync(
        self,
        *,
        bot_id: str,
        search: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        normalized_bot_id = str(bot_id or "").strip()
        normalized_search = str(search or "").strip()
        normalized_offset = max(0, int(offset))
        normalized_limit = max(1, min(200, int(limit)))
        where = "WHERE bu.bot_id=?"
        params: list[Any] = [normalized_bot_id]
        if normalized_search:
            where += (
                " AND (m.user_id LIKE ? OR m.username LIKE ? OR "
                "m.nickname LIKE ? OR CAST(m.onebot_id AS TEXT) LIKE ?)"
            )
            token = f"%{normalized_search}%"
            params.extend((token, token, token, token))

        with self._lock, self._open_connection() as connection:
            total = int(
                connection.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM user_mappings m
                    JOIN bot_users bu ON bu.user_id=m.user_id
                    {where}
                    """,
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT m.*
                FROM user_mappings m
                JOIN bot_users bu ON bu.user_id=m.user_id
                {where}
                ORDER BY m.first_seen_at, m.user_id
                LIMIT ? OFFSET ?
                """,
                (*params, normalized_limit, normalized_offset),
            ).fetchall()
            user_ids = [str(row["user_id"]) for row in rows]
            conflicts_by_user: dict[str, list[dict[str, Any]]] = {
                user_id: [] for user_id in user_ids
            }
            if user_ids:
                placeholders = ",".join("?" for _ in user_ids)
                conflict_rows = connection.execute(
                    f"""
                    SELECT c.*,
                           i.username AS incumbent_username,
                           i.nickname AS incumbent_nickname,
                           i.onebot_id AS incumbent_onebot_id,
                           d.username AS displaced_username,
                           d.nickname AS displaced_nickname
                    FROM identity_conflicts c
                    JOIN user_mappings i ON i.user_id=c.incumbent_user_id
                    JOIN user_mappings d ON d.user_id=c.displaced_user_id
                    WHERE c.status='active'
                      AND (
                        c.incumbent_user_id IN ({placeholders})
                        OR c.displaced_user_id IN ({placeholders})
                      )
                    ORDER BY c.created_at
                    """,
                    (*user_ids, *user_ids),
                ).fetchall()
                for conflict_row in conflict_rows:
                    payload = dict(conflict_row)
                    incumbent = str(conflict_row["incumbent_user_id"])
                    displaced = str(conflict_row["displaced_user_id"])
                    if incumbent in conflicts_by_user:
                        conflicts_by_user[incumbent].append(
                            {**payload, "role": "incumbent"}
                        )
                    if displaced in conflicts_by_user:
                        conflicts_by_user[displaced].append(
                            {**payload, "role": "displaced"}
                        )

        items = []
        for row in rows:
            mapping = self._row_to_mapping(row)
            payload = asdict(mapping)
            payload["conflicts"] = conflicts_by_user.get(mapping.user_id, [])
            roles = {item.get("role") for item in payload["conflicts"]}
            payload["conflict_role"] = (
                "displaced"
                if "displaced" in roles
                else "incumbent"
                if "incumbent" in roles
                else ""
            )
            items.append(payload)
        return {
            "items": items,
            "total": total,
            "offset": normalized_offset,
            "limit": normalized_limit,
            "algorithm": USER_ID_ALGORITHM,
            "scope_key": self.scope_key,
        }

    async def override_onebot_id(
        self,
        *,
        bot_id: str,
        user_id: str,
        onebot_id: int | str,
        revision: int,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.override_onebot_id_sync,
            bot_id=bot_id,
            user_id=user_id,
            onebot_id=onebot_id,
            revision=revision,
        )

    def override_onebot_id_sync(
        self,
        *,
        bot_id: str,
        user_id: str,
        onebot_id: int | str,
        revision: int,
    ) -> dict[str, Any]:
        normalized_bot_id = str(bot_id or "").strip()
        normalized_user_id = str(user_id or "").strip()
        desired_onebot_id = validate_onebot_user_id(onebot_id)
        now = time.time()
        created_conflicts: list[dict[str, Any]] = []
        resolved_count = 0

        with self._lock, self._open_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT m.*
                    FROM user_mappings m
                    JOIN bot_users bu ON bu.user_id=m.user_id
                    WHERE bu.bot_id=? AND m.user_id=?
                    """,
                    (normalized_bot_id, normalized_user_id),
                ).fetchone()
                if row is None:
                    raise UserIdentityError("找不到该 bot 的用户映射")
                if int(row["revision"]) != int(revision):
                    raise UserIdentityRevisionError(
                        "映射已被其他操作更新，请刷新列表后重试"
                    )

                occupant = connection.execute(
                    "SELECT * FROM user_mappings WHERE onebot_id=? AND user_id<>?",
                    (desired_onebot_id, normalized_user_id),
                ).fetchone()
                if occupant is not None:
                    occupant_payload = asdict(self._row_to_mapping(occupant))
                    raise UserIdentityConflictError(
                        "该 OneBot ID 已被其他用户实际占用",
                        occupant=occupant_payload,
                    )

                previous_onebot_id = int(row["onebot_id"])
                primary_onebot_id = int(row["primary_onebot_id"])
                new_revision = int(row["revision"]) + 1
                connection.execute(
                    """
                    UPDATE user_mappings
                    SET onebot_id=?, probe_offset=?, manual_override=1,
                        last_seen_at=?, revision=?
                    WHERE user_id=?
                    """,
                    (
                        desired_onebot_id,
                        compute_probe_offset(primary_onebot_id, desired_onebot_id),
                        now,
                        new_revision,
                        normalized_user_id,
                    ),
                )

                resolved_cursor = connection.execute(
                    """
                    UPDATE identity_conflicts
                    SET status='resolved', resolved_at=?
                    WHERE status='active' AND displaced_user_id=?
                    """,
                    (now, normalized_user_id),
                )
                resolved_count = int(resolved_cursor.rowcount)

                primary_owners = connection.execute(
                    """
                    SELECT *
                    FROM user_mappings
                    WHERE primary_onebot_id=? AND user_id<>?
                    ORDER BY first_seen_at, user_id
                    """,
                    (desired_onebot_id, normalized_user_id),
                ).fetchall()
                for owner in primary_owners:
                    cursor = connection.execute(
                        """
                        INSERT INTO identity_conflicts(
                            primary_onebot_id, incumbent_user_id,
                            displaced_user_id, displaced_onebot_id,
                            probe_offset, reason, status, created_at
                        ) VALUES(?, ?, ?, ?, ?, 'manual_primary_collision', 'active', ?)
                        """,
                        (
                            desired_onebot_id,
                            str(owner["user_id"]),
                            normalized_user_id,
                            desired_onebot_id,
                            compute_probe_offset(
                                int(row["primary_onebot_id"]),
                                desired_onebot_id,
                            ),
                            now,
                        ),
                    )
                    created_conflicts.append(
                        {
                            "conflict_id": int(cursor.lastrowid),
                            "primary_onebot_id": desired_onebot_id,
                            "incumbent_user_id": str(owner["user_id"]),
                            "incumbent_username": str(owner["username"] or ""),
                            "incumbent_nickname": str(owner["nickname"] or ""),
                            "incumbent_onebot_id": int(owner["onebot_id"]),
                            "displaced_user_id": normalized_user_id,
                            "displaced_username": str(row["username"] or ""),
                            "displaced_nickname": str(row["nickname"] or ""),
                            "displaced_onebot_id": desired_onebot_id,
                            "probe_offset": compute_probe_offset(
                                int(row["primary_onebot_id"]),
                                desired_onebot_id,
                            ),
                            "reason": "manual_primary_collision",
                            "status": "active",
                            "created_at": now,
                        }
                    )

                updated = connection.execute(
                    "SELECT * FROM user_mappings WHERE user_id=?",
                    (normalized_user_id,),
                ).fetchone()
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        self._remember_bot_user(normalized_bot_id, normalized_user_id)

        self._by_onebot.pop(previous_onebot_id, None)
        mapping = self._row_to_mapping(updated)
        self._cache(mapping)
        for conflict in created_conflicts:
            self._record_warning(conflict)
            self._log_conflict(conflict, repeated=False)
        try:
            self.sync_warning_file_sync(
                bot_id=normalized_bot_id,
                warning_path=self.warning_path,
            )
        except Exception:
            logger.exception(
                "[RocketCatShell][UserIdentity] override 后同步 re_waring 失败"
            )
        return {
            "item": asdict(mapping),
            "resolved_conflict_count": resolved_count,
            "created_conflict_count": len(created_conflicts),
        }

    async def delete_mapping(
        self,
        *,
        bot_id: str,
        user_id: str,
        revision: int,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.delete_mapping_sync,
            bot_id=bot_id,
            user_id=user_id,
            revision=revision,
        )

    def delete_mapping_sync(
        self,
        *,
        bot_id: str,
        user_id: str,
        revision: int,
    ) -> dict[str, Any]:
        normalized_bot_id = str(bot_id or "").strip()
        normalized_user_id = str(user_id or "").strip()
        now = time.time()

        with self._lock, self._open_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT m.*
                    FROM user_mappings m
                    JOIN bot_users bu ON bu.user_id=m.user_id
                    WHERE bu.bot_id=? AND m.user_id=?
                    """,
                    (normalized_bot_id, normalized_user_id),
                ).fetchone()
                if row is None:
                    raise UserIdentityError("找不到该 bot 的用户映射")
                if int(row["revision"]) != int(revision):
                    raise UserIdentityRevisionError(
                        "映射已被其他操作更新，请刷新列表后重试"
                    )

                mapping = self._row_to_mapping(row)
                resolved_cursor = connection.execute(
                    """
                    UPDATE identity_conflicts
                    SET status='resolved', resolved_at=?
                    WHERE status='active'
                      AND (
                        incumbent_user_id=?
                        OR displaced_user_id=?
                      )
                    """,
                    (now, normalized_user_id, normalized_user_id),
                )
                resolved_count = int(resolved_cursor.rowcount)

                delete_cursor = connection.execute(
                    "DELETE FROM user_mappings WHERE user_id=?",
                    (normalized_user_id,),
                )
                if int(delete_cursor.rowcount or 0) != 1:
                    raise UserIdentityError("删除用户映射失败")
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        self.invalidate_cache(normalized_user_id)
        with self._lock:
            for pair in tuple(self._bot_user_seen):
                if pair[1] == normalized_user_id:
                    self._bot_user_seen.pop(pair, None)
        try:
            self.sync_warning_file_sync(
                bot_id=normalized_bot_id,
                warning_path=self.warning_path,
            )
        except Exception:
            logger.exception(
                "[RocketCatShell][UserIdentity] delete 后同步 re_waring 失败"
            )
        return {
            "item": asdict(mapping),
            "resolved_conflict_count": resolved_count,
        }

    async def sync_warning_file(
        self,
        *,
        bot_id: str | None = None,
        warning_path: Path | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self.sync_warning_file_sync,
            bot_id=bot_id,
            warning_path=warning_path,
        )

    def sync_warning_file_sync(
        self,
        *,
        bot_id: str | None = None,
        warning_path: Path | None = None,
    ) -> dict[str, Any]:
        normalized_bot_id = str(bot_id if bot_id is not None else self.bot_id).strip()
        target = Path(warning_path) if warning_path is not None else self.warning_path
        if target is None:
            return {"conflicts": []}
        with self._lock, self._open_connection() as connection:
            rows = connection.execute(
                """
                SELECT c.*,
                       i.username AS incumbent_username,
                       i.nickname AS incumbent_nickname,
                       i.onebot_id AS incumbent_onebot_id,
                       d.username AS displaced_username,
                       d.nickname AS displaced_nickname
                FROM identity_conflicts c
                JOIN user_mappings i ON i.user_id=c.incumbent_user_id
                JOIN user_mappings d ON d.user_id=c.displaced_user_id
                WHERE c.status='active'
                  AND (
                    EXISTS(
                        SELECT 1 FROM bot_users bu
                        WHERE bu.bot_id=? AND bu.user_id=c.incumbent_user_id
                    )
                    OR EXISTS(
                        SELECT 1 FROM bot_users bu
                        WHERE bu.bot_id=? AND bu.user_id=c.displaced_user_id
                    )
                  )
                ORDER BY c.created_at
                """,
                (normalized_bot_id, normalized_bot_id),
            ).fetchall()
        payload = {
            "version": 1,
            "algorithm": USER_ID_ALGORITHM,
            "scope_key": self.scope_key,
            "bot_id": normalized_bot_id,
            "updated_at": time.time(),
            "conflicts": [dict(row) for row in rows],
        }
        self._write_json_atomic(target, payload)
        return payload

    def repeat_persisted_warnings(self) -> int:
        if self.warning_path is None or not self.warning_path.exists():
            return 0
        try:
            payload = json.loads(self.warning_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(
                "[RocketCatShell][UserIdentity] re_waring 文件读取失败: path=%s error=%r",
                self.warning_path,
                exc,
            )
            return 0
        conflicts = payload.get("conflicts") if isinstance(payload, dict) else []
        repeated = 0
        for conflict in conflicts or []:
            if not isinstance(conflict, dict):
                continue
            self._log_conflict(conflict, repeated=True)
            repeated += 1
        return repeated

    async def inject_synthetic_collision(
        self,
        *,
        anchor_user_id: str,
        synthetic_user_id: str,
        username: str,
        nickname: str,
        bot_id: str,
    ) -> UserIdentityMapping:
        anchor = await self.get_by_user_id(anchor_user_id)
        if anchor is None:
            raise UserIdentityError("注入测试冲突前必须先存在锚点用户映射")
        return await self.ensure_mapping(
            synthetic_user_id,
            username=username,
            nickname=nickname,
            synthetic=True,
            primary_override=anchor.primary_onebot_id,
            bot_id=bot_id,
        )

    def _touch_bot_user(self, bot_id: str, user_id: str) -> None:
        now = time.time()
        with self._lock, self._open_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._upsert_bot_user(connection, bot_id, user_id, now)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        self._remember_bot_user(bot_id, user_id)

    @staticmethod
    def _upsert_bot_user(
        connection: sqlite3.Connection,
        bot_id: str,
        user_id: str,
        now: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO bot_users(bot_id, user_id, last_seen_at)
            VALUES(?, ?, ?)
            ON CONFLICT(bot_id, user_id)
            DO UPDATE SET last_seen_at=excluded.last_seen_at
            """,
            (bot_id, user_id, now),
        )

    def _record_warning(self, conflict: dict[str, Any]) -> None:
        if self.warning_path is None:
            return
        try:
            self.sync_warning_file_sync()
        except Exception:
            logger.exception(
                "[RocketCatShell][UserIdentity] 写入 re_waring 失败 | conflict_id=%s",
                conflict.get("conflict_id"),
            )

    @staticmethod
    def _log_conflict(conflict: dict[str, Any], *, repeated: bool) -> None:
        prefix = "持久化重复提醒" if repeated else "检测到新的哈希冲突"
        logger.warning(
            "[RocketCatShell][UserIdentity] %s | 主槽=%s | "
            "先入 userId=%s username=%s nickname=%s onebot_id=%s | "
            "后入 userId=%s username=%s nickname=%s onebot_id=%s | "
            "偏移=%s | reason=%s | "
            "请审查管理员权限配置；实际 OneBot ID 已保持一对一",
            prefix,
            conflict.get("primary_onebot_id"),
            conflict.get("incumbent_user_id"),
            conflict.get("incumbent_username", ""),
            conflict.get("incumbent_nickname", ""),
            conflict.get("incumbent_onebot_id", conflict.get("primary_onebot_id")),
            conflict.get("displaced_user_id"),
            conflict.get("displaced_username", ""),
            conflict.get("displaced_nickname", ""),
            conflict.get("displaced_onebot_id"),
            conflict.get("probe_offset"),
            conflict.get("reason", "hash_collision"),
        )

    def _cache(self, mapping: UserIdentityMapping) -> None:
        with self._lock:
            previous = self._by_user.get(mapping.user_id)
            if previous is not None and previous.onebot_id != mapping.onebot_id:
                self._by_onebot.pop(previous.onebot_id, None)
            self._by_user[mapping.user_id] = mapping
            self._by_user.move_to_end(mapping.user_id)
            self._by_onebot[mapping.onebot_id] = mapping.user_id
            self._by_onebot.move_to_end(mapping.onebot_id)
            self._evict_caches_locked()

    def _touch_cached_mapping(self, mapping: UserIdentityMapping) -> None:
        with self._lock:
            if mapping.user_id in self._by_user:
                self._by_user.move_to_end(mapping.user_id)
            if mapping.onebot_id in self._by_onebot:
                self._by_onebot.move_to_end(mapping.onebot_id)

    def _remember_bot_user(self, bot_id: str, user_id: str) -> None:
        pair = (str(bot_id or "").strip(), str(user_id or "").strip())
        if not pair[0] or not pair[1]:
            return
        with self._lock:
            self._bot_user_seen[pair] = None
            self._bot_user_seen.move_to_end(pair)
            while len(self._bot_user_seen) > self._state.cache_max_entries:
                self._bot_user_seen.popitem(last=False)

    def _get_matching_cached_mapping(
        self,
        user_id: str,
        *,
        username: str,
        nickname: str,
        is_bot: bool,
        synthetic: bool,
        bot_id: str | None,
    ) -> UserIdentityMapping | None:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return None
        normalized_bot_id = str(bot_id if bot_id is not None else self.bot_id).strip()
        with self._lock:
            cached = self._by_user.get(normalized_user_id)
            if cached is None:
                return None
            if (
                (username and str(username).strip() != cached.username)
                or (nickname and str(nickname).strip() != cached.nickname)
                or bool(is_bot) != cached.is_bot
                or bool(synthetic) != cached.synthetic
            ):
                return None
            if normalized_bot_id and (
                normalized_bot_id,
                normalized_user_id,
            ) not in self._bot_user_seen:
                return None
            self._touch_cached_mapping(cached)
            return cached

    def _evict_caches_locked(self) -> None:
        limit = max(128, int(self._state.cache_max_entries))
        while len(self._by_user) > limit:
            user_id, mapping = self._by_user.popitem(last=False)
            if self._by_onebot.get(mapping.onebot_id) == user_id:
                self._by_onebot.pop(mapping.onebot_id, None)
        while len(self._by_onebot) > limit:
            onebot_id, user_id = self._by_onebot.popitem(last=False)
            mapping = self._by_user.get(user_id)
            if mapping is not None and mapping.onebot_id == onebot_id:
                self._by_user.pop(user_id, None)
        while len(self._bot_user_seen) > limit:
            self._bot_user_seen.popitem(last=False)

    def _record_cache_result(self, *, hit: bool) -> None:
        with self._lock:
            if hit:
                self._state.cache_hits += 1
            else:
                self._state.cache_misses += 1

    @staticmethod
    def _row_to_mapping(row: sqlite3.Row | None) -> UserIdentityMapping:
        if row is None:
            raise UserIdentityError("用户映射记录不存在")
        return UserIdentityMapping(
            user_id=str(row["user_id"]),
            username=str(row["username"] or ""),
            nickname=str(row["nickname"] or ""),
            onebot_id=int(row["onebot_id"]),
            primary_onebot_id=int(row["primary_onebot_id"]),
            probe_offset=int(row["probe_offset"]),
            manual_override=bool(row["manual_override"]),
            is_bot=bool(row["is_bot"]),
            synthetic=bool(row["synthetic"]),
            first_seen_at=float(row["first_seen_at"]),
            last_seen_at=float(row["last_seen_at"]),
            revision=int(row["revision"]),
        )

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)


class UserIdentityIdMap(DurableIdMap):
    def __init__(
        self,
        base_map: DurableIdMap,
        registry: UserIdentityRegistry,
    ):
        self._base_map = base_map
        self.registry = registry

    async def ensure_user(
        self,
        user_id: str,
        *,
        username: str = "",
        nickname: str = "",
        is_bot: bool = False,
        synthetic: bool = False,
    ) -> IdMapping:
        mapping = await self.registry.ensure_mapping(
            user_id,
            username=username,
            nickname=nickname,
            is_bot=is_bot,
            synthetic=synthetic,
        )
        return mapping.to_id_mapping()

    async def ensure_users(
        self,
        items: list[dict[str, Any]],
    ) -> dict[str, IdMapping]:
        mappings = await self.registry.ensure_mappings(items)
        return {
            mapping.user_id: mapping.to_id_mapping()
            for mapping in mappings
        }

    async def get_or_create(self, namespace: str, source_id: str) -> IdMapping:
        if namespace == "user":
            mapping = await self.registry.ensure_mapping(source_id)
            return mapping.to_id_mapping()
        return await self._base_map.get_or_create(namespace, source_id)

    async def get_source(self, namespace: str, surrogate_id: int | str) -> str | None:
        if namespace == "user":
            mapping = await self.registry.get_by_onebot_id(surrogate_id)
            return mapping.user_id if mapping is not None else None
        return await self._base_map.get_source(namespace, surrogate_id)

    async def get_surrogate(self, namespace: str, source_id: str) -> int | None:
        if namespace == "user":
            mapping = await self.registry.get_by_user_id(source_id)
            return mapping.onebot_id if mapping is not None else None
        return await self._base_map.get_surrogate(namespace, source_id)

    def begin_batch(self):
        return self._base_map.begin_batch()

    def set_message_window_size(self, message_window_size: Any) -> None:
        self._base_map.set_message_window_size(message_window_size)

    async def rebuild_message_window(self, *, force_compact: bool = False) -> dict[str, Any]:
        return await self._base_map.rebuild_message_window(force_compact=force_compact)

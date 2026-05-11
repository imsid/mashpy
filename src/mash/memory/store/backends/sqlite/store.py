"""Async SQLite-backed memory store implementation."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

import aiosqlite

from .....logging.events import inflate_logged_event
from ....search.types import SearchColumn
from ...protocol import MemoryStore


class SQLiteStore(MemoryStore):
    """Async SQLite-backed conversation store with signals."""

    _FTS_TABLE = "fts_turns"

    def __init__(self, path: Union[str, Path] = ":memory:") -> None:
        self._db_path = self._prepare_path(path)
        self._conn: aiosqlite.Connection | None = None
        self._open_lock = asyncio.Lock()
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        """Open the SQLite connection and initialize schema lazily."""
        if self._conn is not None:
            return

        async with self._open_lock:
            if self._conn is not None:
                return

            conn = await aiosqlite.connect(self._db_path)
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA busy_timeout = 5000")
            if self._db_path != ":memory:":
                try:
                    await conn.execute("PRAGMA journal_mode = WAL")
                except aiosqlite.Error:
                    pass
            self._conn = conn
            await self._init_schema()

    async def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    async def _get_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteStore is not open")
        assert self._conn is not None
        return self._conn

    async def _init_schema(self) -> None:
        conn = await self._get_conn()
        async with self._lock:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS turns (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    app_id TEXT NOT NULL DEFAULT 'default',
                    user_message TEXT NOT NULL,
                    agent_response TEXT NOT NULL,
                    session_total_tokens INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    turn_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    app_id TEXT NOT NULL DEFAULT 'default',
                    signal_name TEXT NOT NULL,
                    signal_value TEXT NOT NULL,
                    PRIMARY KEY (turn_id, signal_name),
                    FOREIGN KEY (turn_id) REFERENCES turns(turn_id)
                )
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_id TEXT NOT NULL,
                    session_id TEXT,
                    trace_id TEXT,
                    event_class TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_app ON turns(app_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signals_name ON signals(signal_name)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signals_app_session ON signals(app_id, session_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signals_app_name ON signals(app_id, signal_name)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_app_id ON logs(app_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_session_id ON logs(session_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_trace_id ON logs(trace_id)"
            )

            try:
                await conn.execute(
                    f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS {self._FTS_TABLE} USING fts5(
                        turn_id UNINDEXED,
                        session_id UNINDEXED,
                        user_message,
                        agent_response,
                        tokenize='unicode61'
                    )
                    """
                )
            except aiosqlite.OperationalError as exc:
                raise RuntimeError(
                    "SQLite FTS5 support is required for SQLiteStore keyword search"
                ) from exc

            turns_has_rows = (
                await self._fetchone_unlocked("SELECT 1 FROM turns LIMIT 1") is not None
            )
            fts_has_rows = (
                await self._fetchone_unlocked(
                    f"SELECT 1 FROM {self._FTS_TABLE} LIMIT 1"
                )
                is not None
            )
            if turns_has_rows and not fts_has_rows:
                await self._rebuild_turns_fts_index_unlocked()

            await conn.commit()

    async def save_logs(self, logs: List[Dict[str, Any]]) -> None:
        if not logs:
            return

        await self.open()
        rows: List[tuple[Any, ...]] = []
        for log in logs:
            payload = log.get("payload")
            rows.append(
                (
                    str(log["app_id"]),
                    log.get("session_id"),
                    log.get("trace_id"),
                    str(log["event_class"]),
                    str(log["event_type"]),
                    float(log["created_at"]),
                    json.dumps(
                        payload if isinstance(payload, dict) else {}, default=str
                    ),
                )
            )

        conn = await self._get_conn()
        async with self._lock:
            await conn.executemany(
                """
                INSERT INTO logs (
                    app_id,
                    session_id,
                    trace_id,
                    event_class,
                    event_type,
                    created_at,
                    payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            await conn.commit()

    async def get_logs(
        self,
        app_id: str,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        limit: Optional[int] = None,
        after_log_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        params: List[Any] = [app_id]
        filters = ["app_id = ?"]

        if session_id is not None:
            filters.append("session_id = ?")
            params.append(session_id)
        if trace_id is not None:
            filters.append("trace_id = ?")
            params.append(trace_id)
        if after_log_id is not None:
            filters.append("id > ?")
            params.append(int(after_log_id))

        where_clause = " AND ".join(filters)
        normalized_limit = None if limit is None else max(0, int(limit))
        if normalized_limit == 0:
            return []
        await self.open()

        if after_log_id is None and normalized_limit is not None:
            sql = f"""
                SELECT id, app_id, session_id, trace_id, event_class, event_type, created_at, payload
                FROM logs
                WHERE {where_clause}
                ORDER BY id DESC
                LIMIT ?
            """
            params.append(normalized_limit)
            reverse_results = True
        else:
            sql = f"""
                SELECT id, app_id, session_id, trace_id, event_class, event_type, created_at, payload
                FROM logs
                WHERE {where_clause}
                ORDER BY id ASC
            """
            if normalized_limit is not None:
                sql += "\nLIMIT ?"
                params.append(normalized_limit)
            reverse_results = False

        async with self._lock:
            rows = await self._fetchall_unlocked(sql, params)

        if reverse_results:
            rows = list(reversed(rows))

        return [
            inflate_logged_event(
                log_id=int(row["id"]),
                app_id=str(row["app_id"]),
                session_id=(
                    None if row["session_id"] is None else str(row["session_id"])
                ),
                trace_id=None if row["trace_id"] is None else str(row["trace_id"]),
                event_class=str(row["event_class"]),
                event_type=str(row["event_type"]),
                created_at=float(row["created_at"] or 0.0),
                payload=self._load_json_dict(row["payload"]),
            )
            for row in rows
        ]

    async def get_latest_log_trace(
        self,
        app_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        traces = await self.list_recent_log_traces(
            app_id=app_id,
            session_id=session_id,
            limit=1,
        )
        return traces[0] if traces else None

    async def list_recent_log_traces(
        self,
        app_id: str,
        session_id: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        normalized_limit = max(1, int(limit))
        await self.open()
        async with self._lock:
            rows = await self._fetchall_unlocked(
                """
                SELECT
                    trace_id,
                    session_id,
                    app_id,
                    MIN(created_at) AS started_at,
                    MAX(created_at) AS last_event_at,
                    COUNT(*) AS event_count
                FROM logs
                WHERE app_id = ?
                  AND session_id = ?
                  AND trace_id IS NOT NULL
                  AND TRIM(trace_id) != ''
                GROUP BY app_id, session_id, trace_id
                ORDER BY last_event_at DESC, started_at DESC, trace_id DESC
                LIMIT ?
                """,
                (app_id, session_id, normalized_limit),
            )

        return [
            {
                "trace_id": str(row["trace_id"]),
                "session_id": str(row["session_id"]),
                "app_id": str(row["app_id"]),
                "started_at": float(row["started_at"] or 0.0),
                "last_event_at": float(row["last_event_at"] or 0.0),
                "event_count": int(row["event_count"] or 0),
            }
            for row in rows
        ]

    async def save_turn(
        self,
        trace_id: str,
        session_id: str,
        app_id: str,
        user_message: str,
        agent_response: str,
        signals: Dict[str, Any],
        session_total_tokens: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        turn_id = trace_id
        timestamp = time.time()
        metadata_json = json.dumps(metadata or {})

        await self.open()
        conn = await self._get_conn()
        async with self._lock:
            await conn.execute(
                """
                INSERT INTO turns (
                    turn_id,
                    session_id,
                    app_id,
                    user_message,
                    agent_response,
                    session_total_tokens,
                    metadata,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session_id,
                    app_id,
                    user_message,
                    agent_response,
                    int(session_total_tokens),
                    metadata_json,
                    timestamp,
                ),
            )
            await conn.execute(
                f"""
                INSERT INTO {self._FTS_TABLE} (
                    turn_id,
                    session_id,
                    user_message,
                    agent_response
                )
                VALUES (?, ?, ?, ?)
                """,
                (turn_id, session_id, user_message, agent_response),
            )
            if signals:
                await conn.executemany(
                    """
                    INSERT INTO signals (
                        turn_id,
                        session_id,
                        app_id,
                        signal_name,
                        signal_value
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            turn_id,
                            session_id,
                            app_id,
                            signal_name,
                            json.dumps(signal_value),
                        )
                        for signal_name, signal_value in signals.items()
                    ],
                )
            await conn.commit()
        return turn_id

    async def get_turns(
        self,
        session_id: str,
        app_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        await self.open()
        params: list[Any] = [session_id, app_id]
        async with self._lock:
            if limit is None:
                rows = await self._fetchall_unlocked(
                    """
                    SELECT turn_id, user_message, agent_response, session_total_tokens, metadata, created_at
                    FROM turns
                    WHERE session_id = ? AND app_id = ?
                    ORDER BY created_at ASC
                    """,
                    params,
                )
            else:
                rows = await self._fetchall_unlocked(
                    """
                    SELECT turn_id, user_message, agent_response, session_total_tokens, metadata, created_at
                    FROM turns
                    WHERE session_id = ? AND app_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    [*params, max(0, int(limit))],
                )
                rows = list(reversed(rows))

            turn_ids = [str(row["turn_id"]) for row in rows]
            signals_by_turn = await self._get_signals_for_turn_ids_unlocked(turn_ids)

        turns: List[Dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            except json.JSONDecodeError:
                metadata = {}
            turns.append(
                {
                    "turn_id": str(row["turn_id"]),
                    "user_message": row["user_message"],
                    "agent_response": row["agent_response"],
                    "session_total_tokens": row["session_total_tokens"],
                    "signals": signals_by_turn.get(str(row["turn_id"]), {}),
                    "metadata": metadata,
                    "created_at": row["created_at"],
                }
            )
        return turns

    async def get_session_signals(
        self,
        session_id: str,
        app_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        await self.open()
        params: list[Any] = [session_id, app_id]
        async with self._lock:
            if limit is None:
                rows = await self._fetchall_unlocked(
                    """
                    SELECT turn_id, created_at
                    FROM turns
                    WHERE session_id = ? AND app_id = ?
                    ORDER BY created_at ASC
                    """,
                    params,
                )
            else:
                rows = await self._fetchall_unlocked(
                    """
                    SELECT turn_id, created_at
                    FROM turns
                    WHERE session_id = ? AND app_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    [*params, max(0, int(limit))],
                )
                rows = list(reversed(rows))

            turn_ids = [str(row["turn_id"]) for row in rows]
            signals_by_turn = await self._get_signals_for_turn_ids_unlocked(turn_ids)

        return [
            {
                "turn_id": str(row["turn_id"]),
                "created_at": float(row["created_at"] or 0.0),
                "signals": signals_by_turn.get(str(row["turn_id"]), {}),
            }
            for row in rows
        ]

    async def list_sessions(self, app_id: str) -> List[Dict[str, Any]]:
        await self.open()
        async with self._lock:
            rows = await self._fetchall_unlocked(
                """
                SELECT
                    session_id,
                    COUNT(*) AS turn_count,
                    MAX(created_at) AS last_activity_at,
                    MAX(session_total_tokens) AS session_total_tokens
                FROM turns
                WHERE app_id = ?
                GROUP BY session_id
                ORDER BY last_activity_at DESC, session_id ASC
                """,
                (app_id,),
            )

        return [
            {
                "session_id": str(row["session_id"]),
                "turn_count": int(row["turn_count"] or 0),
                "last_activity_at": float(row["last_activity_at"] or 0.0),
                "session_total_tokens": int(row["session_total_tokens"] or 0),
            }
            for row in rows
        ]

    async def get_latest_session(self, app_id: str) -> Optional[Dict[str, Any]]:
        sessions = await self.list_sessions(app_id=app_id)
        return sessions[0] if sessions else None

    async def get_latest_trace(
        self,
        app_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        traces = await self.list_recent_traces(
            app_id=app_id,
            session_id=session_id,
            limit=1,
        )
        return traces[0] if traces else None

    async def list_recent_traces(
        self,
        app_id: str,
        session_id: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        normalized_limit = max(1, int(limit))
        async with self._lock:
            rows = await self._fetchall_unlocked(
                """
                SELECT
                    turn_id,
                    session_id,
                    user_message,
                    agent_response,
                    metadata,
                    created_at
                FROM turns
                WHERE app_id = ? AND session_id = ?
                ORDER BY created_at DESC, turn_id DESC
                LIMIT ?
                """,
                (app_id, session_id, normalized_limit),
            )

        traces: List[Dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata"]) if row["metadata"] else {}
            except json.JSONDecodeError:
                metadata = {}
            traces.append(
                {
                    "trace_id": str(row["turn_id"]),
                    "session_id": str(row["session_id"]),
                    "user_message": (
                        "" if row["user_message"] is None else str(row["user_message"])
                    ),
                    "agent_response": (
                        ""
                        if row["agent_response"] is None
                        else str(row["agent_response"])
                    ),
                    "metadata": metadata if isinstance(metadata, dict) else {},
                    "created_at": float(row["created_at"] or 0.0),
                }
            )
        return traces

    async def get_turn_by_ids(
        self,
        pairs: List[Dict[str, str]],
        app_id: str,
    ) -> Optional[List[Dict[str, Any]]]:
        await self.open()
        if not pairs:
            return None

        requested_keys: List[tuple[str, str]] = []
        unique_keys: List[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for pair in pairs:
            session_id = str(pair.get("session_id", "")).strip()
            turn_id = str(pair.get("turn_id", "")).strip()
            if not session_id or not turn_id:
                continue
            key = (session_id, turn_id)
            requested_keys.append(key)
            if key not in seen:
                unique_keys.append(key)
                seen.add(key)

        if not requested_keys:
            return None

        where_clauses: List[str] = []
        params: List[Any] = []
        for session_id, turn_id in unique_keys:
            where_clauses.append("(session_id = ? AND turn_id = ?)")
            params.extend([session_id, turn_id])

        params.append(app_id)

        sql = f"""
            SELECT turn_id, session_id, user_message, agent_response
            FROM turns
            WHERE ({' OR '.join(where_clauses)}) AND app_id = ?
        """

        async with self._lock:
            rows = await self._fetchall_unlocked(sql, params)

        by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
        for row in rows:
            key = (str(row["session_id"]), str(row["turn_id"]))
            by_key[key] = {
                "turn_id": str(row["turn_id"]),
                "session_id": str(row["session_id"]),
                "user_message": (
                    "" if row["user_message"] is None else str(row["user_message"])
                ),
                "agent_response": (
                    "" if row["agent_response"] is None else str(row["agent_response"])
                ),
            }

        results = [by_key[key] for key in requested_keys if key in by_key]
        return results or None

    async def keyword_search(
        self,
        column: SearchColumn,
        query_term: str,
        limit: int,
        session_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        normalized_limit = max(0, int(limit))
        if normalized_limit <= 0:
            return []
        await self.open()

        match_query = self._build_keyword_match_query(column, query_term)
        if not match_query:
            return []

        column_name = self._validated_search_column(column)
        params: List[Any] = [match_query]
        filters: List[str] = []
        if session_id is not None:
            filters.append(f"{self._FTS_TABLE}.session_id = ?")
            params.append(session_id)
        if app_id is not None:
            filters.append("t.app_id = ?")
            params.append(app_id)

        where_filters = ""
        if filters:
            where_filters = " AND " + " AND ".join(filters)

        sql = f"""
            SELECT
                t.turn_id,
                t.session_id,
                t.{column_name} AS preview,
                bm25({self._FTS_TABLE}) AS bm25_score
            FROM {self._FTS_TABLE}
            JOIN turns AS t ON t.turn_id = {self._FTS_TABLE}.turn_id
            WHERE {self._FTS_TABLE} MATCH ?{where_filters}
            ORDER BY bm25_score ASC, t.created_at DESC, t.turn_id ASC
            LIMIT ?
        """
        params.append(normalized_limit)

        try:
            async with self._lock:
                rows = await self._fetchall_unlocked(sql, params)
        except aiosqlite.OperationalError as exc:
            raise RuntimeError(
                "SQLite FTS5 support is required for SQLiteStore keyword search"
            ) from exc

        return [
            {
                "turn_id": str(row["turn_id"]),
                "session_id": str(row["session_id"]),
                "score": 1.0 / (1.0 + rank),
                "preview": "" if row["preview"] is None else str(row["preview"]),
            }
            for rank, row in enumerate(rows, start=1)
        ]

    async def semantic_search(
        self,
        column: SearchColumn,
        query_term: str,
        query_embedding: Optional[List[float]],
        limit: int,
        session_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        del column, query_term, query_embedding, limit, session_id, app_id
        raise NotImplementedError("SQLiteStore.semantic_search is not implemented yet")

    async def _fetchall_unlocked(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[aiosqlite.Row]:
        conn = await self._get_conn()
        cursor = await conn.execute(sql, params)
        try:
            return list(await cursor.fetchall())
        finally:
            await cursor.close()

    async def _fetchone_unlocked(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> aiosqlite.Row | None:
        conn = await self._get_conn()
        cursor = await conn.execute(sql, params)
        try:
            return await cursor.fetchone()
        finally:
            await cursor.close()

    async def _get_signals_for_turn_ids_unlocked(
        self,
        turn_ids: Iterable[str],
    ) -> Dict[str, Dict[str, Any]]:
        normalized_turn_ids = [str(turn_id) for turn_id in turn_ids if str(turn_id)]
        if not normalized_turn_ids:
            return {}

        placeholders = ", ".join("?" for _ in normalized_turn_ids)
        rows = await self._fetchall_unlocked(
            f"""
            SELECT turn_id, signal_name, signal_value
            FROM signals
            WHERE turn_id IN ({placeholders})
            """,
            normalized_turn_ids,
        )

        signals_by_turn: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            turn_id = str(row["turn_id"])
            signals = signals_by_turn.setdefault(turn_id, {})
            try:
                signals[str(row["signal_name"])] = json.loads(row["signal_value"])
            except json.JSONDecodeError:
                signals[str(row["signal_name"])] = row["signal_value"]
        return signals_by_turn

    async def _rebuild_turns_fts_index_unlocked(self) -> None:
        conn = await self._get_conn()
        await conn.execute(f"DELETE FROM {self._FTS_TABLE}")
        await conn.execute(
            f"""
            INSERT INTO {self._FTS_TABLE} (
                turn_id,
                session_id,
                user_message,
                agent_response
            )
            SELECT turn_id, session_id, user_message, agent_response
            FROM turns
            """
        )

    @staticmethod
    def _load_json_dict(value: Any) -> Dict[str, Any]:
        if not isinstance(value, str):
            return {}
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _validated_search_column(column: SearchColumn) -> str:
        if column not in ("user_message", "agent_response"):
            raise ValueError(f"Unsupported search column: {column}")
        return column

    @classmethod
    def _build_keyword_match_query(
        cls,
        column: SearchColumn,
        query_term: str,
    ) -> str:
        column_name = cls._validated_search_column(column)
        tokens = [token for token in str(query_term).split() if token]
        if not tokens:
            return ""
        escaped_tokens = [
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens
        ]
        token_expr = " AND ".join(escaped_tokens)
        return f"{column_name} : ({token_expr})"

    @staticmethod
    def _prepare_path(path: Union[str, Path]) -> str:
        raw = str(path) if isinstance(path, Path) else path
        if raw == ":memory:":
            return raw
        location = Path(raw).expanduser()
        location.parent.mkdir(parents=True, exist_ok=True)
        return str(location)

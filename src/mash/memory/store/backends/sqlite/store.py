"""SQLite-backed memory store implementation."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .....logging.events import inflate_logged_event
from ....search.types import SearchColumn
from ...protocol import MemoryStore


class SQLiteStore(MemoryStore):
    """SQLite-backed conversation store with signals."""

    _FTS_TABLE = "fts_turns"

    def __init__(self, path: Union[str, Path] = ":memory:") -> None:
        """Initialize SQLite store.

        Args:
            path: Path to SQLite database file.
        """
        self._db_path = self._prepare_path(path)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        """Initialize database schema."""
        with self._lock:
            # Turns table (with app_id for isolation)
            self._conn.execute(
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

            self._conn.execute(
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

            # Preferences table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS preferences (
                    app_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (app_id, session_id)
                )
                """
            )

            # App-specific data table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_data (
                    app_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (app_id, session_id, key)
                )
                """
            )

            self._conn.execute(
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

            # Indexes for faster queries
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_app ON turns(app_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signals_name ON signals(signal_name)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signals_app_session ON signals(app_id, session_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signals_app_name ON signals(app_id, signal_name)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_app_data_session ON app_data(app_id, session_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_app_id ON logs(app_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_session_id ON logs(session_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_trace_id ON logs(trace_id)"
            )

            try:
                self._conn.execute(
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
            except sqlite3.OperationalError as exc:
                raise RuntimeError(
                    "SQLite FTS5 support is required for SQLiteStore keyword search"
                ) from exc

            turns_has_rows = (
                self._conn.execute("SELECT 1 FROM turns LIMIT 1").fetchone() is not None
            )
            fts_has_rows = (
                self._conn.execute(
                    f"SELECT 1 FROM {self._FTS_TABLE} LIMIT 1"
                ).fetchone()
                is not None
            )
            if turns_has_rows and not fts_has_rows:
                self._rebuild_turns_fts_index_locked()

            self._conn.commit()

    def save_logs(self, logs: List[Dict[str, Any]]) -> None:
        """Persist one or more structured log records."""
        if not logs:
            return

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
                    json.dumps(payload if isinstance(payload, dict) else {}, default=str),
                )
            )

        with self._lock:
            self._conn.executemany(
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
            self._conn.commit()

    def get_logs(
        self,
        app_id: str,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        limit: Optional[int] = None,
        after_log_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return structured log records for one app/session/trace scope."""
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

        if after_log_id is None and normalized_limit is not None:
            sql = f"""
                SELECT
                    id,
                    app_id,
                    session_id,
                    trace_id,
                    event_class,
                    event_type,
                    created_at,
                    payload
                FROM logs
                WHERE {where_clause}
                ORDER BY id DESC
                LIMIT ?
            """
            params.append(normalized_limit)
            reverse_results = True
        else:
            sql = f"""
                SELECT
                    id,
                    app_id,
                    session_id,
                    trace_id,
                    event_class,
                    event_type,
                    created_at,
                    payload
                FROM logs
                WHERE {where_clause}
                ORDER BY id ASC
            """
            if normalized_limit is not None:
                sql += "\nLIMIT ?"
                params.append(normalized_limit)
            reverse_results = False

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        if reverse_results:
            rows = list(reversed(rows))

        events: List[Dict[str, Any]] = []
        for (
            log_id,
            found_app_id,
            found_session_id,
            found_trace_id,
            event_class,
            event_type,
            created_at,
            payload_json,
        ) in rows:
            events.append(
                inflate_logged_event(
                    log_id=int(log_id),
                    app_id=str(found_app_id),
                    session_id=None if found_session_id is None else str(found_session_id),
                    trace_id=None if found_trace_id is None else str(found_trace_id),
                    event_class=str(event_class),
                    event_type=str(event_type),
                    created_at=float(created_at or 0.0),
                    payload=self._load_json_dict(payload_json),
                )
            )
        return events

    def get_latest_log_trace(
        self,
        app_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the latest trace summary from persisted logs."""
        traces = self.list_recent_log_traces(app_id=app_id, session_id=session_id, limit=1)
        if not traces:
            return None
        return traces[0]

    def list_recent_log_traces(
        self,
        app_id: str,
        session_id: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """List recent trace summaries from persisted logs."""
        normalized_limit = max(1, int(limit))
        with self._lock:
            rows = self._conn.execute(
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
            ).fetchall()

        traces: List[Dict[str, Any]] = []
        for (
            found_trace_id,
            found_session_id,
            found_app_id,
            started_at,
            last_event_at,
            event_count,
        ) in rows:
            traces.append(
                {
                    "trace_id": str(found_trace_id),
                    "session_id": str(found_session_id),
                    "app_id": str(found_app_id),
                    "started_at": float(started_at or 0.0),
                    "last_event_at": float(last_event_at or 0.0),
                    "event_count": int(event_count or 0),
                }
            )
        return traces

    def save_turn(
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
        """Save a conversation turn with signals."""
        turn_id = trace_id
        timestamp = time.time()

        # Serialize metadata
        metadata_json = json.dumps(metadata or {})

        with self._lock:
            # Save turn
            self._conn.execute(
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

            self._conn.execute(
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

            # Save signals
            for signal_name, signal_value in signals.items():
                self._conn.execute(
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
                    (
                        turn_id,
                        session_id,
                        app_id,
                        signal_name,
                        json.dumps(signal_value),
                    ),
                )

            self._conn.commit()

        return turn_id

    def get_turns(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Get conversation turns for a session."""
        with self._lock:
            if limit is None:
                rows = self._conn.execute(
                    """
                    SELECT turn_id, user_message, agent_response, session_total_tokens, metadata, created_at
                    FROM turns
                    WHERE session_id = ?
                    ORDER BY created_at ASC
                    """,
                    (session_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT turn_id, user_message, agent_response, session_total_tokens, metadata, created_at
                    FROM turns
                    WHERE session_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (session_id, max(0, int(limit))),
                ).fetchall()
                rows = list(reversed(rows))

        turns = []
        for (
            turn_id,
            user_msg,
            agent_resp,
            session_total_tokens,
            metadata_json,
            created_at,
        ) in rows:
            # Get signals for this turn
            signals = self._get_signals_for_turn(turn_id)

            # Parse metadata
            try:
                metadata = json.loads(metadata_json) if metadata_json else {}
            except json.JSONDecodeError:
                metadata = {}

            turns.append(
                {
                    "turn_id": turn_id,
                    "user_message": user_msg,
                    "agent_response": agent_resp,
                    "session_total_tokens": session_total_tokens,
                    "signals": signals,
                    "metadata": metadata,
                    "created_at": created_at,
                }
            )

        return turns

    def list_sessions(self, app_id: str) -> List[Dict[str, Any]]:
        """List persisted sessions for one application."""
        with self._lock:
            rows = self._conn.execute(
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
            ).fetchall()

        sessions: List[Dict[str, Any]] = []
        for session_id, turn_count, last_activity_at, session_total_tokens in rows:
            sessions.append(
                {
                    "session_id": str(session_id),
                    "turn_count": int(turn_count or 0),
                    "last_activity_at": float(last_activity_at or 0.0),
                    "session_total_tokens": int(session_total_tokens or 0),
                }
            )
        return sessions

    def get_latest_session(self, app_id: str) -> Optional[Dict[str, Any]]:
        """Return the most recent persisted session for one application."""
        sessions = self.list_sessions(app_id=app_id)
        if not sessions:
            return None
        return sessions[0]

    def get_latest_trace(
        self,
        app_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent trace for a session in one application."""
        traces = self.list_recent_traces(
            app_id=app_id,
            session_id=session_id,
            limit=1,
        )
        if not traces:
            return None
        return traces[0]

    def list_recent_traces(
        self,
        app_id: str,
        session_id: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """List recent traces for a session in one application."""
        normalized_limit = max(1, int(limit))
        with self._lock:
            rows = self._conn.execute(
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
            ).fetchall()

        traces: List[Dict[str, Any]] = []
        for (
            turn_id,
            found_session_id,
            user_message,
            agent_response,
            metadata_json,
            created_at,
        ) in rows:
            try:
                metadata = json.loads(metadata_json) if metadata_json else {}
            except json.JSONDecodeError:
                metadata = {}

            traces.append(
                {
                    "trace_id": str(turn_id),
                    "session_id": str(found_session_id),
                    "user_message": "" if user_message is None else str(user_message),
                    "agent_response": (
                        "" if agent_response is None else str(agent_response)
                    ),
                    "metadata": metadata if isinstance(metadata, dict) else {},
                    "created_at": float(created_at or 0.0),
                }
            )
        return traces

    def get_turn_by_ids(
        self,
        pairs: List[Dict[str, str]],
    ) -> Optional[List[Dict[str, Any]]]:
        """Get turns by exact session/turn identifier pairs in one DB call."""
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

        sql = f"""
            SELECT turn_id, session_id, user_message, agent_response
            FROM turns
            WHERE {' OR '.join(where_clauses)}
        """

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()

        by_key: Dict[tuple[str, str], Dict[str, Any]] = {}
        for found_turn_id, found_session_id, user_message, agent_response in rows:
            key = (str(found_session_id), str(found_turn_id))
            by_key[key] = {
                "turn_id": str(found_turn_id),
                "session_id": str(found_session_id),
                "user_message": "" if user_message is None else str(user_message),
                "agent_response": "" if agent_response is None else str(agent_response),
            }

        results = [by_key[key] for key in requested_keys if key in by_key]
        return results or None

    @staticmethod
    def _load_json_dict(value: Any) -> Dict[str, Any]:
        if not isinstance(value, str):
            return {}
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _get_signals_for_turn(self, turn_id: str) -> Dict[str, Any]:
        """Get signals for a specific turn."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT signal_name, signal_value
                FROM signals
                WHERE turn_id = ?
                """,
                (turn_id,),
            ).fetchall()

        signals: Dict[str, Any] = {}
        for name, value_json in rows:
            try:
                signals[str(name)] = json.loads(value_json)
            except json.JSONDecodeError:
                signals[str(name)] = value_json
        return signals

    def keyword_search(
        self,
        column: SearchColumn,
        query_term: str,
        limit: int,
        session_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search turns by keyword using SQLite FTS5 BM25 ranking."""
        normalized_limit = max(0, int(limit))
        if normalized_limit <= 0:
            return []

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
            with self._lock:
                rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "SQLite FTS5 support is required for SQLiteStore keyword search"
            ) from exc

        hits: List[Dict[str, Any]] = []
        for rank, (turn_id, hit_session_id, preview, _bm25_score) in enumerate(
            rows, start=1
        ):
            hits.append(
                {
                    "turn_id": turn_id,
                    "session_id": str(hit_session_id),
                    "score": 1.0 / (1.0 + rank),
                    "preview": "" if preview is None else str(preview),
                }
            )
        return hits

    def semantic_search(
        self,
        column: SearchColumn,
        query_term: str,
        query_embedding: Optional[List[float]],
        limit: int,
        session_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Phase 1 contract only; SQLite-backed implementation comes next."""
        raise NotImplementedError("SQLiteStore.semantic_search is not implemented yet")

    def _rebuild_turns_fts_index_locked(self) -> None:
        """Rebuild the FTS index from the canonical turns table.

        Caller must hold ``self._lock``.
        """
        self._conn.execute(f"DELETE FROM {self._FTS_TABLE}")
        self._conn.execute(
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
    def _validated_search_column(column: SearchColumn) -> str:
        """Return a whitelisted turns column name for search."""
        if column not in ("user_message", "agent_response"):
            raise ValueError(f"Unsupported search column: {column}")
        return column

    @classmethod
    def _build_keyword_match_query(
        cls,
        column: SearchColumn,
        query_term: str,
    ) -> str:
        """Build a column-scoped FTS5 MATCH expression with token-AND semantics."""
        column_name = cls._validated_search_column(column)
        tokens = [token for token in str(query_term).split() if token]
        if not tokens:
            return ""
        escaped_tokens = [
            f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens
        ]
        token_expr = " AND ".join(escaped_tokens)
        return f"{column_name} : ({token_expr})"

    def get_preferences(
        self,
        app_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get user preferences for app and session."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT value
                FROM preferences
                WHERE app_id = ? AND session_id = ?
                """,
                (app_id, session_id),
            ).fetchone()

        if row is None:
            return None

        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return {}

    def get_latest_preferences(
        self,
        app_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get latest user preferences for app."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT value
                FROM preferences
                WHERE app_id = ?
                ORDER BY updated_at DESC
                """,
                (app_id,),
            ).fetchone()

        if row is None:
            return None

        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return {}

    def set_preferences(
        self,
        app_id: str,
        session_id: str,
        preferences: Dict[str, Any],
    ) -> None:
        """Set user preferences for app and session."""
        timestamp = time.time()
        preferences_json = json.dumps(preferences)

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO preferences (app_id, session_id, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(app_id, session_id)
                DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (app_id, session_id, preferences_json, timestamp),
            )
            self._conn.commit()

    def get_app_data(
        self,
        app_id: str,
        session_id: str,
        key: str,
    ) -> Optional[Any]:
        """Get app-specific data by key."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT value
                FROM app_data
                WHERE app_id = ? AND session_id = ? AND key = ?
                """,
                (app_id, session_id, key),
            ).fetchone()

        if row is None:
            return None

        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return row[0]

    def set_app_data(
        self,
        app_id: str,
        session_id: str,
        key: str,
        value: Any,
    ) -> None:
        """Set app-specific data by key."""
        timestamp = time.time()

        try:
            value_json = json.dumps(value)
        except TypeError:
            value_json = json.dumps(str(value))

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO app_data (app_id, session_id, key, value, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(app_id, session_id, key)
                DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (app_id, session_id, key, value_json, timestamp),
            )
            self._conn.commit()

    def list_app_data(
        self,
        app_id: str,
        session_id: str,
    ) -> List[Dict[str, Any]]:
        """List all app-specific data for session."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT key, value, updated_at
                FROM app_data
                WHERE app_id = ? AND session_id = ?
                ORDER BY updated_at DESC, key ASC
                """,
                (app_id, session_id),
            ).fetchall()

        entries: List[Dict[str, Any]] = []
        for key, value_json, updated_at in rows:
            try:
                value = json.loads(value_json)
            except json.JSONDecodeError:
                value = value_json

            entries.append(
                {
                    "key": key,
                    "value": value,
                    "updated_at": updated_at,
                }
            )

        return entries

    def delete_app_data(
        self,
        app_id: str,
        session_id: str,
        key: str,
    ) -> bool:
        """Delete app-specific data by key."""
        with self._lock:
            cursor = self._conn.execute(
                """
                DELETE FROM app_data
                WHERE app_id = ? AND session_id = ? AND key = ?
                """,
                (app_id, session_id, key),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _prepare_path(path: Union[str, Path]) -> str:
        """Normalize and ensure directories exist for the DB path."""
        if isinstance(path, Path):
            raw = str(path)
        else:
            raw = path

        if raw == ":memory:":
            return raw

        location = Path(raw).expanduser()
        location.parent.mkdir(parents=True, exist_ok=True)
        return str(location)

"""Conversation storage with signals for feedback loops."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Union

from .search.types import SearchColumn


class MemoryStore(Protocol):
    """Protocol for conversation storage."""

    def save_turn(
        self,
        trace_id: str,
        session_id: str,
        user_message: str,
        agent_response: str,
        signals: Dict[str, Any],
        session_total_tokens: int,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Save a conversation turn with signals.

        Args:
            trace_id: Trace ID for this turn (used as turn_id).
            session_id: Session identifier.
            user_message: User's message.
            agent_response: Agent's response.
            signals: Collected signals for this turn.
            session_total_tokens: Total tokens used in this session after this turn.
            metadata: Optional metadata.

        Returns:
            Turn ID.
        """
        ...

    def get_turns(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Get conversation turns for a session.

        Args:
            session_id: Session identifier.
            limit: Maximum number of turns to return.

        Returns:
            List of turns.
        """
        ...

    def keyword_search(
        self,
        column: SearchColumn,
        query_term: str,
        limit: int,
        session_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search turns by keyword in a single column.

        Returns:
            List of hits ordered by descending score in [0, 1].
            Each hit must include: turn_id, session_id, score, preview.
        """
        ...

    def semantic_search(
        self,
        column: SearchColumn,
        query_term: str,
        query_embedding: Optional[List[float]],
        limit: int,
        session_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search turns semantically in a single column.

        Returns:
            List of hits ordered by descending score in [0, 1].
            Each hit must include: turn_id, session_id, score, preview.
        """
        ...

    def get_preferences(
        self,
        app_id: str,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get user preferences for app and session.

        Args:
            app_id: Application identifier.
            session_id: Session identifier.

        Returns:
            User preferences as dictionary, or None if not set.
        """
        ...

    def get_latest_preferences(
        self,
        app_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get latest user preferences for app.

        Args:
            app_id: Application identifier.

        Returns:
            User preferences as dictionary, or None if not set.
        """
        ...

    def set_preferences(
        self,
        app_id: str,
        session_id: str,
        preferences: Dict[str, Any],
    ) -> None:
        """Set user preferences for app and session.

        Args:
            app_id: Application identifier.
            session_id: Session identifier.
            preferences: User preferences dictionary.
        """
        ...

    def get_app_data(
        self,
        app_id: str,
        session_id: str,
        key: str,
    ) -> Optional[Any]:
        """Get app-specific data by key.

        Args:
            app_id: Application identifier.
            session_id: Session identifier.
            key: Data key.

        Returns:
            Data value, or None if key doesn't exist.
        """
        ...

    def set_app_data(
        self,
        app_id: str,
        session_id: str,
        key: str,
        value: Any,
    ) -> None:
        """Set app-specific data by key.

        Args:
            app_id: Application identifier.
            session_id: Session identifier.
            key: Data key.
            value: Data value (must be JSON-serializable).
        """
        ...

    def list_app_data(
        self,
        app_id: str,
        session_id: str,
    ) -> List[Dict[str, Any]]:
        """List all app-specific data for session.

        Args:
            app_id: Application identifier.
            session_id: Session identifier.

        Returns:
            List of dictionaries with 'key', 'value', and 'updated_at' fields.
        """
        ...

    def delete_app_data(
        self,
        app_id: str,
        session_id: str,
        key: str,
    ) -> bool:
        """Delete app-specific data by key.

        Args:
            app_id: Application identifier.
            session_id: Session identifier.
            key: Data key.

        Returns:
            True if data was deleted, False if key didn't exist.
        """
        ...


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

            # Signals table
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    turn_id TEXT NOT NULL,
                    signal_name TEXT NOT NULL,
                    signal_value REAL NOT NULL,
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
                "CREATE INDEX IF NOT EXISTS idx_app_data_session ON app_data(app_id, session_id)"
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

    def save_turn(
        self,
        trace_id: str,
        session_id: str,
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
                INSERT INTO turns (turn_id, session_id, user_message, agent_response,
                                   session_total_tokens, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session_id,
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
                try:
                    # Convert to float
                    value = float(signal_value)
                    self._conn.execute(
                        """
                        INSERT INTO signals (turn_id, signal_name, signal_value)
                        VALUES (?, ?, ?)
                        """,
                        (turn_id, signal_name, value),
                    )
                except (ValueError, TypeError):
                    # Skip non-numeric signals
                    pass

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

    def _get_signals_for_turn(self, turn_id: str) -> Dict[str, float]:
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

        return {name: value for name, value in rows}

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
        escaped_tokens = [f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens]
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

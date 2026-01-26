"""Conversation storage with signals for feedback loops."""

from __future__ import annotations

import json
import pickle
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Union


class ConversationStore(Protocol):
    """Protocol for conversation storage."""

    def save_turn(
        self,
        session_id: str,
        user_message: str,
        agent_response: str,
        signals: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Save a conversation turn with signals.

        Args:
            session_id: Session identifier.
            user_message: User's message.
            agent_response: Agent's response.
            signals: Collected signals for this turn.
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

    def search_by_similarity(
        self,
        query_embedding: List[float],
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search for similar conversations by embedding.

        Args:
            query_embedding: Query embedding vector.
            limit: Maximum number of results.

        Returns:
            List of similar turns.
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


class SQLiteStore(ConversationStore):
    """SQLite-backed conversation store with signals."""

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
                    embedding BLOB,
                    metadata TEXT,
                    created_at REAL NOT NULL
                )
                """
            )

            # Migrate existing turns table if app_id column doesn't exist
            self._migrate_turns_table()

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

            self._conn.commit()

    def _migrate_turns_table(self) -> None:
        """Migrate existing turns table to add app_id column if needed."""
        # Check if app_id column exists
        cursor = self._conn.execute("PRAGMA table_info(turns)")
        columns = [row[1] for row in cursor.fetchall()]

        if "app_id" not in columns:
            # Add app_id column with default value
            self._conn.execute(
                "ALTER TABLE turns ADD COLUMN app_id TEXT NOT NULL DEFAULT 'default'"
            )
            # Rebuild index
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_turns_app ON turns(app_id)"
            )

    def save_turn(
        self,
        session_id: str,
        user_message: str,
        agent_response: str,
        signals: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Save a conversation turn with signals."""
        turn_id = f"{session_id}_{int(time.time() * 1000)}"
        timestamp = time.time()

        # Generate embedding (placeholder - would use real embedding model)
        embedding = self._generate_embedding(user_message)
        embedding_blob = pickle.dumps(embedding) if embedding else None

        # Serialize metadata
        metadata_json = json.dumps(metadata or {})

        with self._lock:
            # Save turn
            self._conn.execute(
                """
                INSERT INTO turns (turn_id, session_id, user_message, agent_response,
                                   embedding, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    session_id,
                    user_message,
                    agent_response,
                    embedding_blob,
                    metadata_json,
                    timestamp,
                ),
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
                    SELECT turn_id, user_message, agent_response, metadata, created_at
                    FROM turns
                    WHERE session_id = ?
                    ORDER BY created_at ASC
                    """,
                    (session_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT turn_id, user_message, agent_response, metadata, created_at
                    FROM turns
                    WHERE session_id = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (session_id, max(0, int(limit))),
                ).fetchall()
                rows = list(reversed(rows))

        turns = []
        for turn_id, user_msg, agent_resp, metadata_json, created_at in rows:
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

    def search_by_similarity(
        self,
        query_embedding: List[float],
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search for similar conversations by embedding.

        Note: This is a simplified implementation. A production system
        would use a vector database or more efficient similarity search.
        """
        # Get all turns with embeddings
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT turn_id, session_id, user_message, agent_response,
                       embedding, metadata, created_at
                FROM turns
                WHERE embedding IS NOT NULL
                """
            ).fetchall()

        # Calculate similarity scores
        results = []
        for (
            turn_id,
            session_id,
            user_msg,
            agent_resp,
            embedding_blob,
            metadata_json,
            created_at,
        ) in rows:
            # Deserialize embedding
            embedding = pickle.loads(embedding_blob) if embedding_blob else None
            if not embedding:
                continue

            # Calculate cosine similarity
            similarity = self._cosine_similarity(query_embedding, embedding)

            # Get signals
            signals = self._get_signals_for_turn(turn_id)

            # Parse metadata
            try:
                metadata = json.loads(metadata_json) if metadata_json else {}
            except json.JSONDecodeError:
                metadata = {}

            results.append(
                {
                    "turn_id": turn_id,
                    "session_id": session_id,
                    "user_message": user_msg,
                    "agent_response": agent_resp,
                    "signals": signals,
                    "metadata": metadata,
                    "similarity": similarity,
                    "created_at": created_at,
                }
            )

        # Sort by similarity and return top results
        results.sort(key=lambda x: x["similarity"], reverse=True)
        return results[:limit]

    def _generate_embedding(self, text: str) -> Optional[List[float]]:
        """Generate embedding for text.

        This is a placeholder. In production, use a real embedding model
        like OpenAI embeddings or sentence transformers.
        """
        # Placeholder: return None for now
        # In production: call embedding API or model
        return []

    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        if not vec1 or not vec2 or len(vec1) != len(vec2):
            return 0.0

        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        magnitude1 = sum(a * a for a in vec1) ** 0.5
        magnitude2 = sum(b * b for b in vec2) ** 0.5

        if magnitude1 == 0 or magnitude2 == 0:
            return 0.0

        return dot_product / (magnitude1 * magnitude2)

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

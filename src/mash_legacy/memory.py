"""SQLite-backed local memory implementation."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Union


class Memory(Protocol):
    """Protocol for local persistence providers."""

    def record_conversation(
        self,
        app_id: str,
        session_id: str,
        role: str,
        content: str,
        *,
        created_at: Optional[float] = None,
    ) -> None:
        """Persist a conversation message for the session."""
        raise NotImplementedError

    def get_conversation(
        self,
        app_id: str,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return conversation messages ordered by timestamp."""
        raise NotImplementedError

    def get_preferences(self, app_id: str, session_id: str) -> Optional[Any]:
        """Return stored preferences for the session."""
        raise NotImplementedError

    def set_preferences(self, app_id: str, session_id: str, value: Any) -> None:
        """Persist preferences for the session."""
        raise NotImplementedError

    def get_app_data(self, app_id: str, session_id: str, key: str) -> Optional[Any]:
        """Return stored app-specific data for a key."""
        raise NotImplementedError

    def set_app_data(self, app_id: str, session_id: str, key: str, value: Any) -> None:
        """Persist app-specific data for a key."""
        raise NotImplementedError

    def list_app_data(self, app_id: str, session_id: str) -> List[Dict[str, Any]]:
        """Return all app-specific data entries for a session."""
        raise NotImplementedError


class SqliteMemory(Memory):
    """Simple SQLite key/value store with optional TTL support."""

    def __init__(self, path: Union[str, Path] = ":memory:") -> None:
        self._db_path = self._prepare_path(path)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._lock = threading.Lock()

        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
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
            self._conn.commit()

    def record_conversation(
        self,
        app_id: str,
        session_id: str,
        role: str,
        content: str,
        *,
        created_at: Optional[float] = None,
    ) -> None:
        """Store a conversation message with a timestamp."""

        timestamp = created_at if created_at is not None else time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO conversations (app_id, session_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (app_id, session_id, role, content, timestamp),
            )
            self._conn.commit()

    def get_conversation(
        self,
        app_id: str,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return conversation messages ordered by timestamp."""

        with self._lock:
            if limit is None:
                rows = self._conn.execute(
                    """
                    SELECT role, content, created_at
                    FROM conversations
                    WHERE app_id = ? AND session_id = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (app_id, session_id),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT role, content, created_at
                    FROM conversations
                    WHERE app_id = ? AND session_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (app_id, session_id, max(0, int(limit))),
                ).fetchall()
                rows.reverse()
        return [
            {"role": role, "content": content, "created_at": created_at}
            for role, content, created_at in rows
        ]

    def get_preferences(self, app_id: str, session_id: str) -> Optional[Any]:
        """Return stored preferences for the session."""

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
            return row[0]

    def set_preferences(self, app_id: str, session_id: str, value: Any) -> None:
        """Persist preferences for the session."""

        timestamp = time.time()
        try:
            payload = json.dumps(value)
        except TypeError:
            payload = json.dumps(str(value))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO preferences (app_id, session_id, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(app_id, session_id)
                DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (app_id, session_id, payload, timestamp),
            )
            self._conn.commit()

    def get_app_data(self, app_id: str, session_id: str, key: str) -> Optional[Any]:
        """Return stored app-specific data for a key."""

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

    def set_app_data(self, app_id: str, session_id: str, key: str, value: Any) -> None:
        """Persist app-specific data for a key."""

        timestamp = time.time()
        try:
            payload = json.dumps(value)
        except TypeError:
            payload = json.dumps(str(value))
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO app_data (app_id, session_id, key, value, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(app_id, session_id, key)
                DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (app_id, session_id, key, payload, timestamp),
            )
            self._conn.commit()

    def list_app_data(self, app_id: str, session_id: str) -> List[Dict[str, Any]]:
        """Return all app-specific data entries for a session."""

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

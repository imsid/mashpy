"""SQLite-backed local memory implementation."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Union


class Memory(Protocol):
    """Protocol for local persistence providers."""

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Return the stored value for ``key`` if it exists."""
        ...

    def set(
        self,
        key: str,
        value: Dict[str, Any],
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """Persist ``value`` under ``key`` with an optional TTL."""
        ...

    def delete(self, key: str) -> None:
        """Remove ``key`` from the store."""
        ...

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
        ...

    def get_conversation(
        self,
        app_id: str,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return conversation messages ordered by timestamp."""
        ...

    def get_preferences(self, app_id: str, session_id: str) -> Optional[Any]:
        """Return stored preferences for the session."""
        ...

    def set_preferences(self, app_id: str, session_id: str, value: Any) -> None:
        """Persist preferences for the session."""
        ...


class SqliteMemory(Memory):
    """Simple SQLite key/value store with optional TTL support."""

    def __init__(self, path: Union[str, Path] = ":memory:") -> None:
        self._db_path = self._prepare_path(path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                expires_at REAL
            )
            """
        )
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
        self._conn.commit()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Fetch a value if present and not expired."""

        row = self._conn.execute(
            "SELECT value, expires_at FROM kv WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        value_json, expires_at = row
        if expires_at is not None and expires_at <= time.time():
            self.delete(key)
            return None
        return json.loads(value_json)

    def set(
        self,
        key: str,
        value: Dict[str, Any],
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """Store ``value`` with an optional TTL."""

        expires_at = None
        if ttl_seconds is not None:
            expires_at = time.time() + max(0, ttl_seconds)
        payload = json.dumps(value)
        self._conn.execute(
            """
            INSERT INTO kv (key, value, expires_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at
            """,
            (key, payload, expires_at),
        )
        self._conn.commit()

    def delete(self, key: str) -> None:
        """Remove ``key`` from the store."""

        self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))
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


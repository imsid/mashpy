"""SQLite-backed local memory implementation."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional, Protocol, Union


class Memory(Protocol):
    """Protocol for local persistence providers."""

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        """Return the stored value for ``key`` if it exists."""

    def set(
        self,
        key: str,
        value: Dict[str, Any],
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """Persist ``value`` under ``key`` with an optional TTL."""

    def delete(self, key: str) -> None:
        """Remove ``key`` from the store."""


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

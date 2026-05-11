"""Async Postgres-backed memory store implementation."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, cast

from .....logging.events import inflate_logged_event
from ....search.types import SearchColumn
from ...protocol import MemoryStore

try:  # pragma: no cover - environment dependent
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - exercised only without optional deps
    psycopg = None
    dict_row = None


class PostgresStore(MemoryStore):
    """Async Postgres-backed conversation store with signals."""

    def __init__(self, database_url: str) -> None:
        resolved = str(database_url or "").strip()
        if not resolved:
            raise ValueError("database_url is required")
        self._database_url = resolved
        self._conn: Any = None
        self._open_lock = asyncio.Lock()
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        """Open the Postgres connection and initialize schema lazily."""
        if self._conn is not None:
            return
        if psycopg is None or dict_row is None:  # pragma: no cover - env dependent
            raise RuntimeError(
                "psycopg is required for PostgresStore. Install mashpy with PostgreSQL dependencies."
            )
        async with self._open_lock:
            if self._conn is not None:
                return
            conn = cast(
                Any,
                await psycopg.AsyncConnection.connect(self._database_url),
            )
            conn.row_factory = dict_row
            await conn.set_autocommit(True)
            self._conn = conn
            await self._init_schema()

    async def close(self) -> None:
        """Close the Postgres connection."""
        if self._conn is None:
            return
        await self._conn.close()
        self._conn = None

    def _get_conn(self) -> Any:
        if self._conn is None:
            raise RuntimeError("PostgresStore is not open")
        return self._conn

    async def _init_schema(self) -> None:
        conn = self._get_conn()
        async with self._lock:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS memory_turns (
                            turn_id TEXT PRIMARY KEY,
                            session_id TEXT NOT NULL,
                            app_id TEXT NOT NULL,
                            user_message TEXT NOT NULL,
                            agent_response TEXT NOT NULL,
                            session_total_tokens BIGINT NOT NULL DEFAULT 0,
                            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                            created_at DOUBLE PRECISION NOT NULL
                        )
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS memory_signals (
                            turn_id TEXT NOT NULL REFERENCES memory_turns(turn_id) ON DELETE CASCADE,
                            session_id TEXT NOT NULL,
                            app_id TEXT NOT NULL,
                            signal_name TEXT NOT NULL,
                            signal_value JSONB NOT NULL,
                            PRIMARY KEY (turn_id, signal_name)
                        )
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS memory_logs (
                            id BIGSERIAL PRIMARY KEY,
                            app_id TEXT NOT NULL,
                            session_id TEXT,
                            trace_id TEXT,
                            event_class TEXT NOT NULL,
                            event_type TEXT NOT NULL,
                            created_at DOUBLE PRECISION NOT NULL,
                            payload JSONB NOT NULL
                        )
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_memory_turns_session
                        ON memory_turns(session_id)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_memory_turns_app
                        ON memory_turns(app_id)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_memory_signals_app_session
                        ON memory_signals(app_id, session_id)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_memory_signals_app_name
                        ON memory_signals(app_id, signal_name)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_memory_logs_app_id
                        ON memory_logs(app_id)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_memory_logs_session_id
                        ON memory_logs(session_id)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_memory_logs_trace_id
                        ON memory_logs(trace_id)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_memory_turns_user_message_tsv
                        ON memory_turns
                        USING GIN (to_tsvector('simple', COALESCE(user_message, '')))
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_memory_turns_agent_response_tsv
                        ON memory_turns
                        USING GIN (to_tsvector('simple', COALESCE(agent_response, '')))
                        """
                    )

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
                    json.dumps(payload if isinstance(payload, dict) else {}, default=str),
                )
            )

        conn = self._get_conn()
        async with self._lock:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.executemany(
                        """
                        INSERT INTO memory_logs (
                            app_id,
                            session_id,
                            trace_id,
                            event_class,
                            event_type,
                            created_at,
                            payload
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                        """,
                        rows,
                    )

    async def get_logs(
        self,
        app_id: str,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        limit: Optional[int] = None,
        after_log_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        params: List[Any] = [app_id]
        filters = ["app_id = %s"]

        if session_id is not None:
            filters.append("session_id = %s")
            params.append(session_id)
        if trace_id is not None:
            filters.append("trace_id = %s")
            params.append(trace_id)
        if after_log_id is not None:
            filters.append("id > %s")
            params.append(int(after_log_id))

        where_clause = " AND ".join(filters)
        normalized_limit = None if limit is None else max(0, int(limit))
        if normalized_limit == 0:
            return []

        await self.open()

        if after_log_id is None and normalized_limit is not None:
            sql = f"""
                SELECT id, app_id, session_id, trace_id, event_class, event_type, created_at, payload
                FROM memory_logs
                WHERE {where_clause}
                ORDER BY id DESC
                LIMIT %s
            """
            params.append(normalized_limit)
            reverse_results = True
        else:
            sql = f"""
                SELECT id, app_id, session_id, trace_id, event_class, event_type, created_at, payload
                FROM memory_logs
                WHERE {where_clause}
                ORDER BY id ASC
            """
            if normalized_limit is not None:
                sql += "\nLIMIT %s"
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
                FROM memory_logs
                WHERE app_id = %s
                  AND session_id = %s
                  AND trace_id IS NOT NULL
                  AND BTRIM(trace_id) != ''
                GROUP BY app_id, session_id, trace_id
                ORDER BY last_event_at DESC, started_at DESC, trace_id DESC
                LIMIT %s
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
        metadata_json = json.dumps(metadata or {}, default=str)

        await self.open()
        conn = self._get_conn()
        async with self._lock:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        INSERT INTO memory_turns (
                            turn_id,
                            session_id,
                            app_id,
                            user_message,
                            agent_response,
                            session_total_tokens,
                            metadata,
                            created_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
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
                    if signals:
                        await cursor.executemany(
                            """
                            INSERT INTO memory_signals (
                                turn_id,
                                session_id,
                                app_id,
                                signal_name,
                                signal_value
                            )
                            VALUES (%s, %s, %s, %s, %s::jsonb)
                            """,
                            [
                                (
                                    turn_id,
                                    session_id,
                                    app_id,
                                    signal_name,
                                    json.dumps(signal_value, default=str),
                                )
                                for signal_name, signal_value in signals.items()
                            ],
                        )
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
                    FROM memory_turns
                    WHERE session_id = %s AND app_id = %s
                    ORDER BY created_at ASC, turn_id ASC
                    """,
                    params,
                )
            else:
                rows = await self._fetchall_unlocked(
                    """
                    SELECT turn_id, user_message, agent_response, session_total_tokens, metadata, created_at
                    FROM memory_turns
                    WHERE session_id = %s AND app_id = %s
                    ORDER BY created_at DESC, turn_id DESC
                    LIMIT %s
                    """,
                    [*params, max(0, int(limit))],
                )
                rows = list(reversed(rows))

            turn_ids = [str(row["turn_id"]) for row in rows]
            signals_by_turn = await self._get_signals_for_turn_ids_unlocked(turn_ids)

        turns: List[Dict[str, Any]] = []
        for row in rows:
            metadata = self._load_json_value(row["metadata"])
            turns.append(
                {
                    "turn_id": str(row["turn_id"]),
                    "user_message": (
                        "" if row["user_message"] is None else str(row["user_message"])
                    ),
                    "agent_response": (
                        ""
                        if row["agent_response"] is None
                        else str(row["agent_response"])
                    ),
                    "session_total_tokens": int(row["session_total_tokens"] or 0),
                    "signals": signals_by_turn.get(str(row["turn_id"]), {}),
                    "metadata": metadata if isinstance(metadata, dict) else {},
                    "created_at": float(row["created_at"] or 0.0),
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
                    FROM memory_turns
                    WHERE session_id = %s AND app_id = %s
                    ORDER BY created_at ASC, turn_id ASC
                    """,
                    params,
                )
            else:
                rows = await self._fetchall_unlocked(
                    """
                    SELECT turn_id, created_at
                    FROM memory_turns
                    WHERE session_id = %s AND app_id = %s
                    ORDER BY created_at DESC, turn_id DESC
                    LIMIT %s
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
                FROM memory_turns
                WHERE app_id = %s
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
        await self.open()
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
                FROM memory_turns
                WHERE app_id = %s AND session_id = %s
                ORDER BY created_at DESC, turn_id DESC
                LIMIT %s
                """,
                (app_id, session_id, normalized_limit),
            )

        traces: List[Dict[str, Any]] = []
        for row in rows:
            metadata = self._load_json_value(row["metadata"])
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
        requested_rows: List[tuple[str, str, int]] = []
        for idx, pair in enumerate(pairs):
            session_id = str(pair.get("session_id", "")).strip()
            turn_id = str(pair.get("turn_id", "")).strip()
            if not session_id or not turn_id:
                continue
            requested_keys.append((session_id, turn_id))
            requested_rows.append((session_id, turn_id, idx))

        if not requested_rows:
            return None

        values_sql = ", ".join("(%s, %s, %s)" for _ in requested_rows)
        params: List[Any] = []
        for session_id, turn_id, idx in requested_rows:
            params.extend([session_id, turn_id, idx])
        params.append(app_id)
        sql = f"""
            WITH requested(session_id, turn_id, ord) AS (
                VALUES {values_sql}
            )
            SELECT
                t.turn_id,
                t.session_id,
                t.user_message,
                t.agent_response,
                r.ord
            FROM requested AS r
            JOIN memory_turns AS t
              ON t.session_id = r.session_id
             AND t.turn_id = r.turn_id
            WHERE t.app_id = %s
            ORDER BY r.ord ASC
        """

        async with self._lock:
            rows = await self._fetchall_unlocked(sql, params)

        if not rows:
            return None

        return [
            {
                "turn_id": str(row["turn_id"]),
                "session_id": str(row["session_id"]),
                "user_message": (
                    "" if row["user_message"] is None else str(row["user_message"])
                ),
                "agent_response": (
                    ""
                    if row["agent_response"] is None
                    else str(row["agent_response"])
                ),
            }
            for row in rows
        ]

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

        normalized_query = self._build_keyword_query_text(query_term)
        if not normalized_query:
            return []

        await self.open()

        column_name = self._validated_search_column(column)
        tsv_expression = self._search_tsv_expression(column_name)
        params: List[Any] = [normalized_query, normalized_query]
        filters = [f"{tsv_expression} @@ plainto_tsquery('simple', %s)"]
        if session_id is not None:
            filters.append("session_id = %s")
            params.append(session_id)
        if app_id is not None:
            filters.append("app_id = %s")
            params.append(app_id)

        sql = f"""
            SELECT
                turn_id,
                session_id,
                {column_name} AS preview,
                ts_rank_cd({tsv_expression}, plainto_tsquery('simple', %s)) AS rank_score,
                created_at
            FROM memory_turns
            WHERE {' AND '.join(filters)}
            ORDER BY rank_score DESC, created_at DESC, turn_id ASC
            LIMIT %s
        """
        params.append(normalized_limit)

        async with self._lock:
            rows = await self._fetchall_unlocked(sql, params)

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
        raise NotImplementedError("PostgresStore.semantic_search is not implemented yet")

    async def _fetchall_unlocked(
        self,
        sql: str,
        params: Sequence[Any] = (),
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        async with conn.cursor() as cursor:
            await cursor.execute(sql, tuple(params))
            return list(await cursor.fetchall())

    async def _get_signals_for_turn_ids_unlocked(
        self,
        turn_ids: Iterable[str],
    ) -> Dict[str, Dict[str, Any]]:
        normalized_turn_ids = [str(turn_id) for turn_id in turn_ids if str(turn_id)]
        if not normalized_turn_ids:
            return {}

        rows = await self._fetchall_unlocked(
            """
            SELECT turn_id, signal_name, signal_value
            FROM memory_signals
            WHERE turn_id = ANY(%s)
            """,
            (normalized_turn_ids,),
        )

        signals_by_turn: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            turn_id = str(row["turn_id"])
            signals = signals_by_turn.setdefault(turn_id, {})
            signals[str(row["signal_name"])] = self._load_json_value(
                row["signal_value"]
            )
        return signals_by_turn

    @staticmethod
    def _load_json_dict(value: Any) -> Dict[str, Any]:
        loaded = PostgresStore._load_json_value(value)
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _load_json_value(value: Any) -> Any:
        if isinstance(value, (dict, list, int, float, bool)) or value is None:
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @staticmethod
    def _validated_search_column(column: SearchColumn) -> str:
        if column not in ("user_message", "agent_response"):
            raise ValueError(f"Unsupported search column: {column}")
        return column

    @classmethod
    def _build_keyword_query_text(cls, query_term: str) -> str:
        tokens = [token for token in str(query_term).split() if token]
        return " ".join(tokens)

    @staticmethod
    def _search_tsv_expression(column_name: str) -> str:
        return f"to_tsvector('simple', COALESCE({column_name}, ''))"

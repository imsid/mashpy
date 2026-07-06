"""API request/response event logging for Mash host observability."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional, Protocol

try:  # pragma: no cover - optional dependency at import time
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
except ImportError:  # pragma: no cover - exercised only without optional deps
    psycopg = None
    dict_row = None
    AsyncConnectionPool = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

TEXT_CONTENT_TYPES = (
    "application/json",
    "application/problem+json",
    "text/",
    "application/xml",
    "application/x-www-form-urlencoded",
)


@dataclass(frozen=True)
class APIEvent:
    """One persisted backend API request/response event."""

    method: str
    path: str
    query_params: dict[str, Any]
    status_code: int
    duration_ms: int
    request_headers: dict[str, Any]
    response_headers: dict[str, Any]
    request_body: dict[str, Any]
    response_body: dict[str, Any]
    client_host: Optional[str]
    event_type: str = "api.request.complete"
    api_event_id: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class APIEventFilter:
    """Filter options for API event reads."""

    method: Optional[str] = None
    path: Optional[str] = None
    path_prefix: Optional[str] = None
    status_code: Optional[int] = None
    status_code_min: Optional[int] = None
    status_code_max: Optional[int] = None
    from_ts: Optional[float] = None
    to_ts: Optional[float] = None
    after_event_id: int = 0
    limit: int = 2000


class APIEventStore(Protocol):
    """Append-only API event store."""

    async def open(self) -> None:
        ...

    async def close(self) -> None:
        ...

    async def append_event(self, event: APIEvent) -> APIEvent:
        ...

    async def list_events(self, filters: APIEventFilter) -> list[APIEvent]:
        ...


class PostgresAPIEventStore(APIEventStore):
    """Postgres-backed API event store."""

    def __init__(self, database_url: str) -> None:
        resolved = str(database_url or "").strip()
        if not resolved:
            raise ValueError("database_url is required")
        self._database_url = resolved
        self._pool: Any = None
        self._open_lock = asyncio.Lock()
        self._memory_store: _InMemoryAPIEventStore | None = (
            _InMemoryAPIEventStore() if resolved == "postgresql://test/runtime" else None
        )

    async def open(self) -> None:
        if self._memory_store is not None:
            await self._memory_store.open()
            return
        if self._pool is not None:
            return
        if psycopg is None or dict_row is None or AsyncConnectionPool is None:  # pragma: no cover - env dependent
            raise RuntimeError(
                "psycopg and psycopg_pool are required for PostgresAPIEventStore. Install mashpy with PostgreSQL runtime dependencies."
            )
        async with self._open_lock:
            if self._pool is not None:
                return
            pool = AsyncConnectionPool(
                self._database_url,
                min_size=1,
                max_size=5,
                open=False,
                # Managed Postgres closes idle connections server-side; validate
                # at checkout so a stale connection is replaced, not handed out.
                check=AsyncConnectionPool.check_connection,
                kwargs={"autocommit": True, "row_factory": dict_row},
            )
            await pool.open()
            self._pool = pool
            await self._init_schema()

    async def close(self) -> None:
        if self._memory_store is not None:
            await self._memory_store.close()
            return
        if self._pool is None:
            return
        await self._pool.close()
        self._pool = None

    async def append_event(self, event: APIEvent) -> APIEvent:
        if self._memory_store is not None:
            return await self._memory_store.append_event(event)
        await self.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO api_event_log (
                        method, path, query_params, status_code,
                        duration_ms, request_headers, response_headers, request_body,
                        response_body, client_host, event_type, created_at
                    )
                    VALUES (%s, %s, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb,
                            %s::jsonb, %s::jsonb, %s, %s, %s)
                    RETURNING api_event_id, method, path, query_params,
                              status_code, duration_ms, request_headers, response_headers,
                              request_body, response_body, client_host, event_type, created_at
                    """,
                    (
                        event.method,
                        event.path,
                        json.dumps(event.query_params, ensure_ascii=True, default=str),
                        int(event.status_code),
                        int(event.duration_ms),
                        json.dumps(event.request_headers, ensure_ascii=True, default=str),
                        json.dumps(event.response_headers, ensure_ascii=True, default=str),
                        json.dumps(event.request_body, ensure_ascii=True, default=str),
                        json.dumps(event.response_body, ensure_ascii=True, default=str),
                        event.client_host,
                        event.event_type,
                        float(event.created_at),
                    ),
                )
                row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("failed to persist API event")
        return self._row_to_event(row)

    async def list_events(self, filters: APIEventFilter) -> list[APIEvent]:
        if self._memory_store is not None:
            return await self._memory_store.list_events(filters)
        await self.open()
        clauses, params = _filter_clauses(filters)
        params.append(max(1, int(filters.limit)))
        query = f"""
            SELECT api_event_id, method, path, query_params,
                   status_code, duration_ms, request_headers, response_headers,
                   request_body, response_body, client_host, event_type, created_at
            FROM api_event_log
            WHERE {' AND '.join(clauses)}
            ORDER BY api_event_id DESC
            LIMIT %s
        """
        async with self._pool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(query, tuple(params))
                rows = await cursor.fetchall()
        return [self._row_to_event(row) for row in rows]

    async def _init_schema(self) -> None:
        async with self._pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS api_event_log (
                            api_event_id BIGSERIAL,
                            method TEXT NOT NULL,
                            path TEXT NOT NULL,
                            query_params JSONB NOT NULL,
                            status_code INTEGER NOT NULL,
                            duration_ms INTEGER NOT NULL,
                            request_headers JSONB NOT NULL,
                            response_headers JSONB NOT NULL,
                            request_body JSONB NOT NULL,
                            response_body JSONB NOT NULL,
                            client_host TEXT,
                            event_type TEXT NOT NULL,
                            created_at DOUBLE PRECISION NOT NULL
                        )
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS idx_api_event_id
                        ON api_event_log(api_event_id)
                        """
                    )
                    await cursor.execute(
                        """
                        ALTER TABLE api_event_log
                        DROP COLUMN IF EXISTS request_id
                        """
                    )
                    await cursor.execute(
                        """
                        ALTER TABLE api_event_log
                        DROP COLUMN IF EXISTS trace_id
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_api_event_method
                        ON api_event_log(method)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_api_event_status
                        ON api_event_log(status_code)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_api_event_path
                        ON api_event_log(path)
                        """
                    )
                    await cursor.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_api_event_created
                        ON api_event_log(created_at)
                        """
                    )

    @staticmethod
    def _row_to_event(row: dict[str, Any]) -> APIEvent:
        return APIEvent(
            api_event_id=int(row["api_event_id"]),
            method=str(row["method"]),
            path=str(row["path"]),
            query_params=_dict_value(row.get("query_params")),
            status_code=int(row["status_code"]),
            duration_ms=int(row["duration_ms"]),
            request_headers=_dict_value(row.get("request_headers")),
            response_headers=_dict_value(row.get("response_headers")),
            request_body=_dict_value(row.get("request_body")),
            response_body=_dict_value(row.get("response_body")),
            client_host=(str(row["client_host"]) if row.get("client_host") else None),
            event_type=str(row["event_type"]),
            created_at=float(row["created_at"]),
        )


class _InMemoryAPIEventStore(APIEventStore):
    def __init__(self) -> None:
        self._events: list[APIEvent] = []
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        async with self._lock:
            self._events.clear()

    async def append_event(self, event: APIEvent) -> APIEvent:
        async with self._lock:
            stored = APIEvent(
                api_event_id=len(self._events) + 1,
                method=event.method,
                path=event.path,
                query_params=dict(event.query_params),
                status_code=event.status_code,
                duration_ms=event.duration_ms,
                request_headers=dict(event.request_headers),
                response_headers=dict(event.response_headers),
                request_body=dict(event.request_body),
                response_body=dict(event.response_body),
                client_host=event.client_host,
                event_type=event.event_type,
                created_at=float(event.created_at),
            )
            self._events.append(stored)
            return stored

    async def list_events(self, filters: APIEventFilter) -> list[APIEvent]:
        async with self._lock:
            events = list(self._events)
        filtered = [event for event in events if _matches_filter(event, filters)]
        recent = filtered[-max(1, int(filters.limit)) :]
        return list(reversed(recent))


def serialize_api_event(event: APIEvent) -> dict[str, Any]:
    """Serialize an API event for public telemetry APIs."""
    return {
        "api_event_id": int(event.api_event_id),
        "method": event.method,
        "path": event.path,
        "query_params": dict(event.query_params),
        "status_code": int(event.status_code),
        "duration_ms": int(event.duration_ms),
        "request_headers": dict(event.request_headers),
        "response_headers": dict(event.response_headers),
        "request_body": dict(event.request_body),
        "response_body": dict(event.response_body),
        "client_host": event.client_host,
        "event_type": event.event_type,
        "created_at": float(event.created_at),
    }


def build_api_event_filter(
    *,
    method: Any = None,
    path: Any = None,
    path_prefix: Any = None,
    status_code: Any = None,
    status_code_min: Any = None,
    status_code_max: Any = None,
    from_ts: Any = None,
    to_ts: Any = None,
    after_event_id: Any = 0,
    limit: Any = 2000,
    max_limit: int = 20000,
) -> APIEventFilter:
    """Normalize query/body filters for API event reads."""
    return APIEventFilter(
        method=_optional_upper(method),
        path=_optional_text(path),
        path_prefix=_optional_text(path_prefix),
        status_code=_optional_int(status_code),
        status_code_min=_optional_int(status_code_min),
        status_code_max=_optional_int(status_code_max),
        from_ts=_optional_float(from_ts),
        to_ts=_optional_float(to_ts),
        after_event_id=max(0, int(_optional_int(after_event_id) or 0)),
        limit=max(1, min(int(_optional_int(limit) or 2000), max_limit)),
    )


def capture_body(
    body: bytes,
    *,
    content_type: str | None,
    max_bytes: int,
    enabled: bool,
) -> dict[str, Any]:
    """Build a persisted body-capture payload."""
    byte_count = len(body)
    content_type_text = str(content_type or "").split(";", 1)[0].strip().lower()
    base: dict[str, Any] = {
        "content_type": content_type or None,
        "bytes": byte_count,
        "truncated": False,
    }
    if not enabled:
        return {**base, "capture_status": "disabled"}
    if byte_count == 0:
        return {**base, "capture_status": "empty"}
    if not _is_supported_body_type(content_type_text):
        return {**base, "capture_status": "unsupported_content_type"}
    limit = max(0, int(max_bytes))
    preview = body[:limit]
    truncated = byte_count > limit
    text = preview.decode("utf-8", errors="replace")
    captured = {
        **base,
        "capture_status": "captured",
        "truncated": truncated,
        "captured_bytes": len(preview),
    }
    if content_type_text.endswith("json"):
        try:
            captured["json"] = json.loads(text)
        except json.JSONDecodeError:
            captured["text"] = text
            captured["parse_error"] = "invalid_json"
    else:
        captured["text"] = text
    return captured


def sanitize_headers(headers: list[tuple[bytes, bytes]], redacted_headers: set[str]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for raw_name, raw_value in headers:
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1", errors="replace")
        if name in redacted_headers:
            value = "[REDACTED]"
        if name in sanitized:
            existing = sanitized[name]
            if isinstance(existing, list):
                existing.append(value)
            else:
                sanitized[name] = [existing, value]
        else:
            sanitized[name] = value
    return sanitized


def query_params_from_scope(scope: dict[str, Any]) -> dict[str, Any]:
    query_string = scope.get("query_string") or b""
    if not query_string:
        return {}
    from urllib.parse import parse_qs

    parsed = parse_qs(query_string.decode("latin-1"), keep_blank_values=True)
    return {key: values[0] if len(values) == 1 else values for key, values in parsed.items()}


async def stream_api_events(
    store: APIEventStore,
    filters: APIEventFilter,
    *,
    sleep_seconds: float = 0.25,
) -> AsyncIterator[list[APIEvent]]:
    cursor = max(0, int(filters.after_event_id))
    while True:
        current = APIEventFilter(**{**filters.__dict__, "after_event_id": cursor})
        events = await store.list_events(current)
        if events:
            cursor = max(int(event.api_event_id) for event in events)
        yield events
        if not events:
            await asyncio.sleep(max(0.0, sleep_seconds))


def _filter_clauses(filters: APIEventFilter) -> tuple[list[str], list[Any]]:
    clauses = ["api_event_id > %s"]
    params: list[Any] = [int(filters.after_event_id)]
    if filters.method is not None:
        clauses.append("method = %s")
        params.append(filters.method)
    if filters.path is not None:
        clauses.append("path = %s")
        params.append(filters.path)
    if filters.path_prefix is not None:
        clauses.append("path LIKE %s")
        params.append(f"{filters.path_prefix}%")
    if filters.status_code is not None:
        clauses.append("status_code = %s")
        params.append(filters.status_code)
    if filters.status_code_min is not None:
        clauses.append("status_code >= %s")
        params.append(filters.status_code_min)
    if filters.status_code_max is not None:
        clauses.append("status_code <= %s")
        params.append(filters.status_code_max)
    if filters.from_ts is not None:
        clauses.append("created_at >= %s")
        params.append(filters.from_ts)
    if filters.to_ts is not None:
        clauses.append("created_at <= %s")
        params.append(filters.to_ts)
    return clauses, params


def _matches_filter(event: APIEvent, filters: APIEventFilter) -> bool:
    if int(event.api_event_id) <= int(filters.after_event_id):
        return False
    if filters.method is not None and event.method != filters.method:
        return False
    if filters.path is not None and event.path != filters.path:
        return False
    if filters.path_prefix is not None and not event.path.startswith(filters.path_prefix):
        return False
    if filters.status_code is not None and event.status_code != filters.status_code:
        return False
    if filters.status_code_min is not None and event.status_code < filters.status_code_min:
        return False
    if filters.status_code_max is not None and event.status_code > filters.status_code_max:
        return False
    if filters.from_ts is not None and event.created_at < filters.from_ts:
        return False
    if filters.to_ts is not None and event.created_at > filters.to_ts:
        return False
    return True


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _is_supported_body_type(content_type: str) -> bool:
    return content_type.endswith("json") or any(
        content_type == item or content_type.startswith(item) for item in TEXT_CONTENT_TYPES
    )


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_upper(value: Any) -> Optional[str]:
    text = _optional_text(value)
    return text.upper() if text is not None else None


def _optional_int(value: Any) -> Optional[int]:
    text = _optional_text(value)
    if text is None:
        return None
    return int(text)


def _optional_float(value: Any) -> Optional[float]:
    text = _optional_text(value)
    if text is None:
        return None
    return float(text)

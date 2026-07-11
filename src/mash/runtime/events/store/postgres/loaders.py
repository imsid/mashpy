"""Data-loaders: all read/write query functions for the Postgres runtime store.

Each function accepts a psycopg AsyncConnectionPool as its first argument.
Row-mapping helpers are co-located here since they are tightly coupled to the
query column lists.
"""

from __future__ import annotations

import json
from typing import Any

from ...types import FeedbackRecord, RuntimeEvent, RuntimeEventType


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------


def dict_to_event(row: dict[str, Any]) -> RuntimeEvent:
    payload = row.get("payload")
    decoded = payload if isinstance(payload, dict) else {}
    request_seq = row.get("request_seq", row.get("seq"))
    return RuntimeEvent(
        event_id=int(row["event_id"]),
        request_id=(
            str(row["request_id"]) if row.get("request_id") is not None else None
        ),
        request_seq=(int(request_seq) if request_seq is not None else None),
        trace_id=row.get("trace_id"),
        app_id=str(row["app_id"]),
        agent_id=str(row["agent_id"]),
        session_id=row.get("session_id"),
        host_id=row.get("host_id"),
        workflow_id=row.get("workflow_id"),
        workflow_run_id=row.get("workflow_run_id"),
        event_type=str(row["event_type"]),
        loop_index=(
            int(row["loop_index"]) if row.get("loop_index") is not None else None
        ),
        step_key=row.get("step_key"),
        dedupe_key=row.get("dedupe_key"),
        payload=decoded,
        created_at=float(row["created_at"]),
    )


def dict_to_feedback(row: dict[str, Any]) -> FeedbackRecord:
    context = row.get("context")
    decoded = context if isinstance(context, dict) else {}
    return FeedbackRecord(
        feedback_id=int(row["feedback_id"]),
        feedback_type=str(row["feedback_type"]),
        message=str(row["message"]),
        app_id=str(row["app_id"]),
        host_id=row.get("host_id"),
        session_id=row.get("session_id"),
        request_id=row.get("request_id"),
        trace_id=row.get("trace_id"),
        context=decoded,
        created_at=float(row["created_at"]),
    )


def trace_row_to_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "trace_id": str(row["trace_id"]),
        "session_id": str(row["session_id"]) if row.get("session_id") else None,
        "host_id": str(row["host_id"]) if row.get("host_id") else None,
        "agent_id": str(row["agent_id"]) if row.get("agent_id") else None,
        "workflow_id": str(row["workflow_id"]) if row.get("workflow_id") else None,
        "workflow_run_id": (
            str(row["workflow_run_id"]) if row.get("workflow_run_id") else None
        ),
        "event_count": int(row["event_count"]),
        "total_tokens": int(row["total_tokens"] or 0),
        "cache_read_tokens": int(row["cache_read_tokens"] or 0),
        "cache_write_tokens": int(row["cache_write_tokens"] or 0),
        "started_at": float(row["started_at"]),
        "latest_event_at": float(row["latest_event_at"]),
        "latest_event_id": int(row["latest_event_id"]),
        "status": str(row["status"]),
    }


def usage_row_to_bucket(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "bucket_start": float(row["bucket_start"]),
        "request_count": int(row["request_count"]),
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
        "cache_read_tokens": int(row["cache_read_tokens"] or 0),
        "cache_write_tokens": int(row["cache_write_tokens"] or 0),
        "tool_error_count": int(row["tool_error_count"]),
    }


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


async def append_event(pool: Any, event: RuntimeEvent) -> RuntimeEvent:
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cursor:
                if event.request_id and event.dedupe_key:
                    await cursor.execute(
                        """
                        SELECT event_id, request_id, seq AS request_seq, trace_id, app_id,
                               agent_id, session_id, host_id, workflow_id, workflow_run_id,
                               event_type, loop_index, step_key,
                               dedupe_key, payload, created_at
                        FROM runtime_event_log
                        WHERE request_id = %s AND dedupe_key = %s
                        LIMIT 1
                        """,
                        (event.request_id, event.dedupe_key),
                    )
                    existing = await cursor.fetchone()
                    if existing is not None:
                        return dict_to_event(existing)

                next_request_seq: int | None = None
                if event.request_id:
                    await cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (event.request_id,),
                    )
                    await cursor.execute(
                        """
                        SELECT COALESCE(MAX(seq), 0) + 1 AS next_request_seq
                        FROM runtime_event_log
                        WHERE request_id = %s
                        """,
                        (event.request_id,),
                    )
                    row = await cursor.fetchone()
                    next_request_seq = (
                        int(row["next_request_seq"]) if row is not None else 1
                    )

                await cursor.execute(
                    """
                    INSERT INTO runtime_event_log (
                        request_id, trace_id, app_id, agent_id, session_id, host_id,
                        workflow_id, workflow_run_id, seq,
                        event_type, loop_index, step_key, dedupe_key, payload, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    RETURNING event_id, request_id, seq AS request_seq, trace_id, app_id,
                              agent_id, session_id, host_id, workflow_id, workflow_run_id,
                              event_type, loop_index, step_key,
                              dedupe_key, payload, created_at
                    """,
                    (
                        event.request_id,
                        event.trace_id,
                        event.app_id,
                        event.agent_id,
                        event.session_id,
                        event.host_id,
                        event.workflow_id,
                        event.workflow_run_id,
                        next_request_seq,
                        event.event_type,
                        event.loop_index,
                        event.step_key,
                        event.dedupe_key,
                        json.dumps(event.payload or {}, ensure_ascii=True, default=str),
                        float(event.created_at),
                    ),
                )
                stored = await cursor.fetchone()
                if stored is None:
                    raise RuntimeError("failed to persist runtime event")
                await cursor.execute(
                    "SELECT pg_notify('runtime_events', %s)",
                    (event.request_id or "",),
                )
    return dict_to_event(stored)


async def append_feedback(pool: Any, feedback: FeedbackRecord) -> FeedbackRecord:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO runtime_feedback (
                    feedback_type, message, app_id, host_id, session_id,
                    request_id, trace_id, context, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                RETURNING feedback_id, feedback_type, message, app_id, host_id,
                          session_id, request_id, trace_id, context, created_at
                """,
                (
                    feedback.feedback_type,
                    feedback.message,
                    feedback.app_id,
                    feedback.host_id,
                    feedback.session_id,
                    feedback.request_id,
                    feedback.trace_id,
                    json.dumps(feedback.context or {}, ensure_ascii=True, default=str),
                    float(feedback.created_at),
                ),
            )
            stored = await cursor.fetchone()
    if stored is None:
        raise RuntimeError("failed to persist feedback")
    return dict_to_feedback(stored)


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


async def list_request_events(
    pool: Any,
    request_id: str,
    *,
    after_seq: int = 0,
) -> list[RuntimeEvent]:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT event_id, request_id, seq AS request_seq, trace_id, app_id,
                       agent_id, session_id, host_id, event_type, loop_index, step_key,
                       dedupe_key, payload, created_at
                FROM runtime_event_log
                WHERE request_id = %s AND seq > %s
                ORDER BY seq ASC
                """,
                (request_id, int(after_seq)),
            )
            rows = await cursor.fetchall()
    return [dict_to_event(row) for row in rows]


async def list_session_events(
    pool: Any,
    session_id: str,
    *,
    event_types: list[str] | None = None,
) -> list[RuntimeEvent]:
    """Fetch a session's events across *all* agents (primary + subagents).

    Unlike :func:`list_events`, this is not scoped to one ``app_id`` — subagents
    log under their own app_id but share the session, so metrics aggregation
    needs the whole session in one pass. The bulky ``response`` body is stripped
    from terminal events since only ``response_metadata`` (stop_reason, tokens)
    is needed here.
    """
    clauses = ["session_id = %s"]
    params: list[Any] = [session_id]
    if event_types:
        clauses.append("event_type = ANY(%s)")
        params.append(list(event_types))
    query = f"""
        SELECT event_id, request_id, seq AS request_seq, trace_id, app_id,
               agent_id, session_id, host_id, event_type, loop_index, step_key,
               dedupe_key,
               CASE
                   WHEN event_type IN (
                       'runtime.request.completed', 'runtime.request.failed'
                   ) THEN payload - 'response'
                   ELSE payload
               END AS payload,
               created_at
        FROM runtime_event_log
        WHERE {' AND '.join(clauses)}
        ORDER BY event_id ASC
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(query, tuple(params))
            rows = await cursor.fetchall()
    return [dict_to_event(row) for row in rows]


async def list_events(
    pool: Any,
    app_id: str,
    *,
    session_id: str | None = None,
    trace_id: str | None = None,
    host_id: str | None = None,
    workflow_run_id: str | None = None,
    event_type_prefix: str | None = None,
    after_event_id: int = 0,
    limit: int | None = None,
) -> list[RuntimeEvent]:
    clauses = ["app_id = %s", "event_id > %s"]
    params: list[Any] = [app_id, int(after_event_id)]
    if session_id is not None:
        clauses.append("session_id = %s")
        params.append(session_id)
    if trace_id is not None:
        clauses.append("trace_id = %s")
        params.append(trace_id)
    if host_id is not None:
        clauses.append("host_id = %s")
        params.append(host_id)
    if workflow_run_id is not None:
        clauses.append("workflow_run_id = %s")
        params.append(workflow_run_id)
    if event_type_prefix is not None:
        clauses.append("event_type LIKE %s")
        params.append(f"{event_type_prefix}%")
    if limit is not None:
        query = f"""
            SELECT event_id, request_id, request_seq, trace_id, app_id,
                   agent_id, session_id, host_id, event_type, loop_index, step_key,
                   dedupe_key, payload, created_at
            FROM (
                SELECT event_id, request_id, seq AS request_seq, trace_id, app_id,
                       agent_id, session_id, host_id, event_type, loop_index, step_key,
                       dedupe_key, payload, created_at
                FROM runtime_event_log
                WHERE {' AND '.join(clauses)}
                ORDER BY event_id DESC
                LIMIT %s
            ) AS recent_events
            ORDER BY event_id ASC
        """
        params.append(max(1, int(limit)))
    else:
        query = f"""
            SELECT event_id, request_id, seq AS request_seq, trace_id, app_id,
                   agent_id, session_id, host_id, event_type, loop_index, step_key,
                   dedupe_key, payload, created_at
            FROM runtime_event_log
            WHERE {' AND '.join(clauses)}
            ORDER BY event_id ASC
        """
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(query, tuple(params))
            rows = await cursor.fetchall()
    return [dict_to_event(row) for row in rows]


async def has_request(pool: Any, request_id: str) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT 1
                FROM runtime_event_log
                WHERE request_id = %s
                LIMIT 1
                """,
                (request_id,),
            )
            row = await cursor.fetchone()
    return row is not None


async def is_request_terminal(pool: Any, request_id: str) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT event_type
                FROM runtime_event_log
                WHERE request_id = %s
                ORDER BY seq DESC
                LIMIT 1
                """,
                (request_id,),
            )
            row = await cursor.fetchone()
    if row is None:
        return False
    return str(row["event_type"]) in {
        RuntimeEventType.REQUEST_COMPLETED.value,
        RuntimeEventType.REQUEST_FAILED.value,
    }


async def list_feedback(
    pool: Any,
    app_id: str,
    *,
    after: float,
    before: float | None = None,
    feedback_type: str | None = None,
    session_id: str | None = None,
    q: str | None = None,
    limit: int | None = None,
) -> list[FeedbackRecord]:
    clauses = ["app_id = %s", "created_at > %s"]
    params: list[Any] = [app_id, float(after)]
    if before is not None:
        clauses.append("created_at < %s")
        params.append(float(before))
    if feedback_type is not None:
        clauses.append("feedback_type = %s")
        params.append(feedback_type)
    if session_id is not None:
        clauses.append("session_id = %s")
        params.append(session_id)

    query_term = (q or "").strip()
    if query_term:
        clauses.append(
            "to_tsvector('simple', COALESCE(message, '')) "
            "@@ plainto_tsquery('simple', %s)"
        )
        params.append(query_term)
        order_by = (
            "ts_rank_cd(to_tsvector('simple', COALESCE(message, '')), "
            "plainto_tsquery('simple', %s)) DESC, created_at DESC, feedback_id DESC"
        )
        params.append(query_term)
    else:
        order_by = "created_at DESC, feedback_id DESC"

    query = f"""
        SELECT feedback_id, feedback_type, message, app_id, host_id,
               session_id, request_id, trace_id, context, created_at
        FROM runtime_feedback
        WHERE {' AND '.join(clauses)}
        ORDER BY {order_by}
    """
    if limit is not None:
        query += "\nLIMIT %s"
        params.append(max(1, int(limit)))

    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(query, tuple(params))
            rows = await cursor.fetchall()
    return [dict_to_feedback(row) for row in rows]


async def list_recent_traces(
    pool: Any,
    app_id: str | None = None,
    *,
    session_id: str | None = None,
    host_id: str | None = None,
    status: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    # app_id=None lists a session's traces across every executing agent
    # (primary + subagents + cross-agent workflow tasks).
    filters = ["trace_id IS NOT NULL"]
    params: list[Any] = []
    if app_id is not None:
        filters.append("app_id = %s")
        params.append(app_id)
    if session_id is not None:
        filters.append("session_id = %s")
        params.append(session_id)
    if host_id is not None:
        filters.append("host_id = %s")
        params.append(host_id)
    status_filter = ""
    if status is not None:
        status_filter = "WHERE status = %s"
        params.append(status)
    params.append(max(1, int(limit)))
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            # status mirrors _extract_boundary_events in spans.py: the trace's
            # latest terminal lifecycle event wins, else it is still running.
            await cursor.execute(
                f"""
                SELECT * FROM (
                    SELECT
                        trace_id,
                        session_id,
                        MAX(host_id) AS host_id,
                        MAX(agent_id) AS agent_id,
                        MAX(workflow_id) AS workflow_id,
                        MAX(workflow_run_id) AS workflow_run_id,
                        MIN(created_at) AS started_at,
                        MAX(created_at) AS latest_event_at,
                        MAX(event_id) AS latest_event_id,
                        COUNT(*) AS event_count,
                        CASE (ARRAY_AGG(event_type ORDER BY created_at DESC, event_id DESC)
                              FILTER (WHERE event_type IN (
                                  'runtime.request.completed', 'runtime.request.failed'
                              )))[1]
                            WHEN 'runtime.request.failed' THEN 'error'
                            WHEN 'runtime.request.completed' THEN 'completed'
                            ELSE 'in_progress'
                        END AS status,
                        COALESCE(SUM(
                            CASE WHEN event_type = 'runtime.llm.think.completed' THEN
                                COALESCE(NULLIF(payload -> 'token_usage' ->> 'input', '')::numeric, 0)
                                + COALESCE(NULLIF(payload -> 'token_usage' ->> 'output', '')::numeric, 0)
                            ELSE 0 END
                        ), 0) AS total_tokens,
                        COALESCE(SUM(
                            CASE WHEN event_type = 'runtime.llm.think.completed' THEN
                                COALESCE(NULLIF(payload -> 'token_usage' ->> 'cache_read', '')::numeric, 0)
                            ELSE 0 END
                        ), 0) AS cache_read_tokens,
                        COALESCE(SUM(
                            CASE WHEN event_type = 'runtime.llm.think.completed' THEN
                                COALESCE(NULLIF(payload -> 'token_usage' ->> 'cache_write', '')::numeric, 0)
                            ELSE 0 END
                        ), 0) AS cache_write_tokens
                    FROM runtime_event_log
                    WHERE {' AND '.join(filters)}
                    GROUP BY trace_id, session_id
                ) traces
                {status_filter}
                ORDER BY latest_event_at DESC, latest_event_id DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = await cursor.fetchall()
    return [trace_row_to_summary(row) for row in rows]


async def list_sessions(
    pool: Any,
    *,
    agent_id: str | None = None,
    workflow_id: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    # agent_id matches sessions the agent participated in (any event it
    # logged), not just sessions it owns — subagent runs count. workflow_id
    # matches sessions where any event ran under that workflow.
    filters = ["owner_agent_id IS NOT NULL"]
    params: list[Any] = []
    if agent_id is not None:
        filters.append("%s = ANY(sessions.agent_ids)")
        params.append(agent_id)
    if workflow_id is not None:
        filters.append("%s = ANY(sessions.workflow_ids)")
        params.append(workflow_id)
    sql = f"""
        SELECT *, COUNT(*) OVER () AS total_count FROM (
            SELECT
                session_id,
                (ARRAY_AGG(agent_id ORDER BY created_at ASC, event_id ASC))[1]
                    AS owner_agent_id,
                ARRAY_AGG(DISTINCT agent_id) FILTER (WHERE agent_id IS NOT NULL)
                    AS agent_ids,
                ARRAY_AGG(DISTINCT workflow_id) FILTER (WHERE workflow_id IS NOT NULL)
                    AS workflow_ids,
                MAX(host_id) AS host_id,
                MIN(created_at) AS started_at,
                MAX(created_at) AS latest_event_at,
                COUNT(DISTINCT trace_id) AS trace_count,
                COALESCE(SUM(
                    CASE WHEN event_type = 'runtime.llm.think.completed' THEN
                        COALESCE(NULLIF(payload -> 'token_usage' ->> 'input', '')::numeric, 0)
                        + COALESCE(NULLIF(payload -> 'token_usage' ->> 'output', '')::numeric, 0)
                    ELSE 0 END
                ), 0) AS total_tokens,
                COALESCE(SUM(
                    CASE WHEN event_type = 'runtime.llm.think.completed' THEN
                        COALESCE(NULLIF(payload -> 'token_usage' ->> 'cache_read', '')::numeric, 0)
                    ELSE 0 END
                ), 0) AS cache_read_tokens,
                COALESCE(SUM(
                    CASE WHEN event_type = 'runtime.llm.think.completed' THEN
                        COALESCE(NULLIF(payload -> 'token_usage' ->> 'cache_write', '')::numeric, 0)
                    ELSE 0 END
                ), 0) AS cache_write_tokens
            FROM runtime_event_log
            WHERE session_id IS NOT NULL
            GROUP BY session_id
        ) sessions
        WHERE {' AND '.join(filters)}
        ORDER BY sessions.latest_event_at DESC, sessions.session_id ASC
    """
    if limit is not None:
        sql += "\n        LIMIT %s"
        params.append(max(1, int(limit)))
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(sql, tuple(params))
            rows = await cursor.fetchall()
    sessions = [
        {
            "session_id": str(row["session_id"]),
            "owner_agent_id": (
                str(row["owner_agent_id"]) if row.get("owner_agent_id") else None
            ),
            "agent_ids": [str(a) for a in (row.get("agent_ids") or [])],
            "workflow_ids": [str(w) for w in (row.get("workflow_ids") or [])],
            "host_id": str(row["host_id"]) if row.get("host_id") else None,
            "started_at": float(row["started_at"]),
            "latest_event_at": float(row["latest_event_at"]),
            "trace_count": int(row["trace_count"] or 0),
            "total_tokens": int(row["total_tokens"] or 0),
            "cache_read_tokens": int(row["cache_read_tokens"] or 0),
            "cache_write_tokens": int(row["cache_write_tokens"] or 0),
        }
        for row in rows
    ]
    total = int(rows[0]["total_count"]) if rows else 0
    return {"sessions": sessions, "total": total}


async def aggregate_usage(
    pool: Any,
    app_id: str,
    *,
    host_id: str | None = None,
    session_id: str | None = None,
    bucket: str = "day",
    from_ts: float | None = None,
    to_ts: float | None = None,
) -> list[dict[str, Any]]:
    bucket_seconds = 3600 if str(bucket).lower() == "hour" else 86400
    tool_completed = (
        RuntimeEventType.TOOL_CALL_COMPLETED.value,
        RuntimeEventType.SUBAGENT_CALL_COMPLETED.value,
    )
    # Placeholders are bound in SQL text order: the two bucket divisors and
    # the two tool-completed event types live in the SELECT, ahead of the
    # WHERE-clause filters.
    filters = ["app_id = %s"]
    params: list[Any] = [
        bucket_seconds,
        bucket_seconds,
        tool_completed[0],
        tool_completed[1],
        app_id,
    ]
    if host_id is not None:
        filters.append("host_id = %s")
        params.append(host_id)
    if session_id is not None:
        filters.append("session_id = %s")
        params.append(session_id)
    if from_ts is not None:
        filters.append("created_at >= %s")
        params.append(float(from_ts))
    if to_ts is not None:
        filters.append("created_at < %s")
        params.append(float(to_ts))
    query = f"""
        SELECT
            floor(created_at / %s) * %s AS bucket_start,
            COUNT(DISTINCT trace_id) AS request_count,
            COALESCE(SUM(
                CASE WHEN event_type = 'runtime.llm.think.completed' THEN
                    COALESCE(NULLIF(payload -> 'token_usage' ->> 'input', '')::numeric, 0)
                ELSE 0 END
            ), 0) AS input_tokens,
            COALESCE(SUM(
                CASE WHEN event_type = 'runtime.llm.think.completed' THEN
                    COALESCE(NULLIF(payload -> 'token_usage' ->> 'output', '')::numeric, 0)
                ELSE 0 END
            ), 0) AS output_tokens,
            COALESCE(SUM(
                CASE WHEN event_type = 'runtime.llm.think.completed' THEN
                    COALESCE(NULLIF(payload -> 'token_usage' ->> 'cache_read', '')::numeric, 0)
                ELSE 0 END
            ), 0) AS cache_read_tokens,
            COALESCE(SUM(
                CASE WHEN event_type = 'runtime.llm.think.completed' THEN
                    COALESCE(NULLIF(payload -> 'token_usage' ->> 'cache_write', '')::numeric, 0)
                ELSE 0 END
            ), 0) AS cache_write_tokens,
            COALESCE(SUM(
                CASE
                    WHEN event_type IN (%s, %s)
                     AND (payload -> 'result' ->> 'is_error') = 'true'
                    THEN 1 ELSE 0
                END
            ), 0) AS tool_error_count
        FROM runtime_event_log
        WHERE {' AND '.join(filters)}
        GROUP BY bucket_start
        ORDER BY bucket_start ASC
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(query, tuple(params))
            rows = await cursor.fetchall()
    return [usage_row_to_bucket(row) for row in rows]


async def count_tool_invocations(
    pool: Any,
    app_id: str,
    *,
    from_ts: float | None = None,
    to_ts: float | None = None,
) -> list[dict[str, Any]]:
    filters = [
        "app_id = %s",
        f"event_type = '{RuntimeEventType.TOOL_CALL_COMPLETED.value}'",
        "payload->>'tool_name' IS NOT NULL",
    ]
    params: list[Any] = [app_id]
    if from_ts is not None:
        filters.append("created_at >= %s")
        params.append(float(from_ts))
    if to_ts is not None:
        filters.append("created_at < %s")
        params.append(float(to_ts))
    query = f"""
        SELECT payload->>'tool_name' AS tool_name, COUNT(*) AS count
        FROM runtime_event_log
        WHERE {' AND '.join(filters)}
        GROUP BY payload->>'tool_name'
        ORDER BY count DESC
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(query, tuple(params))
            rows = await cursor.fetchall()
    return [{"tool_name": str(row["tool_name"]), "count": int(row["count"])} for row in rows]


async def count_skill_invocations(
    pool: Any,
    app_id: str,
    *,
    from_ts: float | None = None,
    to_ts: float | None = None,
) -> list[dict[str, Any]]:
    filters = [
        "app_id = %s",
        f"event_type = '{RuntimeEventType.TOOL_CALL_COMPLETED.value}'",
        "payload->>'tool_name' = 'Skill'",
        "payload->'result'->'metadata'->>'skill_name' IS NOT NULL",
    ]
    params: list[Any] = [app_id]
    if from_ts is not None:
        filters.append("created_at >= %s")
        params.append(float(from_ts))
    if to_ts is not None:
        filters.append("created_at < %s")
        params.append(float(to_ts))
    query = f"""
        SELECT payload->'result'->'metadata'->>'skill_name' AS skill_name,
               COUNT(*) AS count
        FROM runtime_event_log
        WHERE {' AND '.join(filters)}
        GROUP BY skill_name
        ORDER BY count DESC
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(query, tuple(params))
            rows = await cursor.fetchall()
    return [{"skill_name": str(row["skill_name"]), "count": int(row["count"])} for row in rows]

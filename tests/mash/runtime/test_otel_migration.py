"""Migration 009 rewrites rows stored under the old event names."""

from __future__ import annotations

import json
import os
import unittest
import uuid
from pathlib import Path

import mash.storage.migrations

try:  # pragma: no cover - environment dependent
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover
    psycopg = None
    dict_row = None

_MIGRATION = Path(mash.storage.migrations.__file__).parent / "009_otel_event_names.sql"


def _database_url() -> str:
    return os.environ.get(
        "MASH_REAL_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/mash"
    )


class OtelEventNameMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if psycopg is None:
            raise unittest.SkipTest("psycopg is not installed")
        try:
            self.conn = psycopg.connect(_database_url(), autocommit=True, row_factory=dict_row)
        except Exception as exc:  # pragma: no cover
            raise unittest.SkipTest(f"Postgres unavailable: {exc}") from exc
        self.addCleanup(self.conn.close)
        self.request_id = f"migrate-{uuid.uuid4()}"
        self.addCleanup(
            self.conn.execute,
            "DELETE FROM runtime_event_log WHERE request_id = %s",
            (self.request_id,),
        )
        # The database already has every migration applied; this test replays
        # 009 over rows written the old way.

    def _insert(self, seq: int, event_type: str, payload: dict, *, trace_id: str | None = "t-1",
                loop_index: int | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO runtime_event_log (
                request_id, trace_id, app_id, agent_id, session_id, seq, event_type,
                loop_index, payload, created_at
            ) VALUES (%s, %s, 'writer', 'writer', 's-1', %s, %s, %s, %s, %s)
            """,
            (self.request_id, trace_id, seq, event_type, loop_index, json.dumps(payload), 100.0 + seq),
        )

    def _rows(self) -> list[dict]:
        return self.conn.execute(
            "SELECT event_type, trace_id, payload FROM runtime_event_log "
            "WHERE request_id = %s ORDER BY seq",
            (self.request_id,),
        ).fetchall()

    def test_old_rows_take_the_new_names_and_shape(self) -> None:
        self._insert(1, "runtime.request.accepted", {"message": "hi", "mode": "fg"}, trace_id=None)
        self._insert(2, "runtime.trace.started", {"message": "hi"})
        self._insert(3, "runtime.context.loaded", {"message": "hi"})
        self._insert(4, "runtime.llm.think.started", {"loop_index": 0}, loop_index=0)
        self._insert(5, "llm.request.complete", {"model": "m"})
        self._insert(6, "runtime.llm.think.completed", {
            "action_type": "tool_call", "stop_reason": "tool_use", "trace_id": "t-1",
            "token_usage": {"input": 10, "output": 2, "cache_read": 5},
        }, loop_index=0)
        self._insert(7, "runtime.tool.call.started", {"tool_call_id": "c1", "tool_name": "search"}, loop_index=0)
        self._insert(8, "runtime.tool.call.completed", {
            "tool_call_id": "c1", "tool_name": "search", "duration_ms": 30,
            "result": {"content": "nope", "is_error": True},
        }, loop_index=0)
        self._insert(9, "runtime.step.completed", {"action_type": "tool_call", "duration_ms": 50}, loop_index=0)
        self._insert(10, "agent.think.complete", {})
        self._insert(11, "runtime.turn.persisted", {})
        self._insert(12, "runtime.request.failed", {"status": "error", "error": "boom", "error_type": "RuntimeError"})

        self.conn.execute(_MIGRATION.read_text())

        rows = self._rows()
        self.assertEqual(
            [r["event_type"] for r in rows],
            [
                "invoke_agent.start",
                "mash.context_load.start",
                "mash.context_load.end",
                "chat.start",
                "chat.end",
                "execute_tool.start",
                "execute_tool.end",
                "mash.step.end",
                "invoke_agent.end",
            ],
        )
        accepted, _, _, _, chat_end, tool_start, tool_end, step_end, end = rows
        self.assertEqual(accepted["trace_id"], "t-1")
        self.assertEqual(accepted["payload"]["attributes"]["gen_ai.agent.id"], "writer")
        self.assertEqual(accepted["payload"]["message"], "hi")
        self.assertEqual(
            chat_end["payload"]["attributes"],
            {
                "gen_ai.operation.name": "chat",
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 2,
                "gen_ai.usage.cache_read.input_tokens": 5,
                "gen_ai.response.finish_reasons": ["tool_use"],
            },
        )
        self.assertNotIn("token_usage", chat_end["payload"])
        self.assertEqual(chat_end["payload"]["status"], "ok")
        self.assertEqual(tool_start["payload"]["attributes"]["gen_ai.tool.call.id"], "c1")
        self.assertEqual(tool_end["payload"]["status"], "error")
        self.assertEqual(tool_end["payload"]["attributes"]["error.type"], "tool_error")
        self.assertEqual(step_end["payload"]["attributes"], {"mash.loop_index": 0})
        self.assertEqual(end["payload"]["status"], "error")
        self.assertEqual(end["payload"]["attributes"], {"error.type": "RuntimeError"})


if __name__ == "__main__":
    unittest.main()

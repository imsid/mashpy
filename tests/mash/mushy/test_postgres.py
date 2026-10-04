"""Real SQL regression: filtering happens before the 50-trace limit."""

import asyncio
import os
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
import pytest

from mash.runtime.events.store.postgres.loaders import list_thought_traces
from mash.storage.migrations import run_migrations


@pytest.mark.skipif(not os.getenv('MASH_REAL_DATABASE_URL'), reason='requires test Postgres')
def test_feed_selection_order_isolation_and_fresh_schema():
    url = os.environ['MASH_REAL_DATABASE_URL']
    schema = 'thoughts_test_' + uuid4().hex
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))

    async def run():
        pool = AsyncConnectionPool(url, open=False, kwargs={
            'autocommit': True, 'row_factory': dict_row,
            'options': '-c search_path=' + schema,
        })
        await pool.open()
        try:
            await run_migrations(pool)
            await run_migrations(pool)  # safe on repeated startup
            async with pool.connection() as conn:
                cur = await conn.execute("SELECT to_regclass('mushy_artifact_legacy') AS table_name")
                assert (await cur.fetchone())['table_name'] is None
                cur = await conn.execute("SELECT to_regclass('mushy_artifact') AS table_name")
                assert (await cur.fetchone())['table_name'] is None

                cur = await conn.execute("SELECT to_regclass('idx_runtime_thought_traces') AS index_name")
                assert (await cur.fetchone())['index_name'] is not None

                async def insert(agent, trace, summary, ts, event='runtime.llm.think.completed'):
                    await conn.execute('''INSERT INTO runtime_event_log
                        (app_id, agent_id, trace_id, session_id, event_type, payload, created_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                        (agent, agent, trace, 'session-' + agent, event, Jsonb({'thought_summary': summary}), ts))
                # 55 qualifying traces and many newer non-qualifying events.
                for i in range(55):
                    await insert('pilot', 'trace-' + str(i), 'Original ' + str(i), i)
                for i in range(60):
                    await insert('pilot', 'blank-' + str(i), ['', ' \n\t ', None, 42, {}, []][i % 6], 1000 + i)
                await insert('pilot', 'not-thoughts', 'Tool result is not a thought.', 2000, 'runtime.tool.completed')
                await insert('pilot', None, 'No trace', 2001)
                original = '**Raw**\n\n🧐 I am reconsidering.  \n'
                # Multiple summaries must not consume multiple trace slots.
                await insert('pilot', 'trace-54', original, 100)
                await insert('pilot', 'trace-54', 'Same timestamp, later event.', 100)
                # Another agent reuses a trace id: never mix its text in pilot.
                await insert('child', 'trace-54', 'Child summary', 101)
            traces = await list_thought_traces(pool, 'pilot')
            assert len(traces) == 50
            assert [t['trace_id'] for t in traces] == ['trace-' + str(i) for i in range(54, 4, -1)]
            assert [s['thought_summary'] for s in traces[0]['summaries']] == ['Original 54', original, 'Same timestamp, later event.']
            assert traces[0]['session_id'] == 'session-pilot'
            assert await list_thought_traces(pool, 'missing') == []
            assert len(await list_thought_traces(pool, 'pilot', limit=999)) == 50
            all_traces = await list_thought_traces(pool)
            assert len(all_traces) == 50
            assert all_traces[0]['agent_id'] == 'child'
            assert len(all_traces[0]['summaries']) == 1
        finally:
            await pool.close()
    try:
        asyncio.run(run())
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))

"""cortex-build: collect, curate and write over the test stores, with a stubbed curator."""

from __future__ import annotations

import inspect
import os
import unittest
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from conftest import build_test_stores
from mash.agents.cortex import (
    CORTEX_AGENT_ID,
    CORTEX_BUILD_WORKFLOW_ID,
    CortexConflictError,
    CortexEntry,
    CortexRuntimeContext,
    CortexSettings,
    CortexStore,
    CuratorOutput,
    EntryChange,
    build_cortex_build_workflow,
)
from mash.agents.cortex import workflows as cortex_workflows
from mash.runtime import HostBuilder
from mash.runtime.events import RuntimeEvent, RuntimeEventType
from mash.runtime.structured_output import serialize_structured_output
from mash.testing.runtime_fixtures import build_spec, metadata
from mash.workflows import StepContext

TEST_URL = "postgresql://test/runtime"

try:  # pragma: no cover - environment dependent
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


class _Curator:
    """Stands in for cortex-agent: records the request, returns a fixed output."""

    def __init__(self, output: dict[str, Any]) -> None:
        self.output = output
        self.requests: list[dict[str, Any]] = []

    async def post(self, runner_id: str, **kwargs: Any) -> str:
        self.requests.append(kwargs)
        return f"req-{kwargs['workflow_run_id']}-{kwargs['task_id']}"

    async def collect(self, runner_id: str, agent_id: str, request_id: str) -> dict[str, Any]:
        return {"response": {"text": "", "structured_output": self.output}}


class _CortexBuildFixture(unittest.IsolatedAsyncioTestCase):
    """Test stores, a Cortex context over them, and a stubbed curator."""

    async def asyncSetUp(self) -> None:
        self.runtime_store, self.memory_store = build_test_stores()
        self.store = CortexStore(TEST_URL)
        pool = SimpleNamespace(
            runner_id="runner-1",
            runtime_database_url=TEST_URL,
            get_memory_store=lambda agent_id: self.memory_store,
            get_runtime_store=lambda: self.runtime_store,
        )
        self.context = CortexRuntimeContext(
            settings=CortexSettings(profile_max_chars=50), pool=pool, store=self.store
        )
        self.workflow = build_cortex_build_workflow(self.context)
        self.curator = _Curator({"profile": "", "upserts": [], "deletes": []})
        for name, fake in (
            ("post_inline_agent_request", self.curator.post),
            ("collect_terminal_payload", self.curator.collect),
        ):
            p = patch.object(cortex_workflows, name, fake)
            p.start()
            self.addCleanup(p.stop)

    async def _turn(
        self,
        trace_id: str,
        *,
        user_id: str | None = "u-1",
        created_at: float,
        app_id: str = "assistant",
        workflow_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        message: str = "hello",
    ) -> None:
        await self.memory_store.save_turn(
            trace_id=trace_id,
            session_id="s-1",
            app_id=app_id,
            user_message=message,
            agent_response=f"reply to {message}",
            signals={},
            session_total_tokens=0,
            metadata=metadata,
            workflow_id=workflow_id,
            user_id=user_id,
        )
        self.memory_store._turns[-1]["created_at"] = created_at

    async def _event(self, trace_id: str, event_type: str, payload: dict[str, Any], **kw: Any) -> None:
        await self.runtime_store.append_event(
            RuntimeEvent(
                app_id="assistant",
                agent_id="assistant",
                request_id=f"req-{trace_id}",
                trace_id=trace_id,
                session_id="s-1",
                event_type=event_type,
                payload=payload,
                **kw,
            )
        )

    async def _run(
        self, run_id: str = "run-1", user_id: str = "u-1", **workflow_input: Any
    ) -> dict[str, Any]:
        prev: dict[str, Any] = {}
        self.outputs: dict[str, dict[str, Any]] = {}
        for step in self.workflow.steps:
            inp = step.input.model_validate({**workflow_input, **prev})
            ctx = StepContext(
                run_id=run_id,
                step_id=step.step_id,
                workflow_input=workflow_input,
                user_id=user_id,
            )
            result = step.run(inp, ctx)
            if inspect.isawaitable(result):
                result = await result
            prev = step.output.model_validate(result).model_dump(mode="json")
            self.outputs[step.step_id] = prev
        return prev

class CortexBuildTests(_CortexBuildFixture):
    async def test_collect_takes_the_users_own_turns_and_their_tools(self) -> None:
        await self._turn("t-1", created_at=1.0, message="I use metric")
        await self._turn("t-2", created_at=2.0, message="I work at Acme")
        await self._turn("t-wf", created_at=3.0, workflow_id="wf")
        await self._turn("t-sub", created_at=4.0, metadata={"subagent_id": "research"})
        await self._turn("t-other", user_id="u-2", created_at=5.0)
        await self._event(
            "t-1",
            RuntimeEventType.LLM_THINK_COMPLETED.value,
            {"tool_calls": [{"id": "c1", "name": "convert", "arguments": {"to": "km"}}]},
        )
        await self._event(
            "t-1",
            RuntimeEventType.TOOL_CALL_COMPLETED.value,
            {"tool_call_id": "c1", "tool_name": "convert", "result": {"content": "12 km"}},
        )
        await self._event(
            "t-1",
            RuntimeEventType.TOOL_CALL_COMPLETED.value,
            {"tool_call_id": "c2", "tool_name": "search_cortex", "result": {"content": "x"}},
        )

        await self._run()

        bundle = self.outputs["collect"]
        self.assertEqual([t["trace_id"] for t in bundle["turns"]], ["t-1", "t-2"])
        self.assertEqual(
            bundle["turns"][0]["tool_calls"],
            [{"tool_name": "convert", "arguments": '{"to": "km"}', "result": "12 km"}],
        )
        self.assertEqual((bundle["base_version"], bundle["through"], bundle["remaining"]), (0, 2.0, 0))
        [request] = self.curator.requests
        self.assertEqual(request["agent_id"], CORTEX_AGENT_ID)
        self.assertEqual(request["workflow_id"], CORTEX_BUILD_WORKFLOW_ID)
        self.assertIn("I work at Acme", request["message"])
        self.assertNotIn("t-other", request["message"])

    async def test_limit_leaves_the_rest_for_the_next_run(self) -> None:
        for index in range(3):
            await self._turn(f"t-{index}", created_at=float(index + 1))

        result = await self._run(limit=2)

        self.assertEqual(result["turns_covered"], 2)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual((await self.store.load("u-1")).built_through, 2.0)

    async def test_no_new_turns_skips_the_curator_and_writes_nothing(self) -> None:
        result = await self._run()

        self.assertEqual(self.curator.requests, [])
        self.assertEqual(result, {
            "user_id": "u-1", "turns_covered": 0, "entries_written": 0,
            "entries_deleted": 0, "remaining": 0,
        })
        self.assertEqual((await self.store.load("u-1")).version, 0)

    async def test_build_writes_profile_and_entries_and_keeps_pinned_entries(self) -> None:
        await self._turn("t-1", created_at=1.0)
        mem = self.store._mem
        mem.profiles["u-1"] = "old"
        mem.users["u-1"] = {"built_through": 0.0, "version": 3}
        for entry_id, origin, content in (
            ("e-build", "build", "Uses imperial"),
            ("e-stale", "build", "Lives in Paris"),
            ("e-app", "app", "Plan: enterprise"),
        ):
            mem.entries[entry_id] = CortexEntry(
                entry_id=entry_id, user_id="u-1", topic="t", content=content, origin=origin
            )
        source = {"agent_id": "assistant", "session_id": "s-1", "trace_id": "t-1"}
        self.curator.output = {
            "profile": "Uses metric units. " * 10,
            "upserts": [
                {"entry_id": "e-build", "topic": "units", "content": "Uses metric", "sources": [source]},
                {"entry_id": None, "topic": "work", "content": "Works at Acme", "sources": [source]},
                {"entry_id": "e-app", "topic": "plan", "content": "Plan: free", "sources": []},
            ],
            "deletes": ["e-stale", "e-app"],
        }

        result = await self._run()

        self.assertEqual((result["entries_written"], result["entries_deleted"]), (2, 1))
        snapshot = await self.store.load("u-1")
        self.assertEqual(snapshot.version, 4)
        self.assertEqual(snapshot.built_through, 1.0)
        self.assertEqual(len(snapshot.profile), 50)
        contents = {e.content: e.origin for e in snapshot.entries}
        self.assertEqual(
            contents, {"Uses metric": "build", "Works at Acme": "build", "Plan: enterprise": "app"}
        )
        # Pinned app entries reach the curator as fixed facts.
        self.assertIn('"origin": "app"', self.curator.requests[0]["message"])

        # The watermark moved, so a second run finds nothing new.
        self.curator.requests.clear()
        await self._run(run_id="run-2")
        self.assertEqual(self.curator.requests, [])

    async def test_write_fails_when_cortex_changed_after_collect(self) -> None:
        await self._turn("t-1", created_at=1.0)
        original = self.workflow.steps[1].run

        async def curate_then_app_edit(inp, ctx):
            output = await original(inp, ctx)
            self.store._mem.profiles["u-1"] = ""
            self.store._mem.users["u-1"] = {"built_through": 0.0, "version": 1}
            return output

        object.__setattr__(self.workflow.steps[1], "run", curate_then_app_edit)

        with self.assertRaises(CortexConflictError):
            await self._run()

    async def test_retried_write_returns_its_stored_result(self) -> None:
        kwargs = dict(
            user_id="u-1", run_id="run-1", base_version=0, built_through=1.0, profile="p",
            upserts=[EntryChange(topic="t", content="c")], deletes=[],
            result={"user_id": "u-1", "turns_covered": 1, "remaining": 0},
        )

        first = await self.store.apply_build(**kwargs)
        second = await self.store.apply_build(**kwargs)

        self.assertEqual(first, second)
        self.assertEqual((await self.store.load("u-1")).version, 1)
        self.assertEqual(len((await self.store.load("u-1")).entries), 1)


class _InlineDBOS:
    @staticmethod
    async def run_step_async(config, func, *args, **kwargs):
        result = func(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result


class CortexBuildEngineTests(_CortexBuildFixture):
    """The same workflow through the forward engine, with DBOS steps run inline."""

    RUNNER = "runner-cortex"

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from mash.workflows import WorkflowRegistry
        from mash.workflows import dbos as workflow_dbos
        from mash.workflows.store import WorkflowStore

        self.workflow_dbos = workflow_dbos
        self.url = _postgres_url()
        self.workflow_store = WorkflowStore(self.url)
        await self.workflow_store.open()
        registry = WorkflowRegistry()
        registry.register(self.workflow)
        self.engine_host = SimpleNamespace(
            runtime_database_url=self.url,
            get_workflow_registry=lambda: registry,
            get_workflow_store=lambda: self.workflow_store,
        )
        workflow_dbos.register_runner(self.RUNNER, self.engine_host)

    async def asyncTearDown(self) -> None:
        self.workflow_dbos.unregister_runner(self.RUNNER, self.engine_host)
        await self.workflow_store.close()
        with psycopg.connect(self.url, autocommit=True) as conn:
            for table in ("workflow_step_events", "workflow_steps", "workflow_runs"):
                conn.execute(
                    f"DELETE FROM {table} WHERE workflow_id = %s", (CORTEX_BUILD_WORKFLOW_ID,)
                )

    async def test_engine_runs_collect_curate_write(self) -> None:
        await self._turn("t-1", created_at=1.0, message="I use metric")
        self.curator.output = {
            "profile": "Uses metric.",
            "upserts": [{"entry_id": None, "topic": "units", "content": "Uses metric", "sources": []}],
            "deletes": [],
        }
        run_id = self.workflow_dbos.make_run_id(self.RUNNER, CORTEX_BUILD_WORKFLOW_ID)
        fake = (_InlineDBOS, None, None, None)
        with patch.object(self.workflow_dbos, "_load_dbos_api", return_value=fake), patch(
            "mash.workflows.engine.load_dbos_api", return_value=fake
        ):
            output = await self.workflow_dbos.execute_registered_workflow(
                self.RUNNER, CORTEX_BUILD_WORKFLOW_ID, run_id, workflow_input={}, user_id="u-1"
            )

        self.assertEqual(output["result"]["entries_written"], 1)
        self.assertEqual(self.curator.requests[0]["workflow_run_id"], run_id)
        steps = await self.workflow_store.get_run_steps(run_id)
        self.assertEqual([s.step_id for s in steps], ["collect", "curate", "write"])
        self.assertEqual((await self.store.load("u-1")).profile, "Uses metric.")


class CortexBuildSpecTests(unittest.TestCase):
    def test_curator_schema_is_strict(self) -> None:
        schema = serialize_structured_output(CuratorOutput)
        assert schema is not None
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        entry = schema["$defs"]["CuratedEntry"]
        self.assertEqual(set(entry["required"]), set(entry["properties"]))

    def test_builder_registers_cortex_agent_and_workflow_only_when_enabled(self) -> None:
        def build(enabled: bool):
            builder = HostBuilder().agent(build_spec(agent_id="assistant"), metadata=metadata())
            return (builder.enable_cortex() if enabled else builder).build()

        enabled, disabled = build(True), build(False)

        self.assertIn(CORTEX_AGENT_ID, enabled.list_agents())
        self.assertNotIn(CORTEX_AGENT_ID, disabled.list_agents())
        def workflow_ids(pool):
            return {w.workflow_id for w in pool.get_workflow_registry().list()}

        self.assertIn(CORTEX_BUILD_WORKFLOW_ID, workflow_ids(enabled))
        self.assertNotIn(CORTEX_BUILD_WORKFLOW_ID, workflow_ids(disabled))
        spec = enabled.get_registered_agent_spec(CORTEX_AGENT_ID)
        self.assertIs(spec.runtime_context.pool, enabled)


def _postgres_url() -> str:
    if psycopg is None:
        raise unittest.SkipTest("psycopg is not installed")
    url = os.environ.get(
        "MASH_REAL_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/mash"
    )
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # pragma: no cover - depends on local postgres
        raise unittest.SkipTest(f"Postgres unavailable: {exc}") from exc
    return url


class PostgresCortexBuildTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.url = _postgres_url()
        self.store = CortexStore(self.url)
        await self.store.open()
        self.user_id = f"cortex-{uuid.uuid4().hex}"

    async def asyncTearDown(self) -> None:
        await self.store.close()
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("DELETE FROM cortex_user WHERE user_id = %s", (self.user_id,))
            conn.execute("DELETE FROM memory_turns WHERE user_id = %s", (self.user_id,))

    def _build(self, run_id: str, base_version: int, **overrides: Any) -> dict[str, Any]:
        build = dict(
            user_id=self.user_id, run_id=run_id, base_version=base_version,
            built_through=float(base_version + 1), profile=f"profile {run_id}",
            upserts=[], deletes=[],
            result={"user_id": self.user_id, "turns_covered": 1, "remaining": 0},
        )
        return {**build, **overrides}

    async def test_apply_build_is_a_versioned_idempotent_write(self) -> None:
        source = {"agent_id": "a", "session_id": "s", "trace_id": "t"}
        new_entry = [EntryChange(topic="work", content="Acme", sources=[source])]
        first = await self.store.apply_build(**self._build("run-1", 0, upserts=new_entry))
        self.assertEqual(first["entries_written"], 1)
        # A retry of the same run returns the stored result and writes nothing.
        self.assertEqual(
            await self.store.apply_build(**self._build("run-1", 0, upserts=new_entry)), first
        )

        with self.assertRaises(CortexConflictError):
            await self.store.apply_build(**self._build("run-2", 0))

        snapshot = await self.store.load(self.user_id)
        self.assertEqual((snapshot.version, snapshot.built_through), (1, 1.0))
        [entry] = snapshot.entries
        self.assertEqual((entry.content, entry.origin, entry.sources), ("Acme", "build", [source]))

        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute(
                "UPDATE cortex_entry SET origin = 'app' WHERE entry_id = %s", (entry.entry_id,)
            )
        result = await self.store.apply_build(
            **self._build(
                "run-3",
                1,
                upserts=[EntryChange(entry_id=entry.entry_id, topic="work", content="Globex")],
                deletes=[entry.entry_id],
            )
        )
        self.assertEqual((result["entries_written"], result["entries_deleted"]), (0, 0))
        [pinned] = (await self.store.load(self.user_id)).entries
        self.assertEqual(pinned.content, "Acme")

    async def test_list_user_turns_skips_workflow_subagent_and_other_users(self) -> None:
        from mash.memory.store import PostgresStore

        memory_store = PostgresStore(self.url)
        await memory_store.open()
        try:
            for trace, kwargs in (
                ("own", {}),
                ("wf", {"workflow_id": "wf"}),
                ("sub", {"metadata": {"subagent_id": "research"}}),
            ):
                await memory_store.save_turn(
                    trace_id=f"{self.user_id}-{trace}", session_id="s", app_id="assistant",
                    user_message="hi", agent_response="yo", signals={}, session_total_tokens=0,
                    user_id=self.user_id, **kwargs,
                )
            turns = await memory_store.list_user_turns(self.user_id)
            self.assertEqual([t["trace_id"] for t in turns], [f"{self.user_id}-own"])
            self.assertEqual(await memory_store.count_user_turns(self.user_id), 1)
            later = await memory_store.list_user_turns(self.user_id, after=turns[0]["created_at"])
            self.assertEqual(later, [])
        finally:
            await memory_store.close()


if __name__ == "__main__":
    unittest.main()

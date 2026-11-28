"""The companion routes each message with a classifier and stops.

All with a scripted classifier: no real model is called.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

import httpx
from pydantic import BaseModel, Field

from mash.agents.companion import COMPANION_AGENT_ID
from mash.agents.companion.classifier import CLASSIFIER_PROMPT, WORKFLOW_INPUT_PROMPT
from mash.runtime import Host, HostBuilder
from mash.runtime.engine import route as route_module
from mash.runtime.engine.loop import RequestRun
from mash.runtime.events import RuntimeEventType
from mash.runtime.host.tokens import derive_agent_token
from mash.testing.agents import DEFAULT_LOCAL_SECRET
from mash.testing.runtime_fixtures import (
    RoutingLLMProvider,
    build_spec,
    metadata,
    patch_companion_llm,
)
from mash.workflows import CodeStep, StepContext, WorkflowSpec


class ImportIn(BaseModel):
    csv_path: str = Field(description="The CSV file to import.")
    dry_run: bool = False


class ImportOut(BaseModel):
    rows: int


def _import(inp: ImportIn, ctx: StepContext) -> ImportOut:
    return ImportOut(rows=len(inp.csv_path))


def _workflow() -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id="import",
        input_model=ImportIn,
        steps=[CodeStep(step_id="load", run=_import, input=ImportIn, output=ImportOut)],
        metadata={"display_name": "Import", "description": "Imports a CSV of transactions."},
    )


class RouteTestCase(unittest.IsolatedAsyncioTestCase):
    async def _pool(self, provider: RoutingLLMProvider, *, workflows: bool = False) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for target in (
            patch.dict(os.environ, {"MASH_DATA_DIR": tmp.name, "MASH_DATABASE_URL": ""}),
            patch_companion_llm(provider),
            patch.object(route_module, "WORKFLOW_POLL_SECONDS", 0.02),
        ):
            target.start()
            self.addCleanup(target.stop)
        builder = (
            HostBuilder()
            .agent(build_spec(agent_id="builder", response_text="built it"), metadata=metadata())
            .agent(build_spec(agent_id="pilot", response_text="flew it"), metadata=metadata())
        )
        if workflows:
            builder = builder.workflow(_workflow())
        host = Host(
            host_id="guide",
            agents=("builder", "pilot"),
            workflows=("import",) if workflows else (),
        )
        self.pool = builder.host(host).build()
        self.pool.configure_runtime_database_url("postgresql://test/runtime")
        await self.pool.start()
        self.addAsyncCleanup(self.pool.close)
        self.provider = provider
        self.companion = self.pool._agent_provider.agent_servers[COMPANION_AGENT_ID].runtime
        self.store = self.pool.get_runtime_store()
        self.memory = self.companion.store

    async def _submit(self, message: str, session_id: str = "s-1") -> str:
        accepted = await self.pool.submit_host_request(
            "guide", message=message, session_id=session_id, app_id="app-1", policy_id="p-1"
        )
        return accepted["request_id"]

    async def _end(self, agent_id: str, request_id: str) -> dict[str, Any]:
        async for event in self.pool.get_client(agent_id).stream_response(request_id, timeout=15):
            if event.get("event") in {"request.completed", "request.error", "request.cancelled"}:
                self.assertEqual(event["event"], "request.completed", event)
                return event["data"]
        self.fail("no end frame")

    async def _interaction(self, request_id: str) -> dict[str, Any]:
        for _ in range(500):
            events = await self.store.list_request_events(request_id)
            opened = [e for e in events if e.event_type == RuntimeEventType.INTERACTION_START.value]
            if opened:
                return dict(opened[-1].payload)
            await asyncio.sleep(0.01)
        self.fail("no interaction opened")

    async def _route(self, message: str, session_id: str = "s-1") -> tuple[str, dict[str, Any]]:
        request_id = await self._submit(message, session_id)
        data = await self._end(COMPANION_AGENT_ID, request_id)
        route = data["response"]["metadata"]["routed"]
        await self._end(route["agent_id"], route["request_id"])
        return request_id, route


class AgentRouteTests(RouteTestCase):
    async def test_an_agent_route_starts_that_agents_request_in_the_session(self) -> None:
        await self._pool(RoutingLLMProvider("builder"))

        request_id, route = await self._route("build it")

        self.assertEqual(
            (route["agent_id"], route["picked_by"]), ("builder", "classifier")
        )
        self.assertEqual(route["request_id"], route_module.routed_request_id(request_id))
        identity = await self.store.get_request_identity(route["request_id"])
        self.assertEqual(
            (identity.agent_id, identity.session_id, identity.parent_request_id),
            ("builder", "s-1", request_id),
        )
        # One entry: the agent's, with the choice. The companion writes none.
        [entry] = await self.memory.read_entries("s-1")
        self.assertEqual(
            (entry["agent_id"], entry["user_message"], entry["agent_response"]),
            ("builder", "build it", "built it"),
        )
        self.assertEqual(entry["metadata"]["route"], {"choice": "builder", "picked_by": "classifier"})

    async def test_the_classifier_is_one_enum_call_over_the_board(self) -> None:
        await self._pool(RoutingLLMProvider(lambda m: "pilot" if "fly" in m else "builder"))
        await self._route("build it")

        await self._route("fly it")

        self.assertEqual(len(self.provider.requests), 2)
        request = self.provider.requests[-1]
        self.assertEqual(request.system, CLASSIFIER_PROMPT)
        self.assertEqual(request.tools, [])
        schema = request.provider_options["structured_output"]
        self.assertEqual(
            schema["properties"]["route"]["enum"], ["builder", "pilot", "about", "unclear"]
        )
        text = request.messages[0].content[0].data["text"]
        self.assertIn("- builder | Research: Research specialist", text)
        self.assertIn("BOARD\n[entry 1 · builder]\nUser: build it\nbuilder: built it", text)
        self.assertTrue(text.endswith("MESSAGE\nfly it"), text)
        entries = await self.memory.read_entries("s-1")
        self.assertEqual([e["agent_id"] for e in entries], ["builder", "pilot"])

    async def test_a_recovered_companion_request_does_not_classify_again(self) -> None:
        await self._pool(RoutingLLMProvider("builder"))
        request_id, route = await self._route("build it")

        events = await self.store.list_request_events(request_id)
        start = next(e for e in events if e.event_type == RuntimeEventType.INVOKE_AGENT_START.value)
        rerun = RequestRun(
            self.companion,
            request_id=request_id,
            message="build it",
            session_id="s-1",
            request_metadata=dict(start.payload["request_metadata"]),
        )
        await rerun.run()

        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(len(await self.memory.read_entries("s-1")), 1)
        again = await self.store.list_request_events(request_id)
        self.assertEqual(
            sum(e.event_type == RuntimeEventType.INVOKE_AGENT_END.value for e in again), 1
        )


class AnswersTests(RouteTestCase):
    async def test_about_answers_from_the_descriptions_with_no_more_calls(self) -> None:
        await self._pool(RoutingLLMProvider("about"), workflows=True)

        data = await self._end(COMPANION_AGENT_ID, await self._submit("what can you do?"))

        text = data["response"]["text"]
        self.assertIn("- Research (builder): Research specialist", text)
        self.assertIn("- Import (import): Imports a CSV of transactions.", text)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertEqual(await self.memory.read_entries("s-1"), [])

    async def test_unclear_asks_the_user_to_pick_an_agent(self) -> None:
        await self._pool(RoutingLLMProvider("unclear"))
        request_id = await self._submit("hmm")

        opened = await self._interaction(request_id)
        self.assertEqual(opened["type"], "choice")
        self.assertEqual(opened["schema"]["options"], ["builder", "pilot"])
        await self.pool.get_client(COMPANION_AGENT_ID).post_interaction(
            request_id, interaction_id=opened["interaction_id"], response=["pilot"]
        )
        data = await self._end(COMPANION_AGENT_ID, request_id)
        route = data["response"]["metadata"]["routed"]
        await self._end("pilot", route["request_id"])

        self.assertEqual((route["agent_id"], route["picked_by"]), ("pilot", "user"))
        [entry] = await self.memory.read_entries("s-1")
        self.assertEqual(entry["metadata"]["route"], {"choice": "pilot", "picked_by": "user"})


class WorkflowRouteTests(RouteTestCase):
    async def test_a_workflow_runs_standalone_after_the_user_fills_a_missing_field(self) -> None:
        await self._pool(
            RoutingLLMProvider("import", workflow_input={"csv_path": None, "dry_run": True}),
            workflows=True,
        )
        request_id = await self._submit("import my statement")

        opened = await self._interaction(request_id)
        self.assertEqual(opened["type"], "info")
        self.assertEqual(opened["prompt"], "To run Import I need csv_path: The CSV file to import.")
        await self.pool.get_client(COMPANION_AGENT_ID).post_interaction(
            request_id, interaction_id=opened["interaction_id"], response="/data/cc.csv"
        )
        data = await self._end(COMPANION_AGENT_ID, request_id)

        # The second call's schema is the workflow's input, every field nullable.
        input_call = self.provider.requests[1]
        self.assertEqual(input_call.system, WORKFLOW_INPUT_PROMPT)
        schema = input_call.provider_options["structured_output"]
        self.assertEqual(sorted(schema["properties"]), ["csv_path", "dry_run"])
        text = data["response"]["text"]
        self.assertIn("Ran Import (import): completed.", text)
        self.assertIn('"csv_path": "/data/cc.csv"', text)
        self.assertIn('"rows": 12', text)
        run = data["response"]["metadata"]["workflow_run"]
        self.assertEqual(run["status"], "completed")
        stored = await self.pool.get_workflow_service().get_run("import", run["run_id"])
        self.assertEqual((stored.app_id, stored.policy_id), ("app-1", "p-1"))
        # One board entry for the run: its input and outcome, by the companion.
        [entry] = await self.memory.read_entries("s-1")
        self.assertEqual(entry["agent_id"], COMPANION_AGENT_ID)
        self.assertEqual(entry["agent_response"], text)
        self.assertEqual(entry["metadata"]["type"], "workflow_result")
        self.assertEqual(
            entry["metadata"]["input"], {"csv_path": "/data/cc.csv", "dry_run": True}
        )
        # The workflow's steps never touch the board.
        self.assertEqual(
            {e["session_id"] for e in await self.memory.read_entries("s-1")}, {"s-1"}
        )

    async def test_a_refused_input_is_the_outcome_and_nothing_retries_it(self) -> None:
        await self._pool(
            RoutingLLMProvider("import", workflow_input={"csv_path": 5, "dry_run": "nope"}),
            workflows=True,
        )

        data = await self._end(COMPANION_AGENT_ID, await self._submit("import it"))

        self.assertIn("Ran Import (import): failed.", data["response"]["text"])
        [entry] = await self.memory.read_entries("s-1")
        self.assertEqual(entry["metadata"]["status"], "failed")


class RelayTests(RouteTestCase):
    async def test_only_the_companion_routes_and_only_into_its_own_session(self) -> None:
        await self._pool(RoutingLLMProvider("builder"))
        request_id, _ = await self._route("build it")
        relay_url = await self.pool._agent_provider._ensure_relay()

        async def submit(caller: str, session_id: str) -> int:
            token = derive_agent_token(DEFAULT_LOCAL_SECRET, caller)
            parent = {
                "kind": "conversation",
                "request_id": request_id,
                "agent_id": caller,
                "session_id": "s-1",
                "started_by": caller,
                "parent_request_id": request_id,
            }
            async with httpx.AsyncClient(base_url=f"{relay_url}/h2a") as http:
                response = await http.post(
                    "/agent/pilot/request",
                    json={"message": "x", "session_id": session_id, "parent": parent},
                    headers={"Authorization": f"Bearer {token}"},
                )
            return response.status_code

        self.assertEqual(await submit("builder", "s-1"), 403)
        self.assertEqual(await submit(COMPANION_AGENT_ID, "elsewhere"), 403)


if __name__ == "__main__":
    unittest.main()

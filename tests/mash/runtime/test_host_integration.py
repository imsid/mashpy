"""Integration tests for host-managed runtime server contracts."""

from __future__ import annotations

import asyncio
import contextvars
import os
import tempfile
import unittest
from unittest.mock import patch

from mash.runtime.client import AgentClientError
from mash.runtime.engine.dbos import register_runtime, require_runtime, unregister_runtime
from mash.runtime.engine.workflow import execute_request_workflow
from mash.runtime import HostBuilder
from mash.testing.runtime_fixtures import (
    build_delegating_spec,
    build_spec,
    metadata,
)
from mash.tools.subagent import derive_subagent_session_id

_IN_FAKE_DBOS_STEP: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "in_fake_dbos_step",
    default=False,
)


class _FakeWorkflowDBOS:
    @staticmethod
    async def run_step_async(_config, func, *args, **kwargs):
        token = _IN_FAKE_DBOS_STEP.set(True)
        try:
            return await func(*args, **kwargs)
        finally:
            _IN_FAKE_DBOS_STEP.reset(token)


class _StepRestrictedRequestEngine:
    def __init__(self, runtime, *, database_url: str) -> None:
        self._runtime = runtime
        self._database_url = database_url
        self._tasks: set[asyncio.Task[None]] = set()

    async def open(self) -> None:
        register_runtime(self._runtime)
        return None

    async def close(self) -> None:
        if self._tasks:
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.clear()
        unregister_runtime(self._runtime)

    async def start_request(
        self,
        *,
        request_id: str,
        message: str,
        session_id: str,
        request_metadata: dict[str, object],
    ) -> None:
        del self._database_url
        if _IN_FAKE_DBOS_STEP.get():
            raise AssertionError()
        task = asyncio.create_task(
            execute_request_workflow(
                self._runtime.app_id,
                request_id,
                message,
                session_id,
                dict(request_metadata or {}),
                require_runtime=require_runtime,
            ),
            name=f"StepRestrictedRequest-{self._runtime.app_id}-{request_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class AgentHostIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_host_builder_requires_primary(self) -> None:
        with self.assertRaises(ValueError):
            HostBuilder().build()

    def test_host_builder_composes_primary_and_subagent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                host = (
                    HostBuilder()
                    .primary(build_spec(agent_id="primary", response_text="primary-ok"))
                    .subagent(
                        build_spec(
                            agent_id="research",
                            response_text="research-ok",
                        ),
                        metadata=metadata(),
                    )
                    .build()
                )
                described = {item["agent_id"]: item for item in host.describe_agents()}
                self.assertEqual(described["primary"]["role"], "primary")
                self.assertEqual(described["research"]["role"], "subagent")

    async def test_host_starts_runtime_servers_and_client_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                host = HostBuilder().primary(
                    build_spec(agent_id="primary", response_text="primary-ok")
                ).build()
                await host.start()
                try:
                    client = host.get_client("primary")
                    request_id = await client.post_request("hello", session_id="s-1")
                    result = await _collect_terminal_payload(client, request_id, timeout=5)
                    self.assertEqual(result["response"]["text"], "primary-ok")

                    primary = host.get_agent("primary")
                    sessions = await primary.list_sessions()
                    self.assertEqual(len(sessions), 1)
                    self.assertEqual(sessions[0]["session_id"], "s-1")

                    definitions = primary.get_signal_definitions()
                    self.assertEqual(set(definitions.keys()), {"unused_tools", "unused_tool_tokens"})
                    self.assertEqual(definitions["unused_tools"]["value_type"], "string_list")
                    self.assertEqual(definitions["unused_tool_tokens"]["computed_at"], "turn_complete")

                    signal_rows = await primary.get_session_signals("s-1")
                    self.assertGreaterEqual(len(signal_rows), 1)
                    self.assertEqual(signal_rows[-1]["turn_id"], result["turn_id"])
                    self.assertIn("unused_tools", signal_rows[-1]["signals"])
                    self.assertIn("unused_tool_tokens", signal_rows[-1]["signals"])
                finally:
                    await host.close()

    async def test_host_start_does_not_self_probe_runtime_health_over_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                host = HostBuilder().primary(
                    build_spec(agent_id="primary", response_text="primary-ok")
                ).build()
                with patch(
                    "mash.runtime.client.AgentClient.health",
                    side_effect=AgentClientError("unexpected health probe"),
                ):
                    await host.start()
                try:
                    client = host.get_client("primary")
                    request_id = await client.post_request("hello", session_id="s-1")
                    result = await _collect_terminal_payload(client, request_id, timeout=5)
                    self.assertEqual(result["response"]["text"], "primary-ok")
                finally:
                    await host.close()

    async def test_subagent_invocation_uses_real_runtime_clients(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                host = (
                    HostBuilder()
                    .primary(
                        build_delegating_spec(
                            agent_id="primary-app",
                            final_text="delegated-ok",
                            subagent_id="research",
                            subagent_prompt="analyze",
                        )
                    )
                    .subagent(
                        build_spec(
                            agent_id="research",
                            response_text="research-ok",
                        ),
                        metadata=metadata(),
                    )
                    .build()
                )
                await host.start()
                try:
                    client = host.get_client("primary-app")
                    request_id = await client.post_request("delegate", session_id="s-1")
                    result = await _collect_terminal_payload(client, request_id, timeout=5)
                    self.assertEqual(result["response"]["text"], "delegated-ok")

                    research = host.get_agent("research")
                    expected_subagent_session = derive_subagent_session_id(
                        "primary-app",
                        "s-1",
                        "research",
                    )
                    turns = await research.store.get_turns(
                        session_id=expected_subagent_session,
                        app_id=research.app_id,
                        limit=1,
                    )
                    self.assertEqual(turns[-1]["user_message"], "analyze")
                    self.assertEqual(turns[-1]["metadata"]["primary_app_id"], "primary-app")
                    self.assertEqual(turns[-1]["metadata"]["primary_session_id"], "s-1")
                finally:
                    await host.close()

    async def test_subagent_invocation_starts_child_workflow_outside_step_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                with patch(
                    "mash.runtime.service.DBOSRequestEngine",
                    _StepRestrictedRequestEngine,
                ), patch("mash.runtime.engine.workflow.DBOS", _FakeWorkflowDBOS):
                    host = (
                        HostBuilder()
                        .primary(
                            build_delegating_spec(
                                agent_id="primary-app",
                                final_text="delegated-ok",
                                subagent_id="research",
                                subagent_prompt="analyze",
                            )
                        )
                        .subagent(
                            build_spec(
                                agent_id="research",
                                response_text="research-ok",
                            ),
                            metadata=metadata(),
                        )
                        .build()
                    )
                    await host.start()
                    try:
                        client = host.get_client("primary-app")
                        request_id = await client.post_request("delegate", session_id="s-1")
                        self.assertTrue(request_id)

                        result = await _collect_terminal_payload(client, request_id, timeout=5)
                        self.assertEqual(result["response"]["text"], "delegated-ok")

                        primary = host.get_agent("primary-app")
                        request_events = await primary.runtime_store.list_request_events(request_id)
                        subagent_events = [
                            event
                            for event in request_events
                            if event.event_type == "runtime.subagent.call.completed"
                        ]
                        self.assertEqual(len(subagent_events), 1)
                        self.assertTrue(
                            subagent_events[0]
                            .payload["result"]["metadata"]
                            .get("request_id")
                        )

                        research = host.get_agent("research")
                        expected_subagent_session = derive_subagent_session_id(
                            "primary-app",
                            "s-1",
                            "research",
                        )
                        turns = await research.store.get_turns(
                            session_id=expected_subagent_session,
                            app_id=research.app_id,
                            limit=1,
                        )
                        self.assertEqual(turns[-1]["user_message"], "analyze")
                    finally:
                        await host.close()

    async def test_request_error_emits_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                host = HostBuilder().primary(
                    build_spec(
                        agent_id="primary",
                        response_text="ok",
                        fail_on_message="boom",
                    )
                ).build()
                await host.start()
                try:
                    client = host.get_client("primary")
                    request_id = await client.post_request("boom", session_id="s-1")
                    events = await _collect_events(client, request_id, timeout=5)
                    self.assertEqual(events[-1]["event"], "request.error")
                    self.assertIn("boom", str(events[-1]["data"]["error"]))
                finally:
                    await host.close()

async def _collect_events(client, request_id: str, *, timeout: float) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    async for event in client.stream_response(request_id, timeout=timeout):
        events.append(event)
        if event.get("event") in {"request.completed", "request.error"}:
            break
    return events


async def _collect_terminal_payload(
    client,
    request_id: str,
    *,
    timeout: float,
) -> dict[str, object]:
    events = await _collect_events(client, request_id, timeout=timeout)
    terminal = events[-1]
    if terminal.get("event") != "request.completed":
        raise AssertionError(f"expected request.completed, got {terminal.get('event')}")
    payload = terminal.get("data")
    if not isinstance(payload, dict):
        raise AssertionError("terminal payload must be a dict")
    return payload


if __name__ == "__main__":
    unittest.main()

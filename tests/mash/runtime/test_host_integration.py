"""Integration tests for host/client/server runtime contracts."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from mash.core.config import AgentConfig, SystemPrompt
from mash.core.context import Context, Response, ToolCall
from mash.core.llm import LLMProvider
from mash.runtime import AgentSpec, MashAgentHost, MashAgentHostBuilder, SubAgentMetadata
from mash.runtime.session import derive_subagent_session_id
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry


class _FakeLLMProvider(LLMProvider):
    def create_message(
        self,
        *,
        model: str,
        system: SystemPrompt,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int,
        temperature: float = 1.0,
        betas: Optional[List[str]] = None,
        use_prompt_caching: bool = True,
    ) -> Any:
        raise NotImplementedError

    def parse_response(
        self,
        response: Any,
    ) -> tuple[str, List[ToolCall], List[Dict[str, Any]]]:
        raise NotImplementedError

    def set_event_logger(self, logger, session_id: str, app_id: str) -> None:
        del logger, session_id, app_id

    def set_trace_id(self, trace_id: Optional[str]) -> None:
        del trace_id


class _Definition(AgentSpec):
    def __init__(self, root: Path, *, app_id: str) -> None:
        self.root = root
        self.app_id = app_id

    def get_agent_id(self) -> str:
        return self.app_id

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return _FakeLLMProvider()

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(app_id=self.app_id, system_prompt=f"You are {self.app_id}.")


def _metadata() -> SubAgentMetadata:
    return SubAgentMetadata(
        display_name="Research",
        description="Research specialist",
        capabilities=["search", "summarize"],
        usage_guidance="Use for focused research tasks.",
    )


class MashAgentHostIntegrationTests(unittest.TestCase):
    def test_host_builder_requires_primary(self) -> None:
        with self.assertRaises(ValueError):
            MashAgentHostBuilder().build()

    def test_host_builder_composes_primary_and_subagent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                host = (
                    MashAgentHostBuilder()
                    .primary(_Definition(root, app_id="primary"), agent_id="primary")
                    .subagent(_Definition(root, app_id="research"), agent_id="research", metadata=_metadata())
                    .build()
                )
                try:
                    self.assertEqual(host.get_primary_agent_id(), "primary")
                    described = {item["agent_id"]: item for item in host.describe_agents()}
                    self.assertEqual(described["primary"]["role"], "primary")
                    self.assertEqual(described["research"]["role"], "subagent")
                finally:
                    host.close()

    def test_host_wires_primary_and_subagent_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                host = MashAgentHost()
                host.register_primary(_Definition(root, app_id="primary"), agent_id="primary")
                host.register_subagent(
                    _Definition(root, app_id="research"),
                    agent_id="research",
                    metadata=_metadata(),
                )
                host.start()
                try:
                    primary = host.get_agent("primary")
                    self.assertEqual(primary.get_subagent_ids(), ["research"])
                    self.assertIn("InvokeSubagent", primary.agent.tools)
                    self.assertIn("SUBAGENTS", str(primary.system_prompt))
                    self.assertEqual(sorted(host.list_agents()), ["primary", "research"])
                finally:
                    host.close()

    def test_client_post_and_stream_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                host = MashAgentHost()
                host.register_primary(_Definition(root, app_id="primary"), agent_id="primary")
                host.start()
                try:
                    runtime = host.get_agent("primary")
                    client = host.get_client("primary")
                    response = Response(
                        text="primary-ok",
                        context=Context(),
                        metadata={"trace_id": "trace-primary"},
                    )
                    with patch.object(runtime.agent, "run", return_value=response):
                        request_id = client.post_request("hello", session_id="s-1")
                        events = []
                        for event in client.stream(request_id, timeout=30):
                            events.append(event)
                            if event.get("event") in {"request.completed", "request.error"}:
                                break

                    event_names = [event["event"] for event in events]
                    self.assertEqual(event_names[0], "request.accepted")
                    self.assertIn("request.started", event_names)
                    self.assertEqual(event_names[-1], "request.completed")
                    self.assertEqual(
                        events[-1]["data"]["response"]["text"],
                        "primary-ok",
                    )
                    self.assertEqual(events[-1]["data"]["session_id"], "s-1")
                finally:
                    host.close()

    def test_invoke_subagent_tool_uses_host_client_server_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                host = MashAgentHost()
                host.register_primary(_Definition(root, app_id="primary-app"), agent_id="primary")
                host.register_subagent(
                    _Definition(root, app_id="research-app"),
                    agent_id="research",
                    metadata=_metadata(),
                )
                host.start()
                try:
                    primary = host.get_agent("primary")
                    research = host.get_agent("research")
                    tool = primary.agent.tools.get("InvokeSubagent")
                    self.assertIsNotNone(tool)

                    response = Response(
                        text="research-ok",
                        context=Context(),
                        metadata={"trace_id": "trace-research"},
                    )
                    with patch.object(research.agent, "run", return_value=response):
                        result = tool.execute(  # type: ignore[union-attr]
                            {"agent_id": "research", "prompt": "analyze", "opts": {"timeout_ms": 1500}}
                        )

                    self.assertFalse(result.is_error)
                    payload = json.loads(result.content)
                    expected_subagent_session = derive_subagent_session_id(
                        "primary-app",
                        primary.get_default_session_id(),
                        "research",
                    )
                    self.assertEqual(payload["agent_id"], "research")
                    self.assertEqual(payload["subagent_session_id"], expected_subagent_session)
                    self.assertEqual(payload["text"], "research-ok")

                    turns = research.store.get_turns(session_id=expected_subagent_session, limit=1)
                    self.assertEqual(turns[-1]["user_message"], "analyze")
                    self.assertEqual(turns[-1]["metadata"]["primary_app_id"], "primary-app")
                    self.assertEqual(turns[-1]["metadata"]["primary_session_id"], primary.get_default_session_id())
                finally:
                    host.close()

    def test_client_get_preferences_reads_session_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                host = MashAgentHost()
                host.register_primary(_Definition(root, app_id="primary"), agent_id="primary")
                host.start()
                try:
                    client = host.get_client("primary")
                    client.set_preferences("s-1", {"tone": "brief"})
                    self.assertEqual(client.get_preferences("s-1"), {"tone": "brief"})
                finally:
                    host.close()

    def test_client_lists_persisted_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {"MASH_DATA_DIR": tmp}):
                host = MashAgentHost()
                host.register_primary(_Definition(root, app_id="primary"), agent_id="primary")
                host.start()
                try:
                    client = host.get_client("primary")
                    primary = host.get_agent("primary")
                    response = Response(text="ok", context=Context())
                    with patch.object(primary.agent, "run", return_value=response):
                        client.invoke("hello", session_id="s-1")
                    sessions = client.list_sessions()
                    self.assertEqual(len(sessions), 1)
                    self.assertEqual(sessions[0]["session_id"], "s-1")
                finally:
                    host.close()


if __name__ == "__main__":
    unittest.main()

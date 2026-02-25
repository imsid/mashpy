"""Tests for the AbstractMashApp SDK protocol."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from mash.cli.app import AbstractMashApp
from mash.core.config import AgentConfig, SystemPrompt
from mash.core.context import ToolCall
from mash.core.llm import LLMProvider
from mash.memory.store import SQLiteStore
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


class _TestApp(AbstractMashApp):
    def __init__(self, tmpdir: Path, *, app_id: str = "test-app") -> None:
        self._tmpdir = tmpdir
        self._app_id = app_id
        super().__init__()

    def get_app_id(self) -> str:
        return self._app_id

    def build_store(self):
        return SQLiteStore(str(self._tmpdir / "state.db"))

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return _FakeLLMProvider()

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=self._app_id,
            system_prompt="You are a test app.",
        )

    def get_log_destination(self) -> Path:
        return self._tmpdir / "logs" / "events.jsonl"


class _MismatchedApp(_TestApp):
    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id="different-app-id",
            system_prompt="Mismatch",
        )


class AbstractMashAppTests(unittest.TestCase):
    def test_abstract_base_enforced(self) -> None:
        class _IncompleteApp(AbstractMashApp):
            pass

        with self.assertRaises(TypeError):
            _IncompleteApp()

    def test_boots_with_default_commands_and_cleanup_without_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = _TestApp(Path(tmp))
            names = [cmd.name for cmd in app.command_registry.list_commands()]
            self.assertIn("help", names)
            self.assertIn("prefs", names)
            self.assertIn("app_data", names)
            app.cleanup()  # no MCP manager created

    def test_app_id_mismatch_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                _MismatchedApp(Path(tmp))


if __name__ == "__main__":
    unittest.main()

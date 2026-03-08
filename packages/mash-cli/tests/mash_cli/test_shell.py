"""Tests for composed CLI shell behavior."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from mash_cli.commands import Command
from mash_cli.shell import CLIAppShell, SubagentRegistration
from mash.core.config import AgentConfig, SystemPrompt
from mash.core.context import ToolCall
from mash.core.llm import LLMProvider
from mash.memory.store import SQLiteStore
from mash.runtime.definition import MashRuntimeDefinition
from mash.runtime.types import SubAgentMetadata
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


class _BaseDefinition(MashRuntimeDefinition):
    def __init__(self, root: Path, *, app_id: str = "test-app") -> None:
        self.root = root
        self.app_id = app_id

    def get_app_id(self) -> str:
        return self.app_id

    def build_store(self):
        return SQLiteStore(str(self.root / f"{self.app_id}.db"))

    def build_tools(self) -> ToolRegistry:
        return ToolRegistry()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return _FakeLLMProvider()

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(app_id=self.app_id, system_prompt="You are a test app.")

    def get_log_destination(self) -> Path:
        return self.root / "logs" / "events.jsonl"


class CLIAppShellTests(unittest.TestCase):
    def test_boots_with_default_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shell = CLIAppShell.from_definition(_BaseDefinition(Path(tmp)))
            names = [command.name for command in shell.command_registry.list_commands()]
            self.assertIn("help", names)
            self.assertIn("prefs", names)
            self.assertIn("app_data", names)
            shell.shutdown()

    def test_custom_command_can_be_registered_on_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shell = CLIAppShell.from_definition(_BaseDefinition(Path(tmp)))
            shell.register_command(
                Command(
                    name="ping",
                    help="Ping command",
                    handler=lambda ctx, _args: ctx.renderer.info("pong"),
                )
            )
            names = [command.name for command in shell.command_registry.list_commands()]
            self.assertIn("ping", names)
            shell.shutdown()

    def test_message_path_renders_compaction_and_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shell = CLIAppShell.from_definition(_BaseDefinition(Path(tmp)))
            with patch.object(
                shell.client,
                "invoke",
                return_value={
                    "session_id": shell.context.session_id,
                    "response": {
                        "text": "assistant reply",
                        "signals": {},
                        "metadata": {},
                    },
                    "compaction_summary_text": "summary text",
                    "compaction_summary_turn_id": "summary-turn",
                    "session_total_tokens": 42,
                },
            ):
                with patch.object(shell.context.renderer, "info") as info:
                    with patch.object(shell.context.renderer, "markdown") as markdown:
                        shell.handle_repl_message(shell.context, "hello")

            info.assert_any_call("Compaction triggered - summary checkpoint created.")
            markdown.assert_any_call("summary text")
            markdown.assert_any_call("assistant reply")
            shell.shutdown()

    def test_session_command_prints_primary_and_subagents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shell = CLIAppShell.from_definition(_BaseDefinition(Path(tmp)))
            shell.client.set_subagent_ids(["research"])
            with patch.object(shell.context.renderer, "info") as info:
                shell.command_registry.execute(shell.context, "/session")
            lines = [call.args[0] for call in info.call_args_list]
            self.assertIn("Primary agent: test-app", lines)
            self.assertIn("Subagents: research", lines)
            shell.shutdown()

    def test_session_command_omits_subagents_when_none_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shell = CLIAppShell.from_definition(_BaseDefinition(Path(tmp)))
            with patch.object(shell.context.renderer, "info") as info:
                shell.command_registry.execute(shell.context, "/session")
            lines = [call.args[0] for call in info.call_args_list]
            self.assertIn("Primary agent: test-app", lines)
            self.assertFalse(any(line.startswith("Subagents:") for line in lines))
            shell.shutdown()

    def test_unknown_command_is_non_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shell = CLIAppShell.from_definition(_BaseDefinition(Path(tmp)))
            with patch.object(shell.context.renderer, "warn") as warn:
                handled = shell.command_registry.execute(shell.context, "/does_not_exist")
            self.assertTrue(handled)
            self.assertTrue(warn.called)
            shell.shutdown()

    def test_from_definition_registers_optional_subagents_via_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shell = CLIAppShell.from_definition(
                _BaseDefinition(root, app_id="primary-app"),
                subagents=[
                    SubagentRegistration(
                        definition=_BaseDefinition(root, app_id="research-app"),
                        agent_id="research",
                        metadata=SubAgentMetadata(
                            display_name="Research",
                            description="Research helper",
                            capabilities=["search"],
                            usage_guidance="Use for repository analysis.",
                        ),
                    )
                ],
            )
            with patch.object(shell.context.renderer, "info") as info:
                shell.command_registry.execute(shell.context, "/session")
            lines = [call.args[0] for call in info.call_args_list]
            self.assertIn("Subagents: research", lines)
            shell.shutdown()


if __name__ == "__main__":
    unittest.main()

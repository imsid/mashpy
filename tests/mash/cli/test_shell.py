"""Tests for remote CLI shell behavior."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from mash.cli.shell import MashRemoteShell, ShellTarget
from mash.runtime import derive_subagent_session_id


class _FakeClient:
    def health(self):
        return {"deployment": {"primary_agent_id": "primary", "agents": [{"agent_id": "primary", "role": "primary"}]}}

    def list_agents(self):
        return [{"agent_id": "primary", "role": "primary"}, {"agent_id": "research", "role": "subagent"}]

    def get_session(self, agent_id: str, session_id: str):
        return {
            "agent_id": agent_id,
            "session_id": session_id,
            "model": "claude-test",
            "max_steps": 8,
            "session_total_tokens": 12,
        }

    def list_sessions(self, agent_id: str):
        return [{"session_id": "s-1", "turn_count": 2, "session_total_tokens": 12, "agent_id": agent_id}]

    def get_history(self, agent_id: str, session_id: str, *, limit=None):
        del agent_id, session_id, limit
        return [{"user_message": "hello", "agent_response": "hi"}]

    def submit_request(self, agent_id: str, *, message: str, session_id: str | None = None, turn_metadata=None):
        del agent_id, message, session_id, turn_metadata
        return "req-1"

    def stream_request(self, agent_id: str, request_id: str):
        del agent_id, request_id
        yield {
            "event": "agent.trace",
            "data": {
                "event_type": "agent.think.complete",
                "trace_id": "trace-1",
                "step_id": 0,
                "duration_ms": 12,
                "action_type": "response",
                "tool_calls": [],
                "token_usage": {"input": 2, "output": 1},
                "payload": {"assistant_text": "draft"},
            },
        }
        yield {
            "event": "agent.trace",
            "data": {
                "event_type": "agent.step.complete",
                "trace_id": "trace-1",
                "step_id": 0,
                "duration_ms": 15,
                "action_type": "response",
                "tool_calls": [],
                "token_usage": {"input": 2, "output": 1},
                "payload": {},
            },
        }
        yield {
            "event": "request.completed",
            "data": {"session_id": "s-1", "response": {"text": "echo: hello"}},
        }


class MashRemoteShellTests(unittest.TestCase):
    def _build_shell(self) -> MashRemoteShell:
        return MashRemoteShell(
            _FakeClient(),
            ShellTarget(api_base_url="http://localhost:8000", agent_id="primary", session_id="s-1"),
        )

    def test_boots_with_remote_commands(self) -> None:
        shell = self._build_shell()
        command_names = [command.name for command in shell.command_registry.list_commands()]
        self.assertIn("status", command_names)
        self.assertIn("agents", command_names)
        self.assertIn("sessions", command_names)
        self.assertIn("use", command_names)

    def test_session_command_reads_remote_session(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "info") as info:
            shell.command_registry.execute(shell.context, "/session")
        lines = [call.args[0] for call in info.call_args_list]
        self.assertIn("Agent: primary", lines)
        self.assertIn("Session ID: s-1", lines)

    def test_use_command_switches_agent(self) -> None:
        shell = self._build_shell()
        shell.command_registry.execute(shell.context, "/use research")
        self.assertEqual(shell.context.agent_id, "research")
        self.assertEqual(
            shell.context.session_id,
            derive_subagent_session_id("primary", "s-1", "research"),
        )

    def test_use_command_restores_primary_session_after_switching_back(self) -> None:
        shell = self._build_shell()
        shell.command_registry.execute(shell.context, "/use research")
        shell.command_registry.execute(shell.context, "/use primary")
        self.assertEqual(shell.context.agent_id, "primary")
        self.assertEqual(shell.context.session_id, "s-1")

    def test_handle_repl_message_renders_remote_response(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "markdown") as markdown:
            shell.handle_repl_message(shell.context, "hello")
        markdown.assert_called_once_with("echo: hello")
        self.assertEqual(shell.context.session_ids["primary"], "s-1")

    def test_handle_repl_message_streams_chain_events(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.chain_renderer, "on_think_complete") as think_complete:
            with patch.object(shell.chain_renderer, "on_step_complete") as step_complete:
                with patch.object(shell.chain_renderer, "finish_trace") as finish_trace:
                    shell.handle_repl_message(shell.context, "hello")
        think_complete.assert_called_once()
        step_complete.assert_called_once()
        finish_trace.assert_called_once()

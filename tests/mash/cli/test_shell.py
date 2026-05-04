"""Tests for remote CLI shell behavior."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from rich.console import Console

from mash.cli.chain_renderer import ChainOfThoughtRenderer
from mash.cli.shell import MashRemoteShell, ShellTarget
from mash.logging.events import AgentTraceEvent
from mash.tools.subagent import derive_subagent_session_id


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

    def submit_request(self, agent_id: str, *, message: str, session_id: str | None = None):
        del agent_id, message, session_id
        return "req-1"

    def stream_request(self, agent_id: str, request_id: str):
        del agent_id, request_id
        yield {
            "event": "agent.trace",
            "data": {
                "event_type": "subagent.request.started",
                "payload": {
                    "agent_id": "research",
                    "data": {"request_id": "sub-1"},
                },
            },
        }
        yield {
            "event": "agent.trace",
            "data": {
                "event_type": "subagent.agent.trace",
                "payload": {
                    "agent_id": "research",
                    "data": {
                        "event_type": "agent.think.complete",
                        "trace_id": "trace-sub-1",
                        "step_id": 0,
                        "duration_ms": 7,
                        "action_type": "tool_call",
                        "tool_calls": ["bash"],
                        "token_usage": {"input": 1, "output": 1},
                        "payload": {"assistant_text": "checking cli flow"},
                    },
                },
            },
        }
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
            "event": "agent.trace",
            "data": {
                "event_type": "subagent.request.completed",
                "payload": {
                    "agent_id": "research",
                    "data": {"status": "completed"},
                },
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
        self.assertEqual(
            [call.args[0] for call in markdown.call_args_list],
            ["draft", "echo: hello"],
        )
        self.assertEqual(shell.context.session_ids["primary"], "s-1")

    def test_handle_repl_message_deduplicates_streamed_and_terminal_response(self) -> None:
        shell = self._build_shell()

        def stream_same_text(_agent_id: str, _request_id: str):
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
                    "payload": {"assistant_text": "echo: hello"},
                },
            }
            yield {
                "event": "request.completed",
                "data": {"session_id": "s-1", "response": {"text": "echo: hello"}},
            }

        shell.client.stream_request = stream_same_text
        with patch.object(shell.context.renderer, "markdown") as markdown:
            shell.handle_repl_message(shell.context, "hello")
        markdown.assert_called_once_with("echo: hello")

    def test_handle_repl_message_does_not_stream_terminal_finish_preview(self) -> None:
        shell = self._build_shell()

        def stream_finish_preview(_agent_id: str, _request_id: str):
            yield {
                "event": "agent.trace",
                "data": {
                    "event_type": "runtime.llm.think.completed",
                    "trace_id": "trace-1",
                    "loop_index": 0,
                    "payload": {
                        "action_type": "finish",
                        "assistant_text": "preview that should not render early",
                        "tool_calls": [],
                        "token_usage": {"input": 2, "output": 1},
                        "duration_ms": 123,
                    },
                },
            }
            yield {
                "event": "request.completed",
                "data": {
                    "session_id": "s-1",
                    "response": {"text": "final response"},
                },
            }

        shell.client.stream_request = stream_finish_preview
        with patch.object(shell.context.renderer, "markdown") as markdown:
            shell.handle_repl_message(shell.context, "hello")
        markdown.assert_called_once_with("final response")

    def test_handle_repl_message_renders_runtime_think_events(self) -> None:
        shell = self._build_shell()

        def stream_runtime_think(_agent_id: str, _request_id: str):
            yield {
                "event": "agent.trace",
                "data": {
                    "event_type": "runtime.llm.think.completed",
                    "trace_id": "trace-1",
                    "loop_index": 0,
                    "payload": {
                        "action_type": "response",
                        "assistant_text": "streamed from runtime",
                        "tool_calls": [],
                        "token_usage": {"input": 2, "output": 1},
                    },
                },
            }
            yield {
                "event": "request.completed",
                "data": {
                    "session_id": "s-1",
                    "response": {"text": "final response"},
                },
            }

        shell.client.stream_request = stream_runtime_think
        with patch.object(shell.context.renderer, "markdown") as markdown:
            shell.handle_repl_message(shell.context, "hello")
        self.assertEqual(
            [call.args[0] for call in markdown.call_args_list],
            ["streamed from runtime", "final response"],
        )

    def test_normalize_runtime_trace_payload_preserves_durations(self) -> None:
        think = MashRemoteShell._normalize_runtime_trace_payload(
            {
                "event_type": "runtime.llm.think.completed",
                "trace_id": "trace-1",
                "loop_index": 2,
                "payload": {
                    "action_type": "tool_call",
                    "assistant_text": "thinking",
                    "tool_calls": [{"name": "bash", "arguments": {"command": "pwd"}}],
                    "token_usage": {"input": 2, "output": 1},
                    "duration_ms": 123,
                },
            }
        )
        act = MashRemoteShell._normalize_runtime_trace_payload(
            {
                "event_type": "runtime.tool.call.completed",
                "trace_id": "trace-1",
                "loop_index": 2,
                "payload": {
                    "tool_name": "bash",
                    "duration_ms": 45,
                },
            }
        )

        self.assertEqual(think["event_type"], "agent.think.complete")
        self.assertEqual(think["duration_ms"], 123)
        self.assertEqual(act["event_type"], "agent.act.complete")
        self.assertEqual(act["duration_ms"], 45)

    def test_handle_repl_message_streams_chain_events(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.chain_renderer, "on_think_complete") as think_complete:
            with patch.object(shell.chain_renderer, "on_step_complete") as step_complete:
                with patch.object(shell.chain_renderer, "finish_trace") as finish_trace:
                    shell.handle_repl_message(shell.context, "hello")
        self.assertEqual(think_complete.call_count, 2)
        step_complete.assert_called_once()
        finish_trace.assert_called_once()

    def test_handle_repl_message_renders_subagent_lifecycle(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.renderer, "info") as info:
            shell.handle_repl_message(shell.context, "hello")
        info.assert_any_call("Subagent research started")
        info.assert_any_call("Subagent research completed")

    def test_handle_repl_message_ignores_null_subagent_payloads(self) -> None:
        shell = self._build_shell()

        def stream_with_null_subagent_payload(_agent_id: str, _request_id: str):
            yield {
                "event": "agent.trace",
                "data": {
                    "event_type": "subagent.request.error",
                    "payload": None,
                },
            }
            yield {
                "event": "request.completed",
                "data": {"session_id": "s-1", "response": {"text": "echo: hello"}},
            }

        shell.client.stream_request = stream_with_null_subagent_payload
        with patch.object(shell.renderer, "error") as error:
            shell.handle_repl_message(shell.context, "hello")
        error.assert_called_once_with("Subagent subagent error: request failed")


class ChainOfThoughtRendererTests(unittest.TestCase):
    def test_think_events_use_step_id_for_display_when_step_complete_is_missing(self) -> None:
        console = Console(record=True, width=120)
        renderer = ChainOfThoughtRenderer(console)
        renderer.on_think_complete(
            AgentTraceEvent(
                event_type="agent.think.complete",
                app_id="primary",
                session_id="s-1",
                trace_id="trace-1",
                step_id=0,
                duration_ms=10,
                action_type="tool_call",
                tool_calls=["bash"],
                token_usage={"input": 10, "output": 1},
            )
        )
        renderer.on_think_complete(
            AgentTraceEvent(
                event_type="agent.think.complete",
                app_id="primary",
                session_id="s-1",
                trace_id="trace-1",
                step_id=1,
                duration_ms=12,
                action_type="tool_call",
                tool_calls=["bash"],
                token_usage={"input": 12, "output": 1},
            )
        )

        output = console.export_text()
        self.assertIn("Step 1:", output)
        self.assertIn("Step 2:", output)

    def test_summary_uses_think_duration_when_step_complete_is_missing(self) -> None:
        console = Console(record=True, width=120)
        renderer = ChainOfThoughtRenderer(console)
        renderer.on_think_complete(
            AgentTraceEvent(
                event_type="agent.think.complete",
                app_id="primary",
                session_id="s-1",
                trace_id="trace-1",
                step_id=0,
                duration_ms=13447,
                action_type="finish",
                tool_calls=[],
                token_usage={"input": 1320, "output": 1406},
            )
        )

        renderer.finish_trace()

        output = console.export_text()
        self.assertIn("Agent Execution Complete:", output)
        self.assertIn("2,726 tokens", output)
        self.assertIn("13,447ms", output)

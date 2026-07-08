"""Tests for remote CLI shell behavior."""

from __future__ import annotations

from typing import Any
import unittest
from unittest.mock import patch

from rich.console import Console

from mash.cli.chain_renderer import ChainOfThoughtRenderer
from mash.cli.shell import MashRemoteShell, ShellTarget
from mash.runtime.events import RuntimeEvent, RuntimeEventType, build_runtime_trace


class _FakeClient:
    def __init__(self) -> None:
        self.workflow_runs: list[dict[str, Any]] = []
        self.workflow_status_requests: list[dict[str, str]] = []
        self.host_workflows: list[str] = []
        self.feedback: list[dict[str, Any]] = []
        self.interactions: list[dict[str, Any]] = []
        self.emit_workflow_interaction = False
        self.workflow_structured_output: dict[str, Any] | None = None

    def health(self):
        return {
            "deployment": {
                "agents": [{"agent_id": "primary", "metadata": {"display_name": "Primary"}}],
                "hosts": [
                    {
                        "host_id": "assistant",
                        "primary": "primary",
                        "subagents": ["research"],
                        "workflows": [],
                    }
                ],
            }
        }

    def list_agents(self):
        return [
            {"agent_id": "primary", "metadata": {"display_name": "Primary"}},
            {"agent_id": "research", "metadata": {"display_name": "Research"}},
        ]

    def list_hosts(self):
        return [
            {
                "host_id": "assistant",
                "primary": "primary",
                "subagents": ["research"],
                "workflows": [],
            }
        ]

    def get_host(self, host_id: str):
        return {
            "host_id": host_id,
            "primary": {"agent_id": "primary", "metadata": {"display_name": "Primary"}},
            "subagents": [{"agent_id": "research", "metadata": {"display_name": "Research"}}],
            "workflows": list(self.host_workflows),
        }

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

    def list_workflows(self, *, host: str | None = None):
        workflows = [
            {
                "workflow_id": "changelog",
                "tasks": [{"task_id": "scan", "agent_id": "worker"}],
            }
        ]
        if host is None:
            return workflows
        # Mirrors the server-side `?host=` filter on GET /v1/workflow.
        return [w for w in workflows if w["workflow_id"] in self.host_workflows]

    def run_workflow(
        self,
        workflow_id: str,
        *,
        dedup_key: str | None = None,
        workflow_input: dict[str, Any] | None = None,
    ):
        self.workflow_runs.append(
            {
                "workflow_id": workflow_id,
                "dedup_key": dedup_key,
                "workflow_input": workflow_input,
            }
        )
        return {
            "workflow_id": workflow_id,
            "run_id": "mw:host:changelog:abc",
            "status": "queued",
        }

    def get_workflow_run(self, workflow_id: str, run_id: str):
        self.workflow_status_requests.append({"workflow_id": workflow_id, "run_id": run_id})
        return {
            "workflow_id": workflow_id,
            "run_id": run_id,
            "dedup_key": "manual",
            "status": "completed",
            "created_at": 1.0,
            "started_at": 2.0,
            "finished_at": 3.0,
            "error": None,
            "output": {"task_states": {"digest-traces": {"status": "ok"}}},
        }

    def submit_request(self, agent_id: str, *, message: str, session_id: str | None = None):
        del agent_id, message, session_id
        return "req-1"

    def submit_feedback(self, agent_id: str, **kwargs: Any):
        self.feedback.append({"agent_id": agent_id, **kwargs})
        return {"feedback": {"feedback_id": len(self.feedback)}}

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
                        "event_type": "runtime.llm.think.completed",
                        "trace_id": "trace-sub-1",
                        "session_id": "subagent:research:x",
                        "loop_index": 0,
                        "created_at": 10.0,
                        "payload": {
                            "duration_ms": 7,
                            "action_type": "tool_call",
                            "assistant_text": "checking cli flow",
                            "tool_calls": [{"name": "bash", "arguments": {"command": "pwd"}}],
                            "token_usage": {"input": 1, "output": 1},
                        },
                    },
                },
            },
        }
        yield {
            "event": "agent.trace",
            "data": {
                "event_type": "runtime.llm.think.completed",
                "trace_id": "trace-1",
                "session_id": "s-1",
                "loop_index": 0,
                "created_at": 100.0,
                "payload": {
                    "duration_ms": 12,
                    "action_type": "response",
                    "assistant_text": "draft",
                    "tool_calls": [],
                    "token_usage": {"input": 2, "output": 1},
                },
            },
        }
        yield {
            "event": "agent.trace",
            "data": {
                "event_type": "runtime.step.completed",
                "trace_id": "trace-1",
                "session_id": "s-1",
                "loop_index": 0,
                "created_at": 101.0,
                "payload": {
                    "duration_ms": 15,
                    "action_type": "response",
                    "tool_calls": [],
                },
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

    def post_interaction(self, agent_id, request_id, *, interaction_id, response):
        self.interactions.append(
            {
                "agent_id": agent_id,
                "request_id": request_id,
                "interaction_id": interaction_id,
                "response": response,
            }
        )

    def stream_workflow_run(self, workflow_id: str, run_id: str):
        del run_id
        task_id = "scan"
        task_agent_id = "worker"
        response_text = "{\"status\":\"ok\"}"
        yield {
            "event": "workflow.status",
            "data": {
                "workflow_id": workflow_id,
                "run_id": "mw:host:changelog:abc",
                "status": "running",
            },
        }
        yield {
            "event": "workflow.task.started",
            "data": {
                "workflow_id": workflow_id,
                "run_id": "mw:host:changelog:abc",
                "task_id": task_id,
                "task_agent_id": task_agent_id,
            },
        }
        if self.emit_workflow_interaction:
            yield {
                "event": "request.interaction.create",
                "data": {
                    "workflow_id": workflow_id,
                    "run_id": "mw:host:changelog:abc",
                    "task_id": task_id,
                    "task_agent_id": task_agent_id,
                    "request_id": "task-req-1",
                    "interaction_id": "int-1",
                    "type": "info",
                    "prompt": "What is your name?",
                },
            }
            yield {
                "event": "request.interaction.ack",
                "data": {
                    "workflow_id": workflow_id,
                    "run_id": "mw:host:changelog:abc",
                    "task_id": task_id,
                    "task_agent_id": task_agent_id,
                    "interaction_id": "int-1",
                    "response": "Ada",
                },
            }
        yield {
            "event": "agent.trace",
            "data": {
                "workflow_id": workflow_id,
                "run_id": "mw:host:changelog:abc",
                "task_id": task_id,
                "task_agent_id": task_agent_id,
                "event_type": "runtime.llm.think.completed",
                "trace_id": "trace-wf-1",
                "session_id": "workflow:changelog:task:scan:run:mw:host:changelog:abc",
                "loop_index": 0,
                "created_at": 100.0,
                "payload": {
                    "duration_ms": 9,
                    "action_type": "response",
                    "assistant_text": response_text,
                    "tool_calls": [],
                    "token_usage": {"input": 2, "output": 1},
                },
            },
        }
        completed_response: dict[str, Any] = {"text": response_text}
        if self.workflow_structured_output is not None:
            completed_response["structured_output"] = self.workflow_structured_output
        yield {
            "event": "request.completed",
            "data": {
                "workflow_id": workflow_id,
                "run_id": "mw:host:changelog:abc",
                "task_id": task_id,
                "task_agent_id": task_agent_id,
                "response": completed_response,
            },
        }
        yield {
            "event": "workflow.task.completed",
            "data": {
                "workflow_id": workflow_id,
                "run_id": "mw:host:changelog:abc",
                "task_id": task_id,
                "task_agent_id": task_agent_id,
            },
        }
        yield {
            "event": "workflow.status",
            "data": {
                "workflow_id": workflow_id,
                "run_id": "mw:host:changelog:abc",
                "status": "completed",
            },
        }


class MashRemoteShellTests(unittest.TestCase):
    def _build_shell(self) -> MashRemoteShell:
        return MashRemoteShell(
            _FakeClient(),
            ShellTarget(api_base_url="http://localhost:8000", agent_id="primary", session_id="s-1"),
        )

    def _build_host_shell(self, *, host_workflows: list[str] | None = None) -> MashRemoteShell:
        client = _FakeClient()
        client.host_workflows = list(host_workflows or [])
        return MashRemoteShell(
            client,
            ShellTarget(
                api_base_url="http://localhost:8000",
                agent_id="primary",
                session_id="s-1",
                host_id="assistant",
            ),
        )

    def test_boots_with_remote_commands(self) -> None:
        shell = self._build_shell()
        command_names = [command.name for command in shell.command_registry.list_commands()]
        self.assertIn("status", command_names)
        self.assertIn("agent", command_names)
        self.assertIn("sessions", command_names)
        self.assertIn("host", command_names)
        self.assertNotIn("use", command_names)
        self.assertNotIn("agents", command_names)
        self.assertNotIn("hosts", command_names)
        self.assertIn("workflow", command_names)
        self.assertNotIn("changelog", command_names)

    def test_feedback_command_records_session_context(self) -> None:
        shell = self._build_host_shell()
        shell.context.last_request_id = "req-1"
        with patch.object(shell.context.renderer, "info") as info:
            shell.command_registry.execute(
                shell.context, "/feedback the trace output is hard to read"
            )
        self.assertEqual(len(shell.client.feedback), 1)
        recorded = shell.client.feedback[0]
        self.assertEqual(recorded["agent_id"], "primary")
        self.assertEqual(recorded["message"], "the trace output is hard to read")
        self.assertEqual(recorded["host_id"], "assistant")
        self.assertEqual(recorded["session_id"], "s-1")
        self.assertEqual(recorded["request_id"], "req-1")
        self.assertIn("Feedback recorded", info.call_args.args[0])

    def test_feedback_command_requires_message(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "error") as error:
            shell.command_registry.execute(shell.context, "/feedback")
        self.assertEqual(shell.client.feedback, [])
        error.assert_called_once_with("Usage: /feedback <message>")

    def test_handle_repl_message_tracks_last_request_id(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "markdown"):
            shell.handle_repl_message(shell.context, "hello")
        self.assertEqual(shell.context.last_request_id, "req-1")

    def test_session_command_reads_remote_session(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "info") as info:
            shell.command_registry.execute(shell.context, "/session")
        lines = [call.args[0] for call in info.call_args_list]
        self.assertIn("Agent: primary", lines)
        self.assertIn("Session ID: s-1", lines)

    def test_hosts_command_lists_hosts(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "table") as table:
            shell.command_registry.execute(shell.context, "/host")
        table.assert_called_once_with(
            ["Host", "Primary", "Subagents", "Workflows"],
            [["assistant", "primary", "research", ""]],
        )

    def test_agents_command_lists_pool_agents_without_host(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "table") as table:
            shell.command_registry.execute(shell.context, "/agent")
        table.assert_called_once_with(
            ["Agent", "Name"],
            [["primary", "Primary"], ["research", "Research"]],
        )

    def test_agents_command_host_scoped_shows_members_with_roles(self) -> None:
        shell = self._build_host_shell()
        with patch.object(shell.context.renderer, "table") as table:
            shell.command_registry.execute(shell.context, "/agent")
        table.assert_called_once_with(
            ["Agent", "Name", "Role"],
            [["primary", "Primary", "primary"], ["research", "Research", "subagent"]],
        )

    def test_workflow_list_host_scoped_filters_attached(self) -> None:
        shell = self._build_host_shell(host_workflows=["changelog"])
        with patch.object(shell.context.renderer, "table") as table:
            shell.command_registry.execute(shell.context, "/workflow list")
        table.assert_called_once_with(
            ["Workflow ID", "Steps"],
            [["changelog", "scan -> worker"]],
        )

    def test_workflow_list_host_scoped_without_attached_workflows(self) -> None:
        shell = self._build_host_shell()
        with patch.object(shell.context.renderer, "warn") as warn:
            shell.command_registry.execute(shell.context, "/workflow list")
        warn.assert_called_once_with("No workflows attached to host 'assistant'.")

    def test_workflow_run_host_scoped_refuses_unattached(self) -> None:
        shell = self._build_host_shell()
        with patch.object(shell.context.renderer, "error") as error:
            shell.command_registry.execute(shell.context, "/workflow run changelog manual")
        self.assertEqual(shell.client.workflow_runs, [])
        self.assertIn("not attached to host 'assistant'", error.call_args.args[0])
        self.assertIn("'changelog'", error.call_args.args[0])

    def test_workflow_run_host_scoped_allows_attached(self) -> None:
        shell = self._build_host_shell(host_workflows=["changelog"])
        with patch.object(shell.context.renderer, "info") as info:
            shell.command_registry.execute(shell.context, "/workflow run changelog manual")
        self.assertEqual(
            shell.client.workflow_runs,
            [{"workflow_id": "changelog", "dedup_key": "manual", "workflow_input": None}],
        )
        lines = [call.args[0] for call in info.call_args_list]
        self.assertIn("Workflow status: completed", lines)

    def test_workflow_run_handles_ask_user_interaction(self) -> None:
        shell = self._build_host_shell(host_workflows=["changelog"])
        shell.client.emit_workflow_interaction = True
        with patch("builtins.input", return_value="Ada"):
            shell.command_registry.execute(shell.context, "/workflow run changelog manual")
        # The response is posted to the TASK's agent and request, not ctx.agent_id.
        self.assertEqual(
            shell.client.interactions,
            [
                {
                    "agent_id": "worker",
                    "request_id": "task-req-1",
                    "interaction_id": "int-1",
                    "response": "Ada",
                }
            ],
        )

    def test_workflow_status_host_scoped_refuses_unattached(self) -> None:
        shell = self._build_host_shell()
        with patch.object(shell.context.renderer, "error") as error:
            shell.command_registry.execute(
                shell.context, "/workflow status changelog mw:host:changelog:abc"
            )
        self.assertEqual(shell.client.workflow_status_requests, [])
        self.assertIn("not attached to host 'assistant'", error.call_args.args[0])

    def test_workflow_status_host_scoped_allows_attached(self) -> None:
        shell = self._build_host_shell(host_workflows=["changelog"])
        run_id = "mw:host:changelog:abc"
        with patch.object(shell.context.renderer, "table"):
            shell.command_registry.execute(shell.context, f"/workflow status changelog {run_id}")
        self.assertEqual(
            shell.client.workflow_status_requests,
            [{"workflow_id": "changelog", "run_id": run_id}],
        )

    def test_workflow_command_lists_workflows(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "table") as table:
            shell.command_registry.execute(shell.context, "/workflow list")
        table.assert_called_once_with(
            ["Workflow ID", "Steps"],
            [["changelog", "scan -> worker"]],
        )

    def test_workflows_alias_is_not_registered(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "warn") as warn:
            shell.command_registry.execute(shell.context, "/workflows")
        warn.assert_called_once_with("Unknown command: /workflows. Try /help.")

    def test_workflow_run_starts_workflow(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "info") as info:
            shell.command_registry.execute(shell.context, "/workflow run changelog manual")
        self.assertEqual(
            shell.client.workflow_runs,
            [{"workflow_id": "changelog", "dedup_key": "manual", "workflow_input": None}],
        )
        lines = [call.args[0] for call in info.call_args_list]
        self.assertIn("Workflow: changelog", lines)
        self.assertIn("Run ID: mw:host:changelog:abc", lines)
        self.assertIn("Workflow status: running", lines)
        self.assertIn("Workflow task scan started", lines)
        self.assertIn("Workflow task scan completed", lines)
        self.assertIn("Workflow status: completed", lines)

    def test_workflow_run_streams_task_response_and_chain_events(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.chain_renderer, "on_runtime_event") as runtime_event:
            with patch.object(shell.chain_renderer, "finish_trace") as finish_trace:
                with patch.object(shell.context.renderer, "markdown") as markdown:
                    shell.command_registry.execute(shell.context, "/workflow run changelog")

        runtime_event.assert_called_once()
        self.assertEqual(
            runtime_event.call_args.args[0].event_type,
            RuntimeEventType.LLM_THINK_COMPLETED.value,
        )
        markdown.assert_called_once_with('{"status":"ok"}')
        # finish_trace is called once per task (inside request.completed) and
        # once more in the finally block after the stream ends.
        self.assertEqual(finish_trace.call_count, 2)

    def test_workflow_run_forwards_input_json(self) -> None:
        shell = self._build_shell()
        shell.command_registry.execute(
            shell.context,
            "/workflow run changelog manual --input '{\"x\":1}'",
        )
        self.assertEqual(
            shell.client.workflow_runs,
            [
                {
                    "workflow_id": "changelog",
                    "dedup_key": "manual",
                    "workflow_input": {"x": 1},
                }
            ],
        )

    def test_workflow_run_rejects_invalid_input_json_locally(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "error") as error:
            shell.command_registry.execute(shell.context, "/workflow run changelog --input '{bad}'")
        self.assertEqual(shell.client.workflow_runs, [])
        self.assertIn("Workflow input must be valid JSON", error.call_args.args[0])

    def test_workflow_run_rejects_non_object_input_json_locally(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "error") as error:
            shell.command_registry.execute(shell.context, "/workflow run changelog --input '[1]'")
        self.assertEqual(shell.client.workflow_runs, [])
        error.assert_called_once_with("Workflow input must be a JSON object")

    def test_workflow_status_fetches_run(self) -> None:
        shell = self._build_shell()
        run_id = "mw:host:changelog:abc"
        with patch.object(shell.context.renderer, "table") as table, patch.object(
            shell.context.renderer,
            "print",
        ) as print_:
            shell.command_registry.execute(shell.context, f"/workflow status changelog {run_id}")
        self.assertEqual(
            shell.client.workflow_status_requests,
            [{"workflow_id": "changelog", "run_id": run_id}],
        )
        rows = table.call_args.args[1]
        self.assertIn(["status", "completed"], rows)
        self.assertIn('"digest-traces"', print_.call_args.args[0])

    def test_workflow_run_usage_error_is_local(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "error") as error:
            shell.command_registry.execute(shell.context, "/workflow run")
        error.assert_called_once_with(
            "Usage: /workflow run <workflow_id> [dedup_key] [--input JSON_OBJECT]"
        )
        self.assertEqual(shell.client.workflow_runs, [])

    def test_workflow_unknown_subcommand_usage_error_is_local(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "error") as error:
            shell.command_registry.execute(shell.context, "/workflow nope")
        error.assert_called_once_with("Usage: /workflow [list|run|status|resume] ...")

    def test_workflow_without_subcommand_lists_workflows(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "table") as table:
            shell.command_registry.execute(shell.context, "/workflow")
        table.assert_called_once_with(
            ["Workflow ID", "Steps"],
            [["changelog", "scan -> worker"]],
        )

    def test_handle_repl_message_renders_remote_response(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "markdown") as markdown:
            shell.handle_repl_message(shell.context, "hello")
        # The legacy per-step preview is gone; only the final text from
        # request.completed renders.
        self.assertEqual(
            [call.args[0] for call in markdown.call_args_list],
            ["echo: hello"],
        )
        self.assertEqual(shell.context.session_ids["primary"], "s-1")

    def test_handle_repl_message_deduplicates_streamed_and_terminal_response(self) -> None:
        shell = self._build_shell()

        def stream_same_text(_agent_id: str, _request_id: str):
            yield {
                "event": "agent.trace",
                "data": {
                    "event_type": "runtime.llm.think.completed",
                    "trace_id": "trace-1",
                    "session_id": "s-1",
                    "loop_index": 0,
                    "created_at": 100.0,
                    "payload": {
                        "duration_ms": 12,
                        "action_type": "response",
                        "assistant_text": "echo: hello",
                        "tool_calls": [],
                        "token_usage": {"input": 2, "output": 1},
                    },
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
        # The LLM_THINK_COMPLETED event still feeds chain_renderer for trace
        # rendering, but its assistant_text is no longer previewed as a separate
        # markdown panel — only the final text from request.completed renders.
        self.assertEqual(
            [call.args[0] for call in markdown.call_args_list],
            ["final response"],
        )

    def test_handle_repl_message_single_render_when_tokens_stream(self) -> None:
        shell = self._build_shell()
        answer = "# Title\n\nBody paragraph one.\n\nDone."

        def stream_with_deltas(_agent_id: str, _request_id: str):
            for chunk in ["# Title\n\n", "Body paragraph one.\n\n", "Done."]:
                yield {
                    "event": "agent.trace",
                    "data": {
                        "event_type": "llm.response.delta",
                        "trace_id": "trace-1",
                        "loop_index": 0,
                        "payload": {"payload": {"text": chunk, "index": 0}},
                    },
                }
            yield {
                "event": "agent.trace",
                "data": {
                    "event_type": "runtime.llm.think.completed",
                    "trace_id": "trace-1",
                    "loop_index": 0,
                    "payload": {
                        "action_type": "finish",
                        "assistant_text": answer,
                        "tool_calls": [],
                        "token_usage": {"input": 2, "output": 1},
                        "duration_ms": 5,
                    },
                },
            }
            yield {
                "event": "request.completed",
                "data": {"session_id": "s-1", "response": {"text": answer}},
            }

        shell.client.stream_request = stream_with_deltas
        with patch.object(shell.context.renderer, "markdown") as markdown:
            shell.handle_repl_message(shell.context, "hello")
        # The answer rendered live (token streaming); the terminal/preview
        # markdown panel must not fire — otherwise it shows twice.
        markdown.assert_not_called()

    def test_split_complete_markdown_buffers_open_code_fence(self) -> None:
        renderer = ChainOfThoughtRenderer(Console(record=True, width=80))
        complete, remainder = renderer._split_complete_markdown(
            "para one\n\n```\ncode\n"
        )
        self.assertEqual(complete, "para one\n")
        # An unterminated code fence stays buffered until it closes.
        self.assertEqual(remainder, "```\ncode\n")

    def test_chain_renderer_runtime_events_preserve_durations(self) -> None:
        console = Console(record=True, width=120)
        renderer = ChainOfThoughtRenderer(console)

        renderer.on_runtime_event(
            RuntimeEvent(
                app_id="primary",
                agent_id="primary",
                event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
                trace_id="trace-1",
                session_id="s-1",
                loop_index=2,
                created_at=100.0,
                payload={
                    "action_type": "tool_call",
                    "assistant_text": "thinking",
                    "tool_calls": [{"name": "bash", "arguments": {"command": "pwd"}}],
                    "token_usage": {"input": 2, "output": 1},
                    "duration_ms": 123,
                },
            )
        )
        renderer.on_runtime_event(
            RuntimeEvent(
                app_id="primary",
                agent_id="primary",
                event_type=RuntimeEventType.TOOL_CALL_COMPLETED.value,
                trace_id="trace-1",
                session_id="s-1",
                loop_index=2,
                created_at=101.0,
                payload={
                    "tool_name": "bash",
                    "duration_ms": 45,
                },
            )
        )

        output = console.export_text()
        self.assertIn("123ms", output)
        self.assertIn("45ms", output)

    def test_handle_repl_message_streams_chain_events(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.chain_renderer, "on_runtime_event") as runtime_event:
            with patch.object(shell.chain_renderer, "finish_trace") as finish_trace:
                shell.handle_repl_message(shell.context, "hello")
        self.assertEqual(runtime_event.call_count, 2)
        self.assertEqual(
            [
                call.args[0].event_type
                for call in runtime_event.call_args_list
            ],
            [
                RuntimeEventType.LLM_THINK_COMPLETED.value,
                RuntimeEventType.STEP_COMPLETED.value,
            ],
        )
        finish_trace.assert_called_once()

    def test_handle_repl_message_renders_subagent_lifecycle(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.chain_renderer, "render_subagent_event") as render_sub:
            with patch.object(shell.chain_renderer, "finish_subagent") as finish_sub:
                shell.handle_repl_message(shell.context, "hello")
        self.assertEqual(render_sub.call_count, 1)
        self.assertEqual(render_sub.call_args[1]["agent_id"], "research")
        finish_sub.assert_called_once_with("research", 0)

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
        error.assert_called_once_with("    Subagent subagent error: request failed")

    def test_handle_repl_message_renders_assistant_blocks_in_order(self) -> None:
        shell = self._build_shell()

        def stream_with_blocks(_agent_id: str, _request_id: str):
            yield {
                "event": "request.completed",
                "data": {
                    "session_id": "s-1",
                    "response": {
                        "text": "text block",
                        "assistant_blocks": [
                            {"type": "thinking", "thinking": "my reasoning"},
                            {"type": "text", "text": "text block"},
                        ],
                    },
                },
            }

        shell.client.stream_request = stream_with_blocks
        thinking_calls: list[str] = []
        markdown_calls: list[str] = []
        with patch.object(shell.context.renderer, "thinking", side_effect=thinking_calls.append):
            with patch.object(shell.context.renderer, "markdown", side_effect=markdown_calls.append):
                shell.handle_repl_message(shell.context, "hello")
        self.assertEqual(thinking_calls, ["my reasoning"])
        self.assertEqual(markdown_calls, ["text block"])

    def test_handle_repl_message_skips_text_block_already_streamed(self) -> None:
        shell = self._build_shell()
        answer = "streamed answer"

        def stream_delta_then_blocks(_agent_id: str, _request_id: str):
            yield {
                "event": "agent.trace",
                "data": {
                    "event_type": "llm.response.delta",
                    "trace_id": "trace-1",
                    "payload": {"payload": {"text": answer, "index": 0}},
                },
            }
            yield {
                "event": "request.completed",
                "data": {
                    "session_id": "s-1",
                    "response": {
                        "text": answer,
                        "assistant_blocks": [
                            {"type": "thinking", "thinking": "deep thought"},
                            {"type": "text", "text": answer},
                        ],
                    },
                },
            }

        shell.client.stream_request = stream_delta_then_blocks
        thinking_calls: list[str] = []
        markdown_calls: list[str] = []
        with patch.object(shell.context.renderer, "thinking", side_effect=thinking_calls.append):
            with patch.object(shell.context.renderer, "markdown", side_effect=markdown_calls.append):
                shell.handle_repl_message(shell.context, "hello")
        # Thinking block renders even though text streamed live.
        self.assertEqual(thinking_calls, ["deep thought"])
        # Text block is skipped — it was already shown via delta streaming.
        self.assertEqual(markdown_calls, [])

    def test_workflow_run_renders_assistant_blocks_with_thinking(self) -> None:
        shell = self._build_shell()

        def stream_blocks_workflow(workflow_id: str, run_id: str):
            yield {
                "event": "workflow.task.started",
                "data": {"task_id": "scan", "task_agent_id": "worker"},
            }
            yield {
                "event": "request.completed",
                "data": {
                    "task_id": "scan",
                    "task_agent_id": "worker",
                    "response": {
                        "text": "result",
                        "assistant_blocks": [
                            {"type": "thinking", "thinking": "task reasoning"},
                            {"type": "text", "text": "result"},
                        ],
                    },
                },
            }
            yield {
                "event": "workflow.task.completed",
                "data": {"task_id": "scan", "task_agent_id": "worker"},
            }

        shell.client.stream_workflow_run = stream_blocks_workflow
        thinking_calls: list[str] = []
        markdown_calls: list[str] = []
        with patch.object(shell.context.renderer, "thinking", side_effect=thinking_calls.append):
            with patch.object(shell.context.renderer, "markdown", side_effect=markdown_calls.append):
                shell.command_registry.execute(shell.context, "/workflow run changelog manual")
        self.assertEqual(thinking_calls, ["task reasoning"])
        self.assertEqual(markdown_calls, ["result"])


class ChainOfThoughtRendererTests(unittest.TestCase):
    def test_think_events_use_step_id_for_display_when_step_complete_is_missing(self) -> None:
        console = Console(record=True, width=120)
        renderer = ChainOfThoughtRenderer(console)
        renderer.on_runtime_event(
            RuntimeEvent(
                app_id="primary",
                agent_id="primary",
                event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
                session_id="s-1",
                trace_id="trace-1",
                loop_index=0,
                payload={
                    "duration_ms": 10,
                    "action_type": "tool_call",
                    "tool_calls": [{"name": "bash", "arguments": {}}],
                    "token_usage": {"input": 10, "output": 1},
                },
            )
        )
        renderer.on_runtime_event(
            RuntimeEvent(
                app_id="primary",
                agent_id="primary",
                event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
                session_id="s-1",
                trace_id="trace-1",
                loop_index=1,
                payload={
                    "duration_ms": 12,
                    "action_type": "tool_call",
                    "tool_calls": [{"name": "bash", "arguments": {}}],
                    "token_usage": {"input": 12, "output": 1},
                },
            )
        )

        output = console.export_text()
        self.assertIn("Step 1:", output)
        self.assertIn("Step 2:", output)

    def test_summary_uses_think_duration_when_step_complete_is_missing(self) -> None:
        console = Console(record=True, width=120)
        renderer = ChainOfThoughtRenderer(console)
        renderer.on_runtime_event(
            RuntimeEvent(
                app_id="primary",
                agent_id="primary",
                event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
                session_id="s-1",
                trace_id="trace-1",
                loop_index=0,
                payload={
                    "duration_ms": 13447,
                    "action_type": "finish",
                    "tool_calls": [],
                    "token_usage": {"input": 1320, "output": 1406},
                },
            )
        )

        renderer.finish_trace()

        output = console.export_text()
        self.assertIn("Agent Execution Complete:", output)
        self.assertIn("2,726 tokens", output)
        self.assertIn("13,447ms", output)

    def test_render_runtime_trace_uses_runtime_trace_events(self) -> None:
        console = Console(record=True, width=120)
        renderer = ChainOfThoughtRenderer(console)
        trace = build_runtime_trace(
            [
                RuntimeEvent(
                    app_id="primary",
                    agent_id="primary",
                    event_type=RuntimeEventType.LLM_THINK_COMPLETED.value,
                    session_id="s-1",
                    trace_id="trace-1",
                    loop_index=0,
                    payload={
                        "duration_ms": 10,
                        "action_type": "tool_call",
                        "tool_calls": [{"name": "bash", "arguments": {"command": "pwd"}}],
                        "token_usage": {"input": 3, "output": 2},
                    },
                ),
                RuntimeEvent(
                    app_id="primary",
                    agent_id="primary",
                    event_type=RuntimeEventType.STEP_COMPLETED.value,
                    session_id="s-1",
                    trace_id="trace-1",
                    loop_index=0,
                    payload={"duration_ms": 12},
                ),
            ]
        )

        renderer.render_runtime_trace(trace)

        output = console.export_text()
        self.assertIn("Step 1:", output)
        self.assertIn("$ pwd", output)
        self.assertIn("Agent Execution Complete:", output)


class StructuredOutputRenderingTests(unittest.TestCase):
    """Tests for /workflow run rendering of structured_output."""

    def _build_shell(self) -> MashRemoteShell:
        return MashRemoteShell(
            _FakeClient(),
            ShellTarget(api_base_url="http://localhost:8000", agent_id="primary", session_id="s-1"),
        )

    def test_structured_output_renders_as_json_code_block_by_default(self) -> None:
        shell = self._build_shell()
        shell.client.workflow_structured_output = {"digest": "hello world", "score": 0.9}
        rendered: list[str] = []
        with patch.object(shell.context.renderer, "markdown", side_effect=rendered.append):
            shell.command_registry.execute(shell.context, "/workflow run changelog manual")
        # request.completed renders the structured_output JSON block; the
        # agent.trace preview is gone so there is only one render.
        self.assertEqual(len(rendered), 1)
        json_block = rendered[0]
        self.assertIn("```json", json_block)
        self.assertIn('"digest": "hello world"', json_block)
        self.assertIn('"score": 0.9', json_block)

    def test_text_not_re_rendered_when_structured_output_present(self) -> None:
        shell = self._build_shell()
        shell.client.workflow_structured_output = {"result": "ok"}
        rendered: list[str] = []
        with patch.object(shell.context.renderer, "markdown", side_effect=rendered.append):
            shell.command_registry.execute(shell.context, "/workflow run changelog manual")
        # Only the structured JSON block is rendered by request.completed;
        # the task text is not repeated.
        self.assertEqual(len(rendered), 1)
        self.assertIn("```json", rendered[0])
        self.assertNotIn('{"status":"ok"}', rendered[0])

    def test_text_rendered_when_no_structured_output(self) -> None:
        shell = self._build_shell()
        # No structured_output set — default fake client behavior.
        rendered: list[str] = []
        with patch.object(shell.context.renderer, "markdown", side_effect=rendered.append):
            shell.command_registry.execute(shell.context, "/workflow run changelog manual")
        # Only one render: streamed text via agent.trace. request.completed sees the
        # same text was already shown and does not repeat it.
        self.assertEqual(len(rendered), 1)
        self.assertEqual(rendered[0], '{"status":"ok"}')

    def test_registered_renderer_called_instead_of_default(self) -> None:
        shell = self._build_shell()
        shell.client.workflow_structured_output = {"summary": "digest ready"}
        calls: list[tuple] = []

        def my_renderer(task_id: str, agent_id: str, data: dict) -> None:
            calls.append((task_id, agent_id, data))

        shell.register_structured_output_renderer("changelog", my_renderer)
        rendered: list[str] = []
        with patch.object(shell.context.renderer, "markdown", side_effect=rendered.append):
            shell.command_registry.execute(shell.context, "/workflow run changelog manual")
        # Custom renderer received the right args.
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "scan")      # task_id
        self.assertEqual(calls[0][1], "worker")    # agent_id
        self.assertEqual(calls[0][2], {"summary": "digest ready"})
        # No default markdown render: no agent.trace preview, and the custom
        # renderer handled structured_output without calling ctx.renderer.markdown.
        self.assertEqual(len(rendered), 0)

    def test_registered_renderer_for_other_workflow_not_called(self) -> None:
        shell = self._build_shell()
        shell.client.workflow_structured_output = {"x": 1}
        calls: list[tuple] = []

        shell.register_structured_output_renderer("other-workflow", lambda *a: calls.append(a))
        rendered: list[str] = []
        with patch.object(shell.context.renderer, "markdown", side_effect=rendered.append):
            shell.command_registry.execute(shell.context, "/workflow run changelog manual")
        # Falls back to default JSON render because "changelog" has no registered renderer.
        self.assertEqual(calls, [])
        self.assertEqual(len(rendered), 1)
        self.assertIn("```json", rendered[0])

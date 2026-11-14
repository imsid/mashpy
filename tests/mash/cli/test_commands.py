"""The built-in slash commands, run against a fake host client."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
import unittest
from unittest.mock import patch

from mash.cli.repl import Repl, Target

if TYPE_CHECKING:
    from mash.cli.client import MashHostClient


class _FakeClient:
    def __init__(self) -> None:
        self.workflow_runs: list[dict[str, Any]] = []
        self.workflow_status_requests: list[dict[str, str]] = []
        self.host_workflows: list[str] = []
        self.feedback: list[dict[str, Any]] = []
        self.interactions: list[dict[str, Any]] = []
        self.submitted_user_ids: list[str] = []
        self.workflow_result: dict[str, Any] = {"status": "ok"}

    def health(self):
        return {
            "deployment": {
                "agents": [{"agent_id": "primary", "metadata": {"display_name": "Primary"}}],
                "hosts": [
                    {
                        "host_id": "assistant",
                        "agents": ["primary", "research"],
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
                "agents": ["primary", "research"],
                "workflows": [],
            }
        ]

    def get_host(self, host_id: str):
        return {
            "host_id": host_id,
            "agents": [
                {"agent_id": "primary", "metadata": {"display_name": "Primary"}},
                {"agent_id": "research", "metadata": {"display_name": "Research"}},
            ],
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
                "display_name": "changelog",
                "mode": "pipeline",
                "step_count": 1,
                "step_preview": [
                    {
                        "ordinal": 0,
                        "step_id": "scan",
                        "kind": "agent",
                        "agent_id": "worker",
                    }
                ],
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
        user_id: str | None = None,
    ):
        self.workflow_runs.append(
            {
                "workflow_id": workflow_id,
                "dedup_key": dedup_key,
                "workflow_input": workflow_input,
                "user_id": user_id,
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
            "result": {"digest": "ok"},
        }

    def submit_request(
        self, agent_id: str, *, message: str, session_id: str, user_id: str, mode: str = "fg"
    ):
        del agent_id, message, session_id, mode
        self.submitted_user_ids.append(user_id)
        return "req-1"

    def list_session_requests(self, agent_id: str, session_id: str, **filters: Any):
        del agent_id, session_id, filters
        return []

    def submit_feedback(self, agent_id: str, **kwargs: Any):
        self.feedback.append({"agent_id": agent_id, **kwargs})
        return {"feedback": {"feedback_id": len(self.feedback)}}

    def get_request_status(self, agent_id: str, request_id: str):
        return {"request_id": request_id, "agent_id": agent_id, "status": "running"}

    def stream_request(self, agent_id: str, request_id: str):
        if request_id == "sub-1":
            # The RunSubAgents child's own stream.
            yield {
                "event": "agent.trace",
                "data": {
                    "event_type": "chat.end",
                    "trace_id": "trace-sub-1",
                    "session_id": "s-1",
                    "loop_index": 0,
                    "created_at": 10.0,
                    "payload": {
                        "duration_ms": 7,
                        "action_type": "tool_call",
                        "assistant_text": "checking cli flow",
                        "tool_calls": [{"name": "bash", "arguments": {"command": "pwd"}}],
                        "attributes": {"gen_ai.usage.input_tokens": 1, "gen_ai.usage.output_tokens": 1},
                    },
                },
            }
            yield {"event": "request.completed", "data": {"status": "ok"}}
            return
        del agent_id
        yield {
            "event": "agent.trace",
            "data": {
                "event_type": "execute_tool.start",
                "trace_id": "trace-1",
                "session_id": "s-1",
                "loop_index": 0,
                "created_at": 99.0,
                "payload": {
                    "arguments": {"requests": [{"agent_id": "research", "prompt": "x"}]},
                    "attributes": {"gen_ai.tool.name": "RunSubAgents", "gen_ai.tool.call.id": "c1"},
                    "subagents": [{"agent_id": "research", "request_id": "sub-1"}],
                },
            },
        }
        yield {
            "event": "agent.trace",
            "data": {
                "event_type": "chat.end",
                "trace_id": "trace-1",
                "session_id": "s-1",
                "loop_index": 0,
                "created_at": 100.0,
                "payload": {
                    "duration_ms": 12,
                    "action_type": "response",
                    "assistant_text": "draft",
                    "tool_calls": [],
                    "attributes": {"gen_ai.usage.input_tokens": 2, "gen_ai.usage.output_tokens": 1},
                },
            },
        }
        yield {
            "event": "agent.trace",
            "data": {
                "event_type": "mash.step.end",
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
        yield {
            "event": "step.started",
            "data": {
                "workflow_id": workflow_id,
                "run_id": "mw:host:changelog:abc",
                "step_id": "scan",
                "attempt": 1,
                "seq": 1,
                "payload": {},
            },
        }
        yield {
            "event": "step.completed",
            "data": {
                "workflow_id": workflow_id,
                "run_id": "mw:host:changelog:abc",
                "step_id": "scan",
                "attempt": 1,
                "seq": 2,
                "payload": {},
            },
        }
        yield {
            "event": "workflow.completed",
            "data": {
                "workflow_id": workflow_id,
                "run_id": "mw:host:changelog:abc",
                "status": "completed",
                "result": self.workflow_result,
            },
        }


def _run(shell: Repl, line: str) -> None:
    """Run one ``/command`` line to its end."""
    asyncio.run(shell.command_registry.execute(shell.context, line))


def _fake(shell: Repl) -> _FakeClient:
    """Recover the typed fake behind the shell's MashHostClient attribute."""
    return cast(_FakeClient, shell.client)


class DefaultCommandTests(unittest.TestCase):
    def _build_shell(self) -> Repl:
        return Repl(
            cast("MashHostClient", _FakeClient()),
            Target(api_base_url="http://localhost:8000", agent_id="primary", session_id="s-1", user_id="u-1"),
        )

    def _build_host_shell(self, *, host_workflows: list[str] | None = None) -> Repl:
        client = _FakeClient()
        client.host_workflows = list(host_workflows or [])
        return Repl(
            cast("MashHostClient", client),
            Target(
                api_base_url="http://localhost:8000",
                agent_id="primary",
                session_id="s-1",
                host_id="assistant",
                user_id="u-1",
            ),
        )

    def test_boots_with_the_built_in_commands(self) -> None:
        shell = self._build_shell()
        names = {command.name for command in shell.command_registry.list_commands()}
        self.assertEqual(
            names,
            {
                "new", "resume", "compact", "usage", "copy",
                "bg", "subagent", "retry", "trace",
                "agents", "tools", "workflow", "memory", "browser",
                "help", "keys", "feedback", "clear", "exit",
            },
        )

    def test_feedback_command_records_session_context(self) -> None:
        shell = self._build_host_shell()
        shell.context.last_request_id = "req-1"
        with patch.object(shell.context.renderer, "info") as info:
            _run(shell, "/feedback the trace output is hard to read"
            )
        self.assertEqual(len(_fake(shell).feedback), 1)
        recorded = _fake(shell).feedback[0]
        self.assertEqual(recorded["agent_id"], "primary")
        self.assertEqual(recorded["message"], "the trace output is hard to read")
        self.assertEqual(recorded["host_id"], "assistant")
        self.assertEqual(recorded["session_id"], "s-1")
        self.assertEqual(recorded["request_id"], "req-1")
        self.assertIn("Feedback recorded", info.call_args.args[0])

    def test_feedback_command_requires_message(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "error") as error:
            _run(shell, "/feedback")
        self.assertEqual(_fake(shell).feedback, [])
        error.assert_called_once_with("Usage: /feedback <message>")

    def test_workflow_list_host_scoped_filters_attached(self) -> None:
        shell = self._build_host_shell(host_workflows=["changelog"])
        with patch.object(shell.context.renderer, "table") as table:
            _run(shell, "/workflow list")
        table.assert_called_once_with(
            ["Workflow ID", "Steps"],
            [["changelog", "scan (agent)"]],
        )

    def test_workflow_list_host_scoped_without_attached_workflows(self) -> None:
        shell = self._build_host_shell()
        with patch.object(shell.context.renderer, "warn") as warn:
            _run(shell, "/workflow list")
        warn.assert_called_once_with("No workflows attached to host 'assistant'.")

    def test_workflow_run_host_scoped_refuses_unattached(self) -> None:
        shell = self._build_host_shell()
        with patch.object(shell.context.renderer, "error") as error:
            _run(shell, "/workflow run changelog manual")
        self.assertEqual(_fake(shell).workflow_runs, [])
        self.assertIn("not attached to host 'assistant'", error.call_args.args[0])
        self.assertIn("'changelog'", error.call_args.args[0])

    def test_workflow_run_host_scoped_allows_attached(self) -> None:
        shell = self._build_host_shell(host_workflows=["changelog"])
        with patch.object(shell.context.renderer, "info") as info:
            _run(shell, "/workflow run changelog manual")
        self.assertEqual(
            _fake(shell).workflow_runs,
            [{"workflow_id": "changelog", "dedup_key": "manual", "workflow_input": None, "user_id": "u-1"}],
        )
        lines = [call.args[0] for call in info.call_args_list]
        self.assertIn("Workflow status: completed", lines)

    def test_workflow_status_host_scoped_refuses_unattached(self) -> None:
        shell = self._build_host_shell()
        with patch.object(shell.context.renderer, "error") as error:
            _run(shell, "/workflow status changelog mw:host:changelog:abc"
            )
        self.assertEqual(_fake(shell).workflow_status_requests, [])
        self.assertIn("not attached to host 'assistant'", error.call_args.args[0])

    def test_workflow_status_host_scoped_allows_attached(self) -> None:
        shell = self._build_host_shell(host_workflows=["changelog"])
        run_id = "mw:host:changelog:abc"
        with patch.object(shell.context.renderer, "table"):
            _run(shell, f"/workflow status changelog {run_id}")
        self.assertEqual(
            _fake(shell).workflow_status_requests,
            [{"workflow_id": "changelog", "run_id": run_id}],
        )

    def test_workflow_command_lists_workflows(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "table") as table:
            _run(shell, "/workflow list")
        table.assert_called_once_with(
            ["Workflow ID", "Steps"],
            [["changelog", "scan (agent)"]],
        )

    def test_workflows_alias_is_not_registered(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "warn") as warn:
            _run(shell, "/workflows")
        warn.assert_called_once_with("Unknown command: /workflows. Try /help.")

    def test_workflow_run_starts_workflow(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "info") as info:
            _run(shell, "/workflow run changelog manual")
        self.assertEqual(
            _fake(shell).workflow_runs,
            [{"workflow_id": "changelog", "dedup_key": "manual", "workflow_input": None, "user_id": "u-1"}],
        )
        lines = [call.args[0] for call in info.call_args_list]
        self.assertIn("Workflow: changelog", lines)
        self.assertIn("Run ID: mw:host:changelog:abc", lines)
        self.assertIn("Workflow step scan started", lines)
        self.assertIn("Workflow step scan completed", lines)
        self.assertIn("Workflow status: completed", lines)

    def test_workflow_run_renders_its_result(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "markdown") as markdown:
            _run(shell, "/workflow run changelog")
        markdown.assert_called_once()
        self.assertIn('"status": "ok"', markdown.call_args.args[0])

    def test_workflow_run_forwards_input_json(self) -> None:
        shell = self._build_shell()
        _run(shell, "/workflow run changelog manual --input '{\"x\":1}'",
        )
        self.assertEqual(
            _fake(shell).workflow_runs,
            [
                {
                    "workflow_id": "changelog",
                    "dedup_key": "manual",
                    "workflow_input": {"x": 1},
                    "user_id": "u-1",
                }
            ],
        )

    def test_workflow_run_rejects_invalid_input_json_locally(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "error") as error:
            _run(shell, "/workflow run changelog --input '{bad}'")
        self.assertEqual(_fake(shell).workflow_runs, [])
        self.assertIn("Workflow input must be valid JSON", error.call_args.args[0])

    def test_workflow_run_rejects_non_object_input_json_locally(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "error") as error:
            _run(shell, "/workflow run changelog --input '[1]'")
        self.assertEqual(_fake(shell).workflow_runs, [])
        error.assert_called_once_with("Workflow input must be a JSON object")

    def test_workflow_status_fetches_run(self) -> None:
        shell = self._build_shell()
        run_id = "mw:host:changelog:abc"
        with patch.object(shell.context.renderer, "table") as table, patch.object(
            shell.context.renderer,
            "print",
        ) as print_:
            _run(shell, f"/workflow status changelog {run_id}")
        self.assertEqual(
            _fake(shell).workflow_status_requests,
            [{"workflow_id": "changelog", "run_id": run_id}],
        )
        rows = table.call_args.args[1]
        self.assertIn(["status", "completed"], rows)
        self.assertIn('"digest"', print_.call_args.args[0])

    def test_workflow_run_usage_error_is_local(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "error") as error:
            _run(shell, "/workflow run")
        error.assert_called_once_with(
            "Usage: /workflow run <workflow_id> [dedup_key] [--input JSON_OBJECT]"
        )
        self.assertEqual(_fake(shell).workflow_runs, [])

    def test_workflow_unknown_subcommand_usage_error_is_local(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "error") as error:
            _run(shell, "/workflow nope")
        error.assert_called_once_with("Usage: /workflow [list|run|status|resume] ...")

    def test_workflow_without_subcommand_lists_workflows(self) -> None:
        shell = self._build_shell()
        with patch.object(shell.context.renderer, "table") as table:
            _run(shell, "/workflow")
        table.assert_called_once_with(
            ["Workflow ID", "Steps"],
            [["changelog", "scan (agent)"]],
        )



class WorkflowResultRenderingTests(unittest.TestCase):
    """Tests for completed workflow result rendering."""

    def _build_shell(self) -> Repl:
        return Repl(
            cast("MashHostClient", _FakeClient()),
            Target(api_base_url="http://localhost:8000", agent_id="primary", session_id="s-1", user_id="u-1"),
        )

    def test_result_renders_as_json_code_block_by_default(self) -> None:
        shell = self._build_shell()
        _fake(shell).workflow_result = {"digest": "hello world", "score": 0.9}
        rendered: list[str] = []
        with patch.object(shell.context.renderer, "markdown", side_effect=rendered.append):
            _run(shell, "/workflow run changelog manual")
        self.assertEqual(len(rendered), 1)
        json_block = rendered[0]
        self.assertIn("```json", json_block)
        self.assertIn('"digest": "hello world"', json_block)
        self.assertIn('"score": 0.9', json_block)

    def test_registered_renderer_called_instead_of_default(self) -> None:
        shell = self._build_shell()
        _fake(shell).workflow_result = {"summary": "digest ready"}
        calls: list[dict[str, Any]] = []

        def my_renderer(data: dict[str, Any]) -> None:
            calls.append(data)

        shell.register_workflow_result_renderer("changelog", my_renderer)
        rendered: list[str] = []
        with patch.object(shell.context.renderer, "markdown", side_effect=rendered.append):
            _run(shell, "/workflow run changelog manual")
        self.assertEqual(calls, [{"summary": "digest ready"}])
        self.assertEqual(len(rendered), 0)

    def test_registered_renderer_for_other_workflow_not_called(self) -> None:
        shell = self._build_shell()
        _fake(shell).workflow_result = {"x": 1}
        calls: list[tuple] = []

        shell.register_workflow_result_renderer(
            "other-workflow", lambda data: calls.append((data,))
        )
        rendered: list[str] = []
        with patch.object(shell.context.renderer, "markdown", side_effect=rendered.append):
            _run(shell, "/workflow run changelog manual")
        self.assertEqual(calls, [])
        self.assertEqual(len(rendered), 1)
        self.assertIn("```json", rendered[0])

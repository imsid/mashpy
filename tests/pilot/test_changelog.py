from __future__ import annotations

# ruff: noqa: E402

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mash.cli.shell import MashRemoteShell, ShellTarget
from pilot.changelog import (
    CHANGELOG_SKILL_NAME,
    CHANGELOG_SKILL_PATH,
    CHANGELOG_STRUCTURED_OUTPUT,
    CHANGELOG_TASK_ID,
    CHANGELOG_WORKFLOW_ID,
    DEFAULT_CHANGELOG_COMMIT_COUNT,
    changelog_skill_payload,
    register_changelog_command,
)
from pilot import cli as pilot_cli


class _FakeClient:
    def __init__(self) -> None:
        self.registered_skills: list[dict[str, Any]] = []
        self.registered_workflows: list[dict[str, Any]] = []
        self.workflow_runs: list[dict[str, Any]] = []

    def health(self) -> dict[str, Any]:
        return {"deployment": {"agents": [{"agent_id": "pilot", "metadata": None}], "hosts": []}}

    def list_agents(self) -> list[dict[str, str]]:
        return [{"agent_id": "pilot", "metadata": {"display_name": "Pilot"}}]

    def register_agent_skill(
        self,
        agent_id: str,
        skill_payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.registered_skills.append(
            {"agent_id": agent_id, "skill_payload": dict(skill_payload)}
        )
        return {"agent_id": agent_id, "skill_name": skill_payload.get("name")}

    def register_agent_workflow(
        self,
        agent_id: str,
        workflow_payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.registered_workflows.append(
            {"agent_id": agent_id, "workflow_payload": dict(workflow_payload)}
        )
        return {"agent_id": agent_id, "workflow_id": workflow_payload.get("workflow_id")}

    def run_workflow(
        self,
        workflow_id: str,
        *,
        dedup_key: str | None = None,
        workflow_input: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        self.workflow_runs.append(
            {
                "workflow_id": workflow_id,
                "dedup_key": dedup_key,
                "workflow_input": workflow_input,
            }
        )
        return {
            "workflow_id": workflow_id,
            "run_id": "mw:host:pilot-changelog:abc",
            "status": "queued",
        }

    def stream_workflow_run(self, workflow_id: str, run_id: str):
        del workflow_id, run_id
        response_text = (
            "{\"markdown\":\"## Changelog\\n- Added dynamic workflows\","
            "\"commits_scanned\":5}"
        )
        structured_output = {
            "markdown": "## Changelog\n- Added dynamic workflows",
            "commits_scanned": 5,
        }
        yield {
            "event": "workflow.task.started",
            "data": {
                "task_id": CHANGELOG_TASK_ID,
                "task_agent_id": "pilot",
            },
        }
        yield {
            "event": "agent.trace",
            "data": {
                "task_id": CHANGELOG_TASK_ID,
                "task_agent_id": "pilot",
                "event_type": "runtime.llm.think.completed",
                "trace_id": "trace-wf-1",
                "session_id": "workflow:pilot-changelog",
                "loop_index": 0,
                "created_at": 100.0,
                "payload": {
                    "action_type": "response",
                    "assistant_text": response_text,
                    "tool_calls": [],
                    "duration_ms": 9,
                    "token_usage": {"input": 1, "output": 1},
                },
            },
        }
        yield {
            "event": "request.completed",
            "data": {
                "task_id": CHANGELOG_TASK_ID,
                "task_agent_id": "pilot",
                "response": {
                    "text": response_text,
                    "structured_output": structured_output,
                },
            },
        }


def _build_shell() -> MashRemoteShell:
    shell = MashRemoteShell(
        _FakeClient(),
        ShellTarget(
            api_base_url="http://localhost:8000",
            agent_id="pilot",
            session_id="s-1",
        ),
    )
    register_changelog_command(shell)
    return shell


def test_register_changelog_command_adds_pilot_only_command() -> None:
    shell = _build_shell()

    command_names = [command.name for command in shell.command_registry.list_commands()]

    assert "changelog" in command_names


def test_changelog_skill_payload_loads_markdown_skill_file() -> None:
    payload = changelog_skill_payload()

    assert CHANGELOG_SKILL_PATH.name == "changelog.md"
    assert payload["content"] == CHANGELOG_SKILL_PATH.read_text(encoding="utf-8")
    assert "Pilot Changelog Workflow" in str(payload["content"])
    assert "```json" not in str(payload["content"])
    assert "commits_scanned" in str(payload["content"])
    assert "Markdown changelog content" in str(payload["content"])


def test_changelog_command_registers_and_runs_default_count() -> None:
    shell = _build_shell()

    with patch.object(shell.context.renderer, "markdown") as markdown:
        shell.command_registry.execute(shell.context, "/changelog")

    assert shell.client.registered_skills[0]["agent_id"] == "pilot"
    assert shell.client.registered_skills[0]["skill_payload"]["name"] == CHANGELOG_SKILL_NAME
    workflow_payload = shell.client.registered_workflows[0]["workflow_payload"]
    assert workflow_payload["workflow_id"] == CHANGELOG_WORKFLOW_ID
    assert workflow_payload["tasks"] == [
        {
            "task_id": CHANGELOG_TASK_ID,
            "agent_id": "pilot",
            "structured_output": CHANGELOG_STRUCTURED_OUTPUT,
        }
    ]
    assert shell.client.workflow_runs[-1] == {
        "workflow_id": CHANGELOG_WORKFLOW_ID,
        "dedup_key": None,
        "workflow_input": {"commit_count": DEFAULT_CHANGELOG_COMMIT_COUNT},
    }
    markdown.assert_called_once_with("## Changelog\n- Added dynamic workflows")


def test_changelog_command_accepts_commit_count() -> None:
    shell = _build_shell()

    shell.command_registry.execute(shell.context, "/changelog 12")

    assert shell.client.workflow_runs[-1]["workflow_input"] == {"commit_count": 12}


def test_changelog_command_rejects_invalid_counts() -> None:
    shell = _build_shell()

    with patch.object(shell.context.renderer, "error") as error:
        shell.command_registry.execute(shell.context, "/changelog 0")
    error.assert_called_once_with("Changelog commit count must be positive")
    assert shell.client.workflow_runs == []

    with patch.object(shell.context.renderer, "error") as error:
        shell.command_registry.execute(shell.context, "/changelog nope")
    error.assert_called_once_with("Usage: /changelog [N]")
    assert shell.client.workflow_runs == []


def test_pilot_cli_repl_registers_changelog_command() -> None:
    registered_shells = []

    class FakeRemoteShell:
        def __init__(self, client: Any, target: Any) -> None:
            self.client = client
            self.target = target
            self.commands: list[str] = []
            registered_shells.append(self)

        @staticmethod
        def new_session_id() -> str:
            return "s-1"

        def register_command(self, command: Any) -> None:
            self.commands.append(command.name)

        def run(self) -> None:
            return None

    class FakeHostClient:
        def __init__(self, base_url: str, *, api_key: str | None = None) -> None:
            self.base_url = base_url
            self.api_key = api_key

        def health(self) -> dict[str, Any]:
            return {"deployment": {"agents": [{"agent_id": "pilot", "metadata": None}], "hosts": []}}

        def close(self) -> None:
            return None

    with patch.object(pilot_cli, "MashHostClient", FakeHostClient), patch.object(
        pilot_cli,
        "MashRemoteShell",
        FakeRemoteShell,
    ):
        result = pilot_cli.main(
            ["--api-base-url", "http://localhost:8000", "--api-key", "secret", "repl"]
        )

    assert result == 0
    assert registered_shells[0].target.agent_id == "pilot"
    assert "changelog" in registered_shells[0].commands

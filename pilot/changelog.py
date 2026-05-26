"""Dynamic Pilot changelog workflow definitions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mash.cli.commands import Command

CHANGELOG_WORKFLOW_ID = "pilot-changelog"
CHANGELOG_TASK_ID = "scan-recent-commits"
CHANGELOG_SKILL_NAME = "workflow:pilot-changelog:v1"
DEFAULT_CHANGELOG_COMMIT_COUNT = 5
CHANGELOG_SKILL_PATH = Path(__file__).resolve().parent / "skills" / "changelog.md"
CHANGELOG_STRUCTURED_OUTPUT = {
    "title": "PilotChangelogWorkflowOutput",
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "commits_scanned": {"type": "integer"},
        "items": {
            "type": "array",
            "items": {"type": "string"},
        },
        "markdown": {"type": "string"},
    },
    "required": ["title", "commits_scanned", "items", "markdown"],
    "additionalProperties": False,
}


def changelog_skill_content() -> str:
    return CHANGELOG_SKILL_PATH.read_text(encoding="utf-8")


def changelog_skill_payload() -> dict[str, object]:
    return {
        "type": "dynamic",
        "name": CHANGELOG_SKILL_NAME,
        "description": "Generate a changelog from recent git commits.",
        "content": changelog_skill_content(),
    }


def changelog_workflow_payload(primary_agent_id: str) -> dict[str, object]:
    return {
        "workflow_id": CHANGELOG_WORKFLOW_ID,
        "tasks": [
            {
                "task_id": CHANGELOG_TASK_ID,
                "agent_id": primary_agent_id,
                "structured_output": CHANGELOG_STRUCTURED_OUTPUT,
            }
        ],
        "metadata": {"source": "pilot", "kind": "changelog"},
        "task_message": {
            "skill_name": CHANGELOG_SKILL_NAME,
            "instruction": (
                "Your first action must be calling the Skill tool with "
                f"name '{CHANGELOG_SKILL_NAME}'. Then summarize the recent commits "
                "requested by workflow_input.commit_count."
            ),
        },
    }


def _workflow_markdown(response_payload: dict[str, Any]) -> str:
    structured_output = response_payload.get("structured_output")
    if isinstance(structured_output, dict):
        markdown = structured_output.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            return markdown
    return str(response_payload.get("text") or "")


def register_changelog_command(shell: Any) -> None:
    """Register Pilot's dynamic changelog workflow command on a Mash shell."""

    def changelog_command(ctx: Any, args: list[str]) -> None:
        if len(args) > 1:
            ctx.renderer.error("Usage: /changelog [N]")
            return
        commit_count = DEFAULT_CHANGELOG_COMMIT_COUNT
        if args:
            try:
                commit_count = int(args[0])
            except ValueError:
                ctx.renderer.error("Usage: /changelog [N]")
                return
        if commit_count <= 0:
            ctx.renderer.error("Changelog commit count must be positive")
            return

        health = ctx.client.health()
        deployment = health.get("deployment") if isinstance(health, dict) else {}
        if not isinstance(deployment, dict):
            deployment = {}
        primary_agent_id = str(deployment.get("primary_agent_id") or "").strip()
        if not primary_agent_id:
            ctx.renderer.error("Primary agent is not available")
            return

        ctx.client.register_agent_skill(
            primary_agent_id,
            changelog_skill_payload(),
        )
        ctx.client.register_agent_workflow(
            primary_agent_id,
            changelog_workflow_payload(primary_agent_id),
        )

        run = ctx.client.run_workflow(
            CHANGELOG_WORKFLOW_ID,
            workflow_input={"commit_count": commit_count},
        )
        ctx.renderer.info(f"Workflow: {run.get('workflow_id') or CHANGELOG_WORKFLOW_ID}")
        run_id = str(run.get("run_id") or "")
        ctx.renderer.info(f"Run ID: {run_id}")
        if not run_id:
            ctx.renderer.info(f"Status: {run.get('status') or ''}")
            return

        final_payload: dict[str, Any] | None = None
        try:
            for event in ctx.client.stream_workflow_run(CHANGELOG_WORKFLOW_ID, run_id):
                event_name = str(event.get("event") or "")
                payload = event.get("data")
                if not isinstance(payload, dict):
                    continue

                task_agent_id = str(payload.get("task_agent_id") or "")

                if event_name == "agent.trace":
                    shell.render_runtime_trace_payload(
                        payload,
                        trace_label="Workflow task",
                        agent_id=task_agent_id or None,
                    )
                    continue

                if event_name == "request.completed":
                    final_payload = payload
                    break

                if event_name == "request.error":
                    error = payload.get("error")
                    raise RuntimeError(str(error or "workflow task request failed"))

                if event_name == "workflow.error":
                    error = payload.get("error")
                    raise RuntimeError(str(error or "workflow stream failed"))
        finally:
            shell.chain_renderer.finish_trace()

        if final_payload is None:
            raise RuntimeError("workflow stream ended without a terminal event")

        response_payload = final_payload.get("response")
        if isinstance(response_payload, dict):
            text = _workflow_markdown(response_payload)
        else:
            text = str(final_payload.get("text") or "")
        if text:
            ctx.renderer.markdown(text)

    shell.register_command(
        Command(
            name="changelog",
            help="Generate a changelog from recent commits",
            handler=changelog_command,
        )
    )

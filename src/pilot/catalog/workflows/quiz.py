"""Pilot quiz workflow agent and REPL command."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from mash.cli.commands import Command
from mash.core.config import AgentConfig
from mash.core.llm import LLMProvider
from mash.runtime import AgentMetadata
from mash.runtime.spec import AgentSpec
from mash.skills.base import Skill
from mash.skills.registry import SkillRegistry
from mash.tools.ask_user import AskUserTool
from mash.tools.registry import ToolRegistry
from mash.workflows import TaskSpec, WorkflowSpec

from ...prompt import build_repo_context
from .._base import PILOT_SKILLS_DIR, build_bash_tool, build_default_llm

QUIZ_AGENT_ID = "quiz-me"
QUIZ_WORKFLOW_ID = "pilot-quiz"
QUIZ_TASK_ID = "run-quiz"
QUIZ_SKILL_NAME = "mash-quiz"
QUIZ_SKILLS_DIR = PILOT_SKILLS_DIR

QUIZ_DOC_ROOTS = (
    "src/mash/core",
    "src/mash/tools",
    "src/mash/skills",
    "src/mash/runtime",
    "src/mash/workflows",
    "src/mash/mcp",
    "src/mash/cli",
    "src/mash/api",
    "src/mash/agents/masher",
)
QUIZ_EXTRA_DOC_PATHS = (
    "README.md",
    "HOW_TO_DEPLOY.md",
    "src/mash/AGENTS.md",
    "docs/rfcs/host-to-agent-protocol.md",
)

_PROMPT = """You are a Mash quiz workflow worker.

You are invoked only by the pilot-quiz workflow. Do not answer free-form chat.

Every request is JSON with workflow_id, workflow_run_id, task_id, workflow_input,
and task_state.

Workflow skill routing:
- workflow_id=pilot-quiz, task_id=run-quiz -> skill=mash-quiz

Routing rules:
- Match both workflow_id and task_id exactly.
- Call the standard Skill tool exactly once with the matched skill name before doing workflow work.
- After the skill loads, follow only the loaded skill's workflow instructions.
- If no route matches, return an error object and do not call workflow tools.

Use the cached docs below as your primary knowledge source for generating
quiz questions. They cover every major Mash module. Use Bash only for narrow
verification or to look up specific implementation details.
"""


def _quiz_cached_doc_paths(
    workspace_root: Path,
    *,
    doc_roots: Sequence[str] = (),
    extra_doc_paths: Sequence[str] = (),
) -> list[str]:
    doc_paths: list[str] = []
    seen: set[str] = set()
    for root in doc_roots:
        root_path = (workspace_root / root).resolve()
        for filename in ("README.md", "AGENTS.md"):
            candidate = root_path / filename
            if candidate.is_file():
                resolved = str(candidate)
                if resolved not in seen:
                    seen.add(resolved)
                    doc_paths.append(resolved)
    for relpath in extra_doc_paths:
        candidate = (workspace_root / relpath).resolve()
        if candidate.is_file():
            resolved = str(candidate)
            if resolved not in seen:
                seen.add(resolved)
                doc_paths.append(resolved)
    return doc_paths


class QuizAgentSpec(AgentSpec):
    """Workflow agent for interactive Mash quizzes."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def get_agent_id(self) -> str:
        return QUIZ_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(AskUserTool())
        tools.register(build_bash_tool(self.workspace_root))
        return tools

    def build_skills(self) -> SkillRegistry:
        skills = SkillRegistry()
        skill_dir = QUIZ_SKILLS_DIR / "mash-quiz"
        skills.register(
            Skill(
                type="custom",
                name=QUIZ_SKILL_NAME,
                description="Interactive quiz about Mash SDK internals for learning.",
                location=str(skill_dir),
            )
        )
        return skills

    def build_llm(self) -> LLMProvider:
        return build_default_llm(QUIZ_AGENT_ID)

    def build_system_prompt(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": _PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        repo_context = build_repo_context(
            repo=str(self.workspace_root),
            cached_files=_quiz_cached_doc_paths(
                self.workspace_root,
                doc_roots=QUIZ_DOC_ROOTS,
                extra_doc_paths=QUIZ_EXTRA_DOC_PATHS,
            ),
        )
        if repo_context:
            blocks.append(
                {
                    "type": "text",
                    "text": repo_context,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        return blocks

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=QUIZ_AGENT_ID,
            system_prompt=self.build_system_prompt(),
            skills_enabled=True,
            max_steps=30,
        )

    def enable_runtime_tools(self) -> bool:
        return False


def create_spec(*, workspace_root: str) -> QuizAgentSpec:
    return QuizAgentSpec(Path(workspace_root).resolve())


def build_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Quiz Me",
        description=(
            "Interactive quiz about Mash internals: three questions of "
            "increasing difficulty, follow-ups welcome. Runs the "
            f"`{QUIZ_WORKFLOW_ID}` workflow task."
        ),
        capabilities=[
            "interactive mash quiz",
            f"workflow `{QUIZ_WORKFLOW_ID}`",
        ],
        usage_guidance=(
            f"Only useful through the `{QUIZ_WORKFLOW_ID}` workflow (the "
            "/quiz command); it refuses free-form chat. Attach the workflow "
            "to a host to enable it there. Not a delegation target."
        ),
    )


def build_quiz_workflow_spec() -> WorkflowSpec:
    """The pilot-quiz definition: one task executed by the pooled quiz agent."""
    return WorkflowSpec(
        workflow_id=QUIZ_WORKFLOW_ID,
        tasks=[
            TaskSpec(
                task_id=QUIZ_TASK_ID,
                agent_id=QUIZ_AGENT_ID,
            )
        ],
        metadata={"source": "pilot", "kind": "quiz"},
    )


def register_quiz_command(shell: Any) -> None:
    """Register Pilot's quiz workflow command on a Mash shell."""

    def quiz_command(ctx: Any, args: list[str]) -> None:
        if args:
            ctx.renderer.error("Usage: /quiz")
            return

        run = ctx.client.run_workflow(QUIZ_WORKFLOW_ID, session_id=ctx.session_id)
        ctx.renderer.info(f"Workflow: {run.get('workflow_id') or QUIZ_WORKFLOW_ID}")
        run_id = str(run.get("run_id") or "")
        ctx.renderer.info(f"Run ID: {run_id}")
        if not run_id:
            ctx.renderer.info(f"Status: {run.get('status') or ''}")
            return

        final_payload: dict[str, Any] | None = None
        pending_interaction: dict[str, Any] | None = None
        try:
            for event in ctx.client.stream_workflow_run(QUIZ_WORKFLOW_ID, run_id):
                event_name = str(event.get("event") or "")
                payload = event.get("data")
                if not isinstance(payload, dict):
                    continue

                task_agent_id = str(payload.get("task_agent_id") or "")

                if event_name == "agent.trace":
                    shell.render_runtime_trace_payload(
                        payload,
                        trace_label="Quiz",
                        agent_id=task_agent_id or None,
                    )
                    continue

                if event_name == "request.interaction.create":
                    shell.chain_renderer.finish_trace()
                    pending_interaction = payload
                    _handle_quiz_interaction(ctx, payload)
                    pending_interaction = None
                    continue

                if event_name == "request.interaction.ack":
                    _render_interaction_ack(ctx, payload)
                    continue

                if event_name == "request.completed":
                    final_payload = payload
                    break

                if event_name == "request.error":
                    error = payload.get("error")
                    raise RuntimeError(str(error or "quiz workflow request failed"))

                if event_name == "workflow.error":
                    error = payload.get("error")
                    raise RuntimeError(str(error or "quiz workflow failed"))
        except KeyboardInterrupt:
            # Swallow Ctrl-C so the REPL survives, and try to end the run
            # gracefully instead of leaving a PENDING durable workflow that
            # DBOS would recover on every host restart.
            shell.chain_renderer.finish_trace()
            _abort_quiz(ctx, pending_interaction, run_id)
        finally:
            shell.chain_renderer.finish_trace()

        # Mash >= 0.12 unifies final-turn rendering: render thinking + text
        # from assistant_blocks, de-duping any text already streamed live.
        # Skipped on abort, where no request.completed event arrives.
        if final_payload is not None:
            shell.render_final_response(
                ctx,
                final_payload.get("response"),
                str(final_payload.get("text") or ""),
                shell.chain_renderer.take_streamed_text(),
            )

    shell.register_command(
        Command(
            name="quiz",
            help="Interactive quiz about Mash internals",
            handler=quiz_command,
        )
    )


QUIZ_ABORT_RESPONSE = (
    "The user exited the quiz (Ctrl-C). Stop immediately: do not ask any more "
    "questions or call AskUser again; end the workflow now with a one-line "
    "goodbye."
)


def _post_abort_response(ctx: Any, interaction: dict[str, Any]) -> None:
    agent_id = str(
        interaction.get("agent_id") or interaction.get("task_agent_id") or ""
    )
    ctx.client.post_interaction(
        agent_id,
        str(interaction.get("request_id") or ""),
        interaction_id=str(interaction.get("interaction_id") or ""),
        response=QUIZ_ABORT_RESPONSE,
    )


def _abort_quiz(
    ctx: Any, pending_interaction: dict[str, Any] | None, run_id: str
) -> None:
    """Wind down an interrupted quiz run.

    Mash has no workflow-cancel API, but the quiz blocks on durable AskUser
    interactions. If one is pending, answer it with a stop instruction so the
    quiz agent completes the run instead of waiting until the interaction
    times out. Keep draining the run's stream and answer any further
    questions the same way, so the run can't be left PENDING.
    """
    if pending_interaction is None:
        ctx.renderer.warn(
            f"\nQuiz interrupted. Run {run_id} is still executing server-side; "
            "it will stop at its next question's timeout."
        )
        return

    try:
        _post_abort_response(ctx, pending_interaction)
        ctx.renderer.info("\nQuiz interrupted — asking the quiz agent to stop...")
        # Drain until the run terminates, re-sending the stop instruction if
        # the agent asks anything else. Capped so a misbehaving agent can't
        # hold the shell hostage.
        aborts_left = 3
        for event in ctx.client.stream_workflow_run(QUIZ_WORKFLOW_ID, run_id):
            event_name = str(event.get("event") or "")
            if event_name == "request.interaction.create":
                if aborts_left <= 0:
                    ctx.renderer.warn(
                        f"Quiz agent kept asking questions; run {run_id} will "
                        "stop at its next question's timeout."
                    )
                    return
                aborts_left -= 1
                payload = event.get("data")
                if isinstance(payload, dict):
                    _post_abort_response(ctx, payload)
                continue
            if event_name in ("request.completed", "request.error", "workflow.error"):
                ctx.renderer.info("Quiz stopped.")
                return
    except KeyboardInterrupt:
        ctx.renderer.warn(
            f"\nQuiz abort abandoned. Run {run_id} is still executing "
            "server-side; it will stop at its next question's timeout."
        )
    except Exception as exc:
        ctx.renderer.warn(
            f"\nQuiz interrupted, but could not notify the quiz agent ({exc}). "
            f"Run {run_id} will stop at its next question's timeout."
        )


def _handle_quiz_interaction(ctx: Any, payload: dict[str, Any]) -> None:
    """Present an AskUser interaction to the user and post the response."""
    interaction_id = str(payload.get("interaction_id") or "")
    interaction_type = str(payload.get("type") or "info")
    prompt = str(payload.get("prompt") or "Input required:")
    schema = payload.get("schema")
    agent_id = str(payload.get("agent_id") or payload.get("task_agent_id") or "")
    request_id = str(payload.get("request_id") or "")

    ctx.renderer.info(f"\n{prompt}")

    if interaction_type == "choice":
        options: list[str] = []
        if isinstance(schema, dict):
            options = schema.get("options", [])
        for i, opt in enumerate(options, 1):
            ctx.renderer.info(f"  {i}. {opt}")
        ctx.renderer.info("  Enter numbers separated by commas:")
        user_input = input("  > ").strip()
        selected: list[str] = []
        for part in user_input.split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(options):
                    selected.append(options[idx])
                elif part in options:
                    selected.append(part)
        response: Any = selected
    else:
        response = input("  > ").strip()

    ctx.client.post_interaction(
        agent_id,
        request_id,
        interaction_id=interaction_id,
        response=response,
    )


def _render_interaction_ack(ctx: Any, payload: dict[str, Any]) -> None:
    """Render an interaction acknowledgement."""
    timed_out = payload.get("timed_out", False)
    if timed_out:
        interaction_id = str(payload.get("interaction_id") or "")
        ctx.renderer.warn(f"  Interaction {interaction_id} timed out")

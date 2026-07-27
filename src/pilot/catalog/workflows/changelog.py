"""Pilot changelog workflow: read CHANGELOG.md, then summarize the most recent
release with a Gemma-backed agent that scans the code when an entry is unclear.

Two steps:
- ``read-changelog`` (CodeStep): deterministic parse of ``CHANGELOG.md`` — slice
  the most recent release section(s) out of the file, no LLM involved.
- ``summarize`` (AgentStep): one run of the ``changelog-summarizer`` agent over
  the sliced excerpt. It writes a plain-language summary and calls out the
  changes that matter, reaching for bash (``rg``/``sed``) only when an entry is
  too terse to explain without looking at the code. Runs on the open Gemma model
  over OpenRouter (``build_default_llm``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from mash import AgentMetadata, AgentSpec, AgentStep, CodeStep, StepContext, WorkflowSpec
from mash.cli.commands import Command
from mash.core.config import AgentConfig
from mash.core.llm import LLMProvider
from mash.skills.registry import SkillRegistry
from mash.tools.registry import ToolRegistry

from .._base import build_bash_tool, build_default_llm

CHANGELOG_AGENT_ID = "changelog-summarizer"
CHANGELOG_WORKFLOW_ID = "pilot-changelog"
READ_STEP_ID = "read-changelog"
SUMMARIZE_STEP_ID = "summarize"

CHANGELOG_FILENAME = "CHANGELOG.md"

# A release section header, e.g.
#   ## [0.19.0](https://.../compare/...) (2026-07-26)
_VERSION_HEADER = re.compile(
    r"^##\s+\[(?P<version>[^\]]+)\][^\n]*?(?:\((?P<date>[^)]+)\)\s*)?$"
)


class ChangelogIn(BaseModel):
    """Workflow input: how many of the most recent release sections to read."""

    versions: int = Field(default=1, ge=1, le=10)


class ChangelogExtract(BaseModel):
    """The sliced changelog: raw markdown of the most recent release(s)."""

    latest_version: str
    released_on: str = ""
    versions_included: int = 0
    excerpt: str


class ChangelogSummary(BaseModel):
    """The workflow result: a plain-language read of the latest changes."""

    latest_version: str
    summary: str
    highlights: list[str]


def _slice_recent_releases(text: str, versions: int) -> ChangelogExtract:
    """Slice the first ``versions`` release sections out of a changelog.

    Deterministic and dependency-free: find the ``## [x.y.z]`` header lines and
    keep everything from the first one up to the (``versions`` + 1)th, so the
    excerpt is exactly the most recent release blocks with their headers intact.
    """
    lines = text.splitlines()
    header_indices = [i for i, line in enumerate(lines) if _VERSION_HEADER.match(line)]
    if not header_indices:
        return ChangelogExtract(latest_version="", excerpt="")

    start = header_indices[0]
    end = (
        header_indices[versions]
        if versions < len(header_indices)
        else len(lines)
    )
    excerpt = "\n".join(lines[start:end]).strip()

    first = _VERSION_HEADER.match(lines[start])
    return ChangelogExtract(
        latest_version=(first.group("version") if first else "").strip(),
        released_on=((first.group("date") or "") if first else "").strip(),
        versions_included=min(versions, len(header_indices)),
        excerpt=excerpt,
    )


def _make_read_changelog(workspace_root: Path):
    """Build the ``read-changelog`` step body bound to a workspace root."""
    changelog_path = (workspace_root / CHANGELOG_FILENAME).resolve()

    def read_changelog(inp: ChangelogIn, ctx: StepContext) -> ChangelogExtract:
        if not changelog_path.is_file():
            raise FileNotFoundError(f"{CHANGELOG_FILENAME} not found at {changelog_path}")
        return _slice_recent_releases(
            changelog_path.read_text(encoding="utf-8"), inp.versions
        )

    return read_changelog


_PROMPT = """You are the Mash changelog summarizer, one step of the pilot-changelog workflow.

You are handed a slice of CHANGELOG.md (the most recent release section(s)) as
your step input under `input.excerpt`, with `input.latest_version` and
`input.released_on`. You do not chat; you produce one structured summary and stop.

Your job:
- Read the excerpt and write a short, plain-language `summary` of what changed in
  the latest release — what a user upgrading would actually notice.
- List the changes that matter most in `highlights`: breaking changes first, then
  notable features and fixes. One crisp line each, in plain words, not the raw
  commit subject. Skip pure-docs/chore churn unless it changes user-facing behavior.
- Set `latest_version` to the version you summarized.

Scanning the code:
- The changelog entries are terse. When one is too vague to explain (e.g. a rename
  or a removed flag), use bash to look it up: a single targeted `rg` in `src/mash`,
  then `sed` on the specific file/lines only if you still need to confirm.
- Do not explore broadly. One or two lookups per unclear entry, then write. If the
  excerpt is already clear, do not run bash at all.

Return only the structured output. Do not ask questions."""


class ChangelogSummarizerSpec(AgentSpec):
    """Workflow agent that summarizes the changelog, scanning code when needed."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def get_agent_id(self) -> str:
        return CHANGELOG_AGENT_ID

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(build_bash_tool(self.workspace_root))
        return tools

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        # Gemma over OpenRouter — the shared subagent model.
        return build_default_llm(CHANGELOG_AGENT_ID)

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=CHANGELOG_AGENT_ID,
            system_prompt=[
                {
                    "type": "text",
                    "text": _PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            skills_enabled=False,
            conversation_history_turns=0,
            max_steps=12,
            temperature=0.2,
        )

    def enable_runtime_tools(self) -> bool:
        return False


def create_spec(*, workspace_root: str) -> ChangelogSummarizerSpec:
    return ChangelogSummarizerSpec(Path(workspace_root).resolve())


def build_metadata() -> AgentMetadata:
    return AgentMetadata(
        display_name="Changelog Summarizer",
        description=(
            "Summarizes the most recent CHANGELOG.md release in plain language, "
            f"scanning the code when an entry is unclear. Runs the "
            f"`{CHANGELOG_WORKFLOW_ID}` workflow."
        ),
        capabilities=[
            "changelog summary",
            f"workflow `{CHANGELOG_WORKFLOW_ID}`",
        ],
        usage_guidance=(
            f"Only useful through the `{CHANGELOG_WORKFLOW_ID}` workflow (the "
            "/changelog command); it refuses free-form chat. Not a delegation target."
        ),
    )


def build_changelog_workflow_spec(workspace_root: Path | None = None) -> WorkflowSpec:
    """The pilot-changelog definition: read CHANGELOG.md, then summarize it."""
    resolved = (workspace_root or Path(".")).resolve()
    return WorkflowSpec(
        workflow_id=CHANGELOG_WORKFLOW_ID,
        input_model=ChangelogIn,
        steps=[
            CodeStep(
                step_id=READ_STEP_ID,
                run=_make_read_changelog(resolved),
                input=ChangelogIn,
                output=ChangelogExtract,
            ),
            AgentStep(
                step_id=SUMMARIZE_STEP_ID,
                agent_spec=ChangelogSummarizerSpec(resolved),
                input=ChangelogExtract,
                output=ChangelogSummary,
            ),
        ],
        metadata={"source": "pilot", "kind": "changelog"},
    )


def register_changelog_command(shell: Any) -> None:
    """Register Pilot's `/changelog` workflow command on a Mash shell."""

    def changelog_command(ctx: Any, args: list[str]) -> None:
        versions = 1
        if args:
            if len(args) > 1 or not args[0].isdigit():
                ctx.renderer.error("Usage: /changelog [versions]")
                return
            versions = int(args[0])

        run = ctx.client.run_workflow(
            CHANGELOG_WORKFLOW_ID,
            session_id=ctx.session_id,
            workflow_input={"versions": versions},
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

                if event_name == "agent.trace":
                    shell.render_runtime_trace_payload(
                        payload,
                        trace_label="Changelog",
                        agent_id=str(payload.get("task_agent_id") or "") or None,
                    )
                    continue
                if event_name == "request.completed":
                    final_payload = payload
                    break
                if event_name in ("request.error", "workflow.error"):
                    error = payload.get("error")
                    raise RuntimeError(str(error or "changelog workflow failed"))
        finally:
            shell.chain_renderer.finish_trace()

        if final_payload is not None:
            shell.render_final_response(
                ctx,
                final_payload.get("response"),
                str(final_payload.get("text") or ""),
                shell.chain_renderer.take_streamed_text(),
            )

    shell.register_command(
        Command(
            name="changelog",
            help="Summarize the most recent CHANGELOG.md release(s)",
            handler=changelog_command,
        )
    )

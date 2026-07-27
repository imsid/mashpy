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

from pydantic import BaseModel, Field

from mash import AgentSpec, AgentStep, CodeStep, StepContext, WorkflowSpec
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

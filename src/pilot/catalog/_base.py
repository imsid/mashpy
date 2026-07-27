"""Shared base class and utilities for agent specs."""

from __future__ import annotations

import abc
import os
from pathlib import Path
from typing import Any, Sequence

from mash.core.config import AgentConfig
from mash.core.llm import LLMProvider
from mash.core.llm.gemini import GeminiProvider
from mash.core.llm.oss import GemmaProvider
from mash import AgentSpec
from mash.skills.registry import SkillRegistry
from mash.tools.bash import BashTool

from ..prompt import build_base_prompt, build_repo_context

APP_NAME = "Mash Pilot"

# pilot/skills — shared by every catalog agent that registers custom skills.
PILOT_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

# The primary pilot agent runs Gemini directly; every subagent runs the open
# Gemma model over OpenRouter's Chat Completions gateway. Both models are
# overridable via ``PILOT_PRIMARY_MODEL`` / ``PILOT_SUBAGENT_MODEL``.
DEFAULT_PRIMARY_MODEL = "gemini-3.5-flash"
DEFAULT_SUBAGENT_MODEL = "google/gemma-4-31b-it"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def build_primary_llm(agent_id: str) -> LLMProvider:
    """Build the Gemini provider for the primary pilot agent.

    Reads the key from ``GEMINI_API_KEY`` (via the provider) and the model from
    ``PILOT_PRIMARY_MODEL`` (default :data:`DEFAULT_PRIMARY_MODEL`)."""
    if not os.getenv("GEMINI_API_KEY", "").strip():
        raise RuntimeError(f"Agent {agent_id!r} requires GEMINI_API_KEY.")
    return GeminiProvider(
        app_id=agent_id,
        model=os.getenv("PILOT_PRIMARY_MODEL", DEFAULT_PRIMARY_MODEL),
    )


def build_default_llm(agent_id: str) -> LLMProvider:
    """Build the OpenRouter-backed Gemma provider for a pilot subagent.

    Reads the OpenRouter key from ``OPENROUTER_API_KEY`` and the model from
    ``PILOT_SUBAGENT_MODEL`` (default :data:`DEFAULT_SUBAGENT_MODEL`)."""
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(f"Agent {agent_id!r} requires OPENROUTER_API_KEY.")
    return GemmaProvider(
        app_id=agent_id,
        model=os.getenv("PILOT_SUBAGENT_MODEL", DEFAULT_SUBAGENT_MODEL),
        base_url=OPENROUTER_BASE_URL,
        api_key=api_key,
    )


def cached_docs_for_scope(
    workspace_root: Path,
    *,
    doc_roots: Sequence[str] = (),
    extra_doc_paths: Sequence[str] = (),
) -> list[str]:
    """Collect cached doc paths for a scope.

    Single indirection point patched by tests to inject fake doc paths
    without touching the filesystem.
    """
    return scope_doc_paths(
        workspace_root,
        doc_roots=doc_roots,
        extra_doc_paths=extra_doc_paths,
    )


def scope_doc_paths(
    workspace_root: Path,
    *,
    doc_roots: Sequence[str],
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


def build_bash_tool(workspace_root: Path) -> BashTool:
    """Construct a BashTool scoped to ``workspace_root``."""
    return BashTool(working_dir=str(workspace_root))


COMMON_COPILOT_RULES = (
    "If the delegated prompt asks you to perform a focused codebase task, do it and answer directly instead of asking back-and-forth permission questions.",
)

COMMON_SEARCH_RULES = (
    "Use bash only when one targeted verification is still needed.",
    "For command, inventory, or existence questions, start with one targeted `rg` and answer as soon as it gives enough evidence.",
    "Use `sed` only after `rg` points to a specific file and line range that needs verification.",
    "Prefer a single `rg` or one small `sed` read over repeated broad reads.",
    "Do not repeat an equivalent bash command.",
    "Do not ask the user for permission to inspect code; inspect the code and answer directly.",
    "If you already have enough evidence, stop and answer instead of continuing to explore.",
)


class CopilotAgentSpec(AgentSpec, abc.ABC):
    """Shared base for pilot and copilot agent specs."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def build_skills(self) -> SkillRegistry:
        return SkillRegistry()

    def build_llm(self) -> LLMProvider:
        return build_default_llm(self.get_agent_id())

    def enable_runtime_tools(self) -> bool:
        return True

    @abc.abstractmethod
    def build_system_prompt(self) -> list[dict[str, Any]]: ...

    def _build_copilot_prompt_blocks(
        self,
        *,
        scope: str,
        doc_roots: Sequence[str],
        cache_label: str,
        extra_rules: Sequence[str] = (),
    ) -> list[dict]:
        blocks: list[dict] = [
            {
                "type": "text",
                "text": build_base_prompt(
                    repo=str(self.workspace_root),
                    role=f"You are the {APP_NAME} copilot for `{scope}`.",
                    extra_rules=(
                        *COMMON_COPILOT_RULES,
                        f"Use the cached {cache_label} docs before using bash.",
                        *COMMON_SEARCH_RULES,
                        *extra_rules,
                    ),
                ),
                "cache_control": {"type": "ephemeral"},
            }
        ]
        repo_context = build_repo_context(
            repo=str(self.workspace_root),
            cached_files=cached_docs_for_scope(
                self.workspace_root,
                doc_roots=doc_roots,
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

    def _build_copilot_config(self, agent_id: str) -> AgentConfig:
        return AgentConfig(
            app_id=agent_id,
            system_prompt=self.build_system_prompt(),
            skills_enabled=False,
            conversation_history_turns=0,
            max_steps=10,
            temperature=0.2,
        )

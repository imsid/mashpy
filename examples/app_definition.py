"""Shared runtime definition used by example CLI and API entrypoints."""

from __future__ import annotations

from pathlib import Path

from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider, LLMProvider
from mash.memory.store import MemoryStore, SQLiteStore
from mash.runtime import MashRuntimeDefinition
from mash.skills.base import Skill
from mash.skills.registry import SkillRegistry
from mash.tools.bash import BashTool
from mash.tools.registry import ToolRegistry

from ._bootstrap import load_example_env, require_anthropic_api_key

APP_ID = "hello-mash"
load_example_env()


class HelloMashDefinition(MashRuntimeDefinition):
    def __init__(self, root: Path, app_id: str = APP_ID) -> None:
        self.root = root
        self.app_id = app_id

    def get_app_id(self) -> str:
        return self.app_id

    def build_store(self) -> MemoryStore:
        return SQLiteStore(self.root / ".mash" / f"{self.app_id}.db")

    def build_tools(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(BashTool(working_dir=str(self.root)))
        return tools

    def build_skills(self) -> SkillRegistry:
        skills = SkillRegistry()
        skill_dir = Path(__file__).resolve().parent / "skills" / "repo-audit"
        skills.register(
            Skill(
                type="custom",
                name="repo-audit",
                description="Checklist for auditing a repository",
                location=str(skill_dir),
            )
        )
        return skills

    def build_llm(self) -> LLMProvider:
        return AnthropicProvider(
            app_id=self.app_id,
            api_key=require_anthropic_api_key(),
        )

    def build_agent_config(self) -> AgentConfig:
        return AgentConfig(
            app_id=self.app_id,
            system_prompt="You are a concise CLI assistant with access to tools and skills.",
            skills_enabled=True,
        )

    def get_log_destination(self) -> Path:
        return self.root / ".mash" / "logs" / f"{self.app_id}.jsonl"

"""Skill tool for loading SKILL.md content into the conversation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from mash.skills.registry import SkillRegistry
from mash.tools.base import ToolResult


class SkillTool:
    """Meta-tool that loads a skill's instructions at runtime."""

    name = "Skill"

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry
        self.description = ""
        self.parameters: Dict[str, Any] = {}

    async def execute(self, args: Dict[str, Any]) -> ToolResult:
        raw_name = (args or {}).get("name")
        if isinstance(raw_name, str):
            skill_name = raw_name.strip()
        elif raw_name is None:
            skill_name = ""
        else:
            skill_name = str(raw_name).strip()

        if not skill_name:
            return ToolResult.error("Skill name is required.")

        skill = self._registry.get(skill_name)
        if skill is None:
            available = ", ".join(self._available_skill_names())
            return ToolResult.error(
                f"Skill '{skill_name}' not found. Available skills: {available}"
            )

        if skill.content is not None:
            payload = {
                "base_path": skill.location or "",
                "skill_path": "",
                "skill_name": skill.name,
                "skill_md": skill.content,
            }
            return ToolResult.success(json.dumps(payload, ensure_ascii=True))

        if not skill.location:
            return ToolResult.error(
                f"Skill '{skill_name}' has no content or location configured."
            )

        skill_dir = Path(skill.location)
        skill_path = skill_dir / "SKILL.md"
        if not skill_path.exists():
            return ToolResult.error(f"Skill file not found: {skill_path}")

        try:
            skill_md = skill_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult.error(f"Failed to read skill file: {exc}")

        payload = {
            "base_path": str(skill_dir),
            "skill_path": str(skill_path),
            "skill_name": skill.name,
            "skill_md": skill_md,
        }
        return ToolResult.success(json.dumps(payload, ensure_ascii=True))

    def to_llm_format(self) -> Dict[str, Any]:
        description = self._build_description()
        input_schema = self._build_input_schema()
        return {
            "name": self.name,
            "description": description,
            "input_schema": input_schema,
        }

    def _available_skill_names(self) -> List[str]:
        return [skill.name for skill in self._registry.list_skills()]

    def _build_input_schema(self) -> Dict[str, Any]:
        names = self._available_skill_names()
        schema = {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name",
                    "enum": names,
                }
            },
            "required": ["name"],
        }
        self.parameters = schema
        return schema

    def _build_description(self) -> str:
        skills = self._registry.list_skills()
        lines = [
            "Execute a skill within the main conversation.",
            "",
            "<skills_instructions>",
            "When users ask you to perform tasks, check if any of the available skills below can help complete the task more effectively. Skills provide specialized capabilities and domain knowledge.",
            "",
            "How to use skills:",
            "- Invoke skills using this tool with the skill name only (no arguments)",
            '- When you invoke a skill, you will see <command-message>The \"{name}\" skill is loading</command-message>',
            "- The skill's prompt will expand and provide detailed instructions on how to complete the task",
            "</skills_instructions>",
            "",
            "<available_skills>",
        ]

        if skills:
            for skill in skills:
                desc = skill.description or ""
                lines.append(f"- name: {skill.name}")
                lines.append(f"  description: {desc}")
        else:
            lines.append("- (none)")

        lines.append("</available_skills>")

        description = "\n".join(lines)
        self.description = description
        return description

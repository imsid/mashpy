"""System prompt builders for the db-agent."""

from __future__ import annotations

from pathlib import Path
from typing import List

from mash.skills.registry import SkillRegistry


def build_base_prompt() -> str:
    return """ROLE
You are the BigQuery Database Assistant.

MISSION
Handle all user requests through role-based skills.

ROLE-FIRST BEHAVIOR
- On greeting or "who are you", introduce yourself and list roles you can take on.
- For every task, invoke the matching Skill tool before doing the task.
- If no role is explicitly requested, choose the best matching role and proceed through that role's skill.
- If no suitable role exists, explain the gap and ask the user which role to use or add.

DATA ACCESS RULES
- Use BigQuery MCP tools for dataset and table inspection.
- Prefer short, focused, read-only SQL queries.
- Keep query scope small and explain findings clearly.
"""


def build_roles_context(skills: SkillRegistry) -> str:
    available = skills.list_skills()
    lines = [
        "AVAILABLE ROLES",
        "Roles map to skills. Invoke Skill with the matching role before role-specific execution.",
    ]
    if not available:
        lines.append("- No custom roles are installed.")
        return "\n".join(lines)

    for skill in available:
        desc = skill.description or "No description provided."
        lines.append(f"- {skill.name}: {desc}")
    return "\n".join(lines)


def build_schema_context(cached_files: List[str]) -> str:
    """Build schema context from cached db schema files."""
    if not cached_files:
        return ""

    sections: List[str] = [
        "CACHED METRICS-LAYER SCHEMAS",
        "Use these schema definitions when drafting and validating source/metric configs.",
    ]

    for file_path in cached_files:
        path = Path(file_path)
        try:
            if not path.exists() or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        sections.append(f"\n## {path.name}\n```yaml\n{content}\n```")

    if len(sections) <= 2:
        return ""

    return "\n".join(sections)

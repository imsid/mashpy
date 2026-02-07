"""Tool registry for managing available tools."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .base import Skill


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """Register a skill.

        Args:
            skill: Skill to register.

        Raises:
            ValueError: If a skill with the same name is already registered.
        """
        if skill.name in self._skills:
            raise ValueError(f"Skill '{skill.name}' is already registered")

        self._skills[skill.name] = skill

    def unregister(self, name: str) -> None:
        """Unregister a skill by name.

        Args:
            name: Name of the skill to unregister.
        """
        self._skills.pop(name, None)

    def get(self, name: str) -> Optional[Skill]:
        """Get a skill by name.

        Args:
            name: Name of the skill.

        Returns:
            Tool if found, None otherwise.
        """
        return self._skills.get(name)

    def list_skills(self) -> List[Skill]:
        """List all registered skills.

        Returns:
            List of skills.
        """
        return list(self._skills.values())

    def get_custom_skills(self, skills_dir: Path) -> List[Skill]:
        skills: List[Skill] = []
        if not skills_dir.exists() or not skills_dir.is_dir():
            return skills

        for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name):
            if entry.is_dir():
                name, description = _parse_skill_frontmatter(entry / "SKILL.md")
                skills.append(
                    Skill(
                        type="custom",
                        name=name or entry.name,
                        description=description,
                        location=str(entry),
                    )
                )

        return skills


def _parse_skill_frontmatter(skill_path: Path) -> Tuple[Optional[str], str]:
    if not skill_path.exists():
        return None, ""

    try:
        content = skill_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, ""

    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, ""

    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break

    if end_idx is None:
        return None, ""

    name: Optional[str] = None
    description = ""
    for raw_line in lines[1:end_idx]:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if key == "name":
            name = value
        elif key == "description":
            description = value

    return name, description

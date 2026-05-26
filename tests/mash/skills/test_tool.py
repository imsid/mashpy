"""Tests for skill loading behavior."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mash.skills import Skill, SkillRegistry
from mash.skills.tool import SkillTool


class SkillToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_loads_inline_dynamic_skill_content(self) -> None:
        registry = SkillRegistry()
        registry.register(
            Skill(
                type="dynamic",
                name="workflow:test:v1",
                description="Dynamic workflow skill.",
                content="# Workflow\nRun the task.",
            )
        )

        result = await SkillTool(registry).execute({"name": "workflow:test:v1"})

        self.assertFalse(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["skill_name"], "workflow:test:v1")
        self.assertEqual(payload["skill_md"], "# Workflow\nRun the task.")
        self.assertEqual(payload["skill_path"], "")

    async def test_loads_filesystem_skill_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "test-skill"
            skill_dir.mkdir()
            skill_path = skill_dir / "SKILL.md"
            skill_path.write_text("# Filesystem\nRun it.", encoding="utf-8")
            registry = SkillRegistry()
            registry.register(
                Skill(
                    type="custom",
                    name="test-skill",
                    description="Filesystem skill.",
                    location=str(skill_dir),
                )
            )

            result = await SkillTool(registry).execute({"name": "test-skill"})

        self.assertFalse(result.is_error)
        payload = json.loads(result.content)
        self.assertEqual(payload["skill_name"], "test-skill")
        self.assertEqual(payload["skill_md"], "# Filesystem\nRun it.")

    def test_skill_requires_content_or_location(self) -> None:
        with self.assertRaises(ValueError):
            Skill(type="dynamic", name="broken")

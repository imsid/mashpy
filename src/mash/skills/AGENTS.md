# AGENTS Guide for `src/mash/skills`

## Scope
Skill metadata registry and runtime skill-loading tool.

## Invariants
- `SkillRegistry` enforces unique skill names.
- Custom skills are discovered from directories and optional frontmatter in `SKILL.md`.
- Skill tool name is `Skill` (capitalized) and should remain stable unless a migration is planned.

## Skill Tool Contract
- Input: `{ "name": <skill_name> }`
- Output payload JSON must include:
  - `base_path`
  - `skill_path`
  - `skill_name`
  - `skill_md`
- Missing skills/files should return `ToolResult.error` with actionable text.

## Agent Integration
- Agent registers the Skill tool only when `skills_enabled=True` and skills exist.

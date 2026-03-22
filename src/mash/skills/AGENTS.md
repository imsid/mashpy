# AGENTS Guide for `src/mash/skills`

## What Must Stay True
- Skills remain optional capability extensions, not a replacement for built-in tools.
- The skill registry stays the source of enabled skills for an agent.
- Skill-to-tool adaptation remains in this package.

## Change Rules
- Keep skill discovery/registration separate from runtime host composition.
- Preserve the basic skill interface expected by agents.
- If skill loading behavior changes, update package docs and tests that rely on it.

## Minimal Validation
- `python -m compileall src/mash/skills`
- Verify one registry path and one skill-to-tool adaptation path.

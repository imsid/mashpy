# AGENTS Guide for `src/mash/agents`

## What Must Stay True
- This package contains built-in agent specs, not core runtime primitives.
- Built-in agents should compose existing Mash runtime, tool, and memory APIs rather than reimplement them.
- Public exports from `src/mash/agents/__init__.py` stay stable for host composition.

## Change Rules
- Keep module-specific behavior and prompt text inside each built-in agent package.
- Avoid coupling built-in agents to `pilot` prompt structure or routing policy.
- If a built-in agent depends on logs, sessions, or host contracts, preserve those interfaces in the owning modules.

## Minimal Validation
- `python -m compileall src/mash/agents`
- Verify one import path for the exported built-in agent spec.

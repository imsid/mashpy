# AGENTS Guide for `src/mash/memory`

## What Must Stay True
- Package-level exports from `src/mash/memory/__init__.py` remain stable unless intentionally changed.
- Store, retrieval, and compaction responsibilities stay separated within this package.
- Memory behavior remains reusable across primary agents and subagents.

## Change Rules
- Keep persistence concerns in `store/` and retrieval concerns in `search/`.
- If storage schemas or retrieval behavior change, update the targeted tests for those paths.
- Avoid coupling memory internals to a single host surface or built-in agent.

## Minimal Validation
- `python -m compileall src/mash/memory`
- Verify one store path and one retrieval path.

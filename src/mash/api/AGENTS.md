# AGENTS Guide for `src/mash/api`

## What Must Stay True
- `create_app` and `run_host` remain the public API-host entrypoints.
- API configuration flows through `MashHostConfig`.
- Telemetry UI asset serving stays rooted in this package.
- Public exports from `src/mash/api/__init__.py` remain stable unless intentionally changed.

## Change Rules
- Keep HTTP and FastAPI wiring in `mash.api`; shared runtime composition belongs in `mash.runtime`.
- Avoid spreading HTTP-specific behavior into CLI, tool, or core modules.
- If routing, startup behavior, or externally visible API defaults change, update the repo docs and targeted tests.

## Minimal Validation
- `python -m compileall src/mash/api`
- Verify one app creation path and one host run path.

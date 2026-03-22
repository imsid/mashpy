# AGENTS Guide for `src/mash/cli`

## What Must Stay True
- `main.py` remains the CLI entrypoint.
- Shell, REPL, and rendering behavior stay inside this package.
- Command dispatch and client-side session routing remain coherent across local and remote usage.
- Public exports from `src/mash/cli/__init__.py` remain stable unless intentionally changed.

## Change Rules
- Keep terminal UX and command behavior in `mash.cli`; shared agent hosting contracts belong in `mash.runtime`.
- If output format or shell lifecycle behavior changes, update the relevant CLI tests.
- Preserve subagent event rendering and session handoff behavior unless intentionally redesigning the CLI UX.

## Minimal Validation
- `python -m compileall src/mash/cli`
- Verify one command parse path and one shell/render path.

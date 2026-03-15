# AGENTS Guide for `src/mash/cli`

## Scope
Remote CLI for Mash deployments: connection config, host API client, REPL loop, slash command routing, and terminal rendering.

## Invariants
- `mash.cli` remote commands are remote-only and must not build or import app runtimes locally.
- CLI modules must only talk to Mash host HTTP APIs.
- Local-only commands (`/help`, `/exit`, `/clear`) run in the CLI without remote calls.
- Deployment commands (`/status`, `/agents`, `/session`, `/sessions`, `/history`, `/use`) call `MashHostClient`.
- Non-command user messages must invoke a remote agent through the host API.
- `CLIContext` stays lean (`api_base_url`, `agent_id`, `session_id`, `client`, `renderer`).

## Command Changes
- Update help text and docs together.
- Keep command parsing tolerant but explicit; unknown commands should never crash the REPL.
- If command names change, ensure completion in `REPL._get_command_words` still matches.

## UI/Rendering
- `RichRenderer` is presentation-only.
- Rendering must not become a source of truth for session or deployment state.

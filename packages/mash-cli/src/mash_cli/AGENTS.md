# AGENTS Guide for `packages/mash-cli/src/mash_cli`

## Scope
CLI shell for Mash apps: REPL loop, slash command routing, terminal rendering, and host/client composition.

## Invariants
- `CLIAppShell` must compose through `MashAgentHost` and hold only a primary `MashAgentClient`.
- CLI modules must not call `MashAgentServer` directly.
- Command routing split:
  - Local-only commands (`/help`, `/exit`, `/clear`) run in CLI without runtime calls.
  - Runtime commands (`/session`, `/prefs`, `/app_data`, `/history`, `/compact`) call `MashAgentClient` control methods.
- Non-command user messages must call `MashAgentClient.invoke(...)`.
- Runtime remains UI-agnostic; rendering only happens in CLI modules.
- `CLIContext` stays lean (`app_id`, `session_id`, `runtime`, `renderer`).
- Built-in commands exposed by default:
  `/help`, `/exit`, `/clear`, `/session`, `/prefs`, `/app_data`, `/history`, `/compact`.
- `/session` output reports primary app identity (`app_id`) and configured subagent ids from runtime state.
- Non-command input must flow through shell message handling and render via `RichRenderer`.
- Command events are emitted through a client-backed logger proxy (`emit_command_event` control action).

## App Interface
- App authors implement `MashRuntimeDefinition` and register custom commands directly on `CLIAppShell`.
- App-specific runtime startup/shutdown behavior belongs in `MashRuntimeDefinition.on_startup(runtime)` / `on_shutdown(runtime)`.
- `MashRuntimeDefinition` must not be used for CLI command registration.

## Command Changes
- Update help text and command docs together.
- Keep command parsing tolerant but explicit; unknown commands should never crash the REPL.
- If command names change, ensure completion (`REPL._get_command_words`) still matches.

## Conversation + Compaction
- Compaction checkpoints use metadata type `summary_checkpoint` and are included in future history windows.
- Preserve session token accumulation behavior (`session_total_tokens`) when changing save logic.

## UI/Rendering
- `RichRenderer` is presentation-only.
- `ChainOfThoughtRenderer` consumes trace events; it is not state truth for runtime logic.

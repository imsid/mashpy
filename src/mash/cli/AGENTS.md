# AGENTS Guide for `src/mash/cli`

## Scope
CLI shell for Mash apps: app orchestration, commands, REPL loop, rich rendering, chain trace rendering.

## Invariants
- `MashApp` owns session lifecycle and command registry setup.
- Built-in commands currently exposed by default:
  `/help`, `/exit`, `/clear`, `/session`, `/prefs`, `/app_data`, `/history`, `/compact`.
- Non-command input must flow through `message_handler` and render via the configured renderer.
- Conversation continuity prepends recent turns before the new user message.

## Command Changes
- Update help text and command docs together.
- Keep command parsing tolerant but explicit; unknown commands should never crash the REPL.
- If command names change, ensure completion (`REPL._get_command_words`) still matches.

## Conversation + Compaction
- Compaction checkpoints use metadata type `summary_checkpoint` and are included in future history windows.
- Preserve session token accumulation behavior (`session_total_tokens`) when changing save logic.

## UI/Rendering
- `RichRenderer` should stay presentation-only.
- `ChainOfThoughtRenderer` consumes trace events; avoid making it a source of state truth for agent logic.

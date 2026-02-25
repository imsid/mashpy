# AGENTS Guide for `src/mash/cli`

## Scope
CLI shell for Mash apps: app orchestration, commands, REPL loop, rich rendering, chain trace rendering.

## Invariants
- `AbstractMashApp` owns app bootstrap, session lifecycle, and command registry setup.
- App subclasses implement the SDK builder interface (`get_app_id`, `build_store`, `build_tools`, `build_skills`, `build_llm`, `build_agent_config`, `get_log_destination`).
- MCP server configs returned by apps are typed `MCPServerConfig` objects (not raw dicts).
- Built-in commands currently exposed by default:
  `/help`, `/exit`, `/clear`, `/session`, `/prefs`, `/app_data`, `/history`, `/compact`.
- `AbstractMashApp.__init__()` boot order matters: build deps -> validate `config.app_id` -> create `Agent` -> initialize runtime -> register app commands -> `on_startup()`.
- Non-command input must flow through `message_handler` and render via the configured renderer.
- Conversation continuity prepends recent turns before the new user message.
- Runtime tool registration and MCP event/log scoping should use `agent.config.app_id`.

## App Interface Changes
- Prefer subclass hooks over custom constructor wiring in apps.
- App-specific startup/shutdown behavior should use `on_startup()` / `on_shutdown()` instead of overriding runtime initialization.
- `cleanup()` must remain safe when no MCP manager was created (e.g., app starts without MCP configs).

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

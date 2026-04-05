# CLI

`src/mash/cli` contains the `mash` command-line, remote shell behavior, and terminal rendering.

## Top-Level Commands

Global

- `--version`: print the installed Mash version and docs URL.

Connection bootstrap

- `connect --api-base-url URL [--api-key KEY] [--agent ID]`: persist a default deployment connection used by the other commands.

Common options shared by most host-facing commands

- `--api-base-url URL`
- `--api-key KEY`
- `--agent ID`
- If these are omitted, the CLI resolves them from env (`MASH_API_BASE_URL`, `MASH_API_KEY`) or saved config from `mash connect`.

Host-facing commands

- `status [common]`: show deployment base URL, primary agent, and agent count.
- `agents [common]`: list available agents and their roles.
- `invoke [common] MESSAGE [--session-id ID]`: invoke a remote agent once and print the response.
- `sessions [common]`: list sessions for the target agent.
- `history [common] --session-id ID [--limit N]`: show turns for a specific remote session.
- `repl [common] [--session-id ID]`: start an interactive remote shell session.

Host management

- `host serve [options]`: run the Mash host API server.
- `--host-app module:attribute` or `MASH_HOST_APP`: host target to load.
- `--host`: API bind host. Defaults to `127.0.0.1` or `MASH_API_HOST`.
- `--port`: API bind port. Defaults to `8000` or `MASH_API_PORT` / `PORT`.
- `--runtime-bind-host`: internal runtime bind host. Defaults to `127.0.0.1` or `MASH_RUNTIME_BIND_HOST`.
- `--api-key`: optional bearer API key, or `MASH_API_KEY`.
- `--cors-origin`: repeatable allowed CORS origin.
- `--memory-db`: optional SQLite path for telemetry memory search, or `MASH_MEMORY_DB`.
- `--disable-observability`: disable telemetry endpoints.

Notes

- If no command is given, the CLI prints help.
- Agent resolution defaults to the deployment primary agent unless `--agent` overrides it.

## Interactive Shell Commands

- `/help`: list available slash commands.
- `/exit`: leave the shell.
- `/clear`: clear the terminal renderer.
- `/status`: show deployment, current agent, and session state.
- `/agent`: list agents exposed by the host.
- `/session`: show the current remote session details.
- `/sessions`: list sessions for the current agent.
- `/history [limit]`: show conversation history for the current session.
- `/use <agent_id>`: switch to a different agent, deriving the subagent session ID when moving from the primary agent to a subagent.

## Main Components
- `main.py`: top-level parser, command dispatch, and command execution.
- `client.py`: HTTP client used to talk to hosted Mash agents.
- `shell.py`: streamed remote shell UX and subagent event rendering.
- `commands.py` and `default_commands.py`: slash-command model, registration, and built-in shell commands.
- `render.py` and `chain_renderer.py`: terminal formatting for responses, tool calls, and chained/subagent output.
- `config.py`, `repl.py`, and `types.py`: saved connection config and shell state types.

## Public Exports
- `MashHostClient`, `MashHostClientError`
- `MashRemoteShell`, `ShellTarget`
- `CLIContext`
- `Command`, `CommandRegistry`

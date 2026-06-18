# CLI

`src/mash/cli` contains the `mash` command-line, remote shell behavior, and terminal rendering.

## Top-Level Commands

Global

- `--version`: print the installed Mash version and docs URL.

Connection bootstrap

- `connect --api-base-url URL [--api-key KEY] [--agent ID | --host ID]`: persist a default deployment connection and target used by the other commands. `--host` targets an existing composition (validated against the deployment).
- `compose --host ID --primary AGENT [--subagents a,b] [--workflows w1,w2]`: define (or replace) the host composition on the deployment, then pin it as the target. The `PUT` is idempotent; re-run `compose` to recompose. `--api-base-url` falls back to env or the saved config, and the saved `--agent` target is cleared so the host takes effect.

Common options shared by most host-facing commands

- `--api-base-url URL`
- `--api-key KEY`
- `--agent ID` (bare-agent targeting) or `--host ID` (host targeting)
- If these are omitted, the CLI resolves them from env (`MASH_API_BASE_URL`, `MASH_API_KEY`) or saved config from `mash connect`.

Host-facing commands

- `status [common]`: show deployment base URL, agent count, and defined hosts.
- `browse [common]`: browse the pool in one view — agents, pool-wide workflows (unfiltered by host), and defined host compositions.
- `agents [common]`: list pooled agents and their display names.
- `hosts [common]`: list defined host compositions.
- `sessions [common]`: list sessions for the target agent.
- `history [common] --session-id ID [--limit N]`: show turns for a specific remote session.
- `repl [common] [--session-id ID]`: start an interactive remote shell session, pinned to the connected target. To change the composition, exit and run `mash compose` (or `mash connect --agent`) again.

Host management

- `host serve [options]`: run the Mash host API server.
- `--host-app module:attribute` or `MASH_HOST_APP`: host target to load.
- `--host`: API bind host. Defaults to `127.0.0.1` or `MASH_API_HOST`.
- `--port`: API bind port. Defaults to `8000` or `MASH_API_PORT` / `PORT`.
- `--api-key`: optional bearer API key, or `MASH_API_KEY`.
- `--cors-origin`: repeatable allowed CORS origin.
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
- `/host`: list the host compositions defined on the deployment, including their attached workflows. The shell's own target is fixed at connect time.
- `/trace [N]`: show trace analysis for the N most recent traces (default 1). Renders timing breakdown, tool stats, and slowest operations for each trace.
- `/feedback <message>`: record a free-form note or bug report about the current session. The message is stored with the host, agent, session, and last request id so app developers can read it back later through the feedback API.
- `/workflow` or `/workflow list`: list registered workflows (host-attached only when connected through a host).
- `/workflow run <workflow_id> [dedup_key]`: start a workflow run.
- `/workflow status <workflow_id> <run_id>`: show workflow run status.

## Main Components
- `main.py`: top-level parser, command dispatch, and command execution.
- `client.py`: HTTP client used to talk to hosted Mash agents.
- `shell.py`: streamed remote shell UX, runtime-event rendering, and subagent event rendering.
- `commands.py` and `default_commands.py`: slash-command model, registration, and built-in shell commands.
- `render.py` and `chain_renderer.py`: terminal formatting for responses, tool calls, and chained/subagent output.
- `config.py`, `repl.py`, and `types.py`: saved connection config and shell state types.

### Live token streaming

When the host streams a response (`llm.response.delta` events), the chain
renderer shows the answer live and formatted, then renders it exactly once:

- `chain_renderer._on_runtime_response_delta` buffers incoming chunks and flushes
  each *completed* markdown block (`_flush_response_markdown` /
  `_split_complete_markdown`) as it finishes — so the answer streams in with full
  markdown formatting (headings, bold, syntax-highlighted code) rather than as
  raw text or a single end-of-turn dump. Block-at-a-time flushing avoids the
  scrollback artifacts a whole-buffer `rich.Live` repaint produces on long
  output, and keeps unterminated code fences buffered until they close.
- Single render is enforced in `shell.py`: the legacy per-step preview render is
  gated on `chain_renderer.response_streamed()` (a non-consuming peek) and the
  terminal `request.completed` render is gated on
  `chain_renderer.take_response_streamed()` (consuming). When tokens streamed
  live, both fall through and nothing re-renders.
- Non-streaming providers (no deltas) keep the previous behavior: the preview
  and/or terminal `renderer.markdown` panel renders the complete response once.

## Public Exports
- `MashHostClient`, `MashHostClientError`
- `MashRemoteShell`, `ShellTarget`
- `CLIContext`
- `Command`, `CommandRegistry`

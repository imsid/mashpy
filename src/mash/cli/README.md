# CLI

`src/mash/cli` contains the `mash` command line, the REPL, and terminal rendering.

## Ship your own client

An app wraps the same command line with its own name and defaults:

```python
import getpass
from mash.cli import App

app = App(
    name="fudge",
    default_host="fudge",
    default_base_url="http://127.0.0.1:8001",
    env_prefix="FUDGE",
    default_user=getpass.getuser,
)

def main() -> int:
    return app.main()
```

Register `fudge = "fudge.cli:main"` in `[project.scripts]`. Running `fudge`
opens the REPL; `fudge run "hello"`, `fudge repl --continue`, `fudge workflow
run export`, `fudge sessions` and `fudge status` use the same connection.
The host must already exist on the deployment. Use `default_agent` instead
of `default_host` for a bare agent; setting both is an error. Without either,
the client resolves the deployment's sole host or agent, or asks for a target.

Flags override environment variables. `env_prefix="FUDGE"` reads
`FUDGE_API_BASE_URL`, `FUDGE_API_KEY` and `FUDGE_USER_ID` before their `MASH_`
equivalents. The app's default URL comes next, then the saved connection.
Saved keys and users apply only when the saved URL matches the resolved URL.
The user falls back to `default_user`, a string or a callable. `connect`
validates and saves the resolved connection and user.

`commands` takes extra `Command` objects; a handler receives `CLIContext`
and a list of arguments, and may be synchronous or async. `tool_lines` maps
tool names to functions taking the arguments dict and returning a display
line. `workflow_renderers` maps workflow ids to result callbacks.
`theme` overrides the shared palette (`accent`, `agent`, `ok`, `error`,
`warn`, `muted`, `info`) in Rich output and the live input area. `name` also
sets the header and history file under `~/.mash/history/`.

Apps omit `compose`, `host`, `agent`, `sandbox` and `browser` operator
commands unless `operator_commands=True`. The built-in `mash` command is
`App(name="mash", operator_commands=True)` and prints help when invoked
without a command.

## Top-Level Commands

Global

- `--version`: print the installed Mash version and docs URL.
- Every command first loads the nearest `.env` from the working directory up, without overriding variables already set. `agent serve` is the exception: an agent process takes its environment from the host, which leaves out `MASH_HOST_SECRET` and `MASH_API_KEY`. The library itself never reads `.env`.

Connection bootstrap

- `connect --api-base-url URL [--api-key KEY] [--user-id ID]`: validate and persist the deployment connection used by the other commands. `connect` checks `GET /health` before saving, so a wrong URL or key fails here. `--user-id` saves the user that REPL requests and `/workflow run` act for; every request needs one. It saves the connection only; targeting lives in per-command `--agent`/`--host` flags and `mash compose`, which keeps the saved user.
- `compose --host ID [--agents a,b] [--workflows w1,w2]`: define (or replace) the host composition on the deployment, then pin it as the target. The `PUT` is idempotent; re-run `compose` to recompose. `--api-base-url` falls back to env or the saved config.

Common options shared by most host-facing commands

- `--api-base-url URL`
- `--api-key KEY`
- `--agent ID` (bare-agent targeting) or `--host ID` (host targeting)
- `--user-id ID`: the user to act for, over the saved connection's
- If these are omitted, the CLI resolves them from env (`MASH_API_BASE_URL`, `MASH_API_KEY`) or saved config from `mash connect`.

Host-facing commands

- `status [common]`: show deployment base URL, agent count, and defined hosts.
- `browse [common]`: browse the pool in one view — agents, pool-wide workflows (unfiltered by host), and defined host compositions.
- `agents [common]`: list pooled agents and their display names.
- `workflows [common]`: list the deployment's workflows with step count and step kinds.
- `hosts [common]`: list defined host compositions.
- `sessions [common]`: list sessions for the target agent.
- `history [common] --session-id ID [--limit N]`: show turns for a specific remote session.
- `repl [common] [--session-id ID | --continue | --resume] [--debug]`: start the REPL, pinned to the resolved target and user. Without a session flag it starts a new session. `--continue` goes on in the user's most recent session for the target, `--resume` opens a picker of the user's sessions (first message, turns, time since last activity), and `--session-id` names one. A resumed session starts with its last two turns, dimmed. `--debug` starts it at the detail level, which Ctrl-O also switches. It needs a user from `--user-id` or `mash connect --user-id`. To change the composition, exit and run `mash compose` (or `mash repl --agent <id>`) again.
- `run [common] [MESSAGE] [--session-id ID | --continue] [--json]`: submit one request, print its answer and exit. The message is the argument, or stdin when there is none (`git diff | mash run`). The answer goes to stdout as the agent wrote it. Tool and subagent lines go to stderr when stderr is a terminal, so a pipe gets the answer alone. `--json` prints each stream event as one JSON object per line. Without a session flag the request starts a new session. The exit status is 0 when the request completed, 1 when it failed, 2 when it asked for input (an approval, a question, a browser handoff), in which case it is cancelled, and 130 on Ctrl-C, which also cancels it.
- `workflow list [common]`: list the workflows the target can run.
- `workflow run [common] WORKFLOW_ID [--input JSON | --input @FILE] [--dedup-key KEY] [--no-follow]`: start a run for the connection's user and follow its steps to the result. `--no-follow` prints the run id and exits. A workflow of code steps runs without a user.
- `workflow status [common] WORKFLOW_ID RUN_ID`: show a run's status, steps and result.
- `workflow resume [common] WORKFLOW_ID RUN_ID`: resume a failed run from its failed step.
- The `workflow` commands exit 1 when the command or a step fails.

Host management

- `host serve [options]`: run the Mash host API server.
- `--host-app module:attribute` or `MASH_HOST_APP`: host target to load.
- `--host`: API bind host. Defaults to `127.0.0.1` or `MASH_API_HOST`.
- `--port`: API bind port. Defaults to `8000` or `MASH_API_PORT` / `PORT`.
- `--api-key`: bearer API key, or `MASH_API_KEY`. Required unless `--host` is a loopback address; without one `host serve` exits.
- `--cors-origin`: repeatable allowed CORS origin.
- `--disable-observability`: disable telemetry endpoints.
- `MASH_HOST_SECRET`: derives each agent's, each sandbox's and each browser's token and enables the `/h2a`, `/sandbox` and `/browser` relays.
- The host runs `agent serve` once per agent as a child process. With a reachable Docker daemon it starts sandboxes for the agents' code, and browsers when an agent's spec lists `browser_sites()`; it logs which it chose, and the images.

Agent processes

- `agent serve --agent ID [options]`: serve one agent of the pool behind its own `AgentServer`. The host runs this for every agent; you rarely run it by hand. The pool is imported but not started; the companion's `RunSubAgents` and `RunWorkflow` calls go to the host's `/h2a` relay.
- `--host-app module:attribute` or `MASH_HOST_APP`: the same host target the host runs.
- `--host`: bind host. Defaults to `127.0.0.1` or `MASH_AGENT_BIND_HOST`.
- `--port`: bind port. Defaults to `9000` or `MASH_AGENT_PORT`.
- `MASH_AGENT_TOKEN` (required): the agent's token. Every route but `/health` requires it.
- `MASH_HOST_API_URL` (required): the host's base URL, for the companion's calls and the sandbox tools.
- `MASH_SANDBOX_ENABLED=1`: the host has sandboxes, so the agent gets `run_code` and `edit_file`, and `sandbox_put` and `sandbox_get` when its spec declares file roots.
- `MASH_BROWSER_ENABLED=1`: the host has browsers, so the agent gets the browser tools when its spec lists sites.
- `MASH_PARENT_PID`: the process shuts down when this pid's process exits.
- On start the process continues this agent's requests that are `running`, from their checkpoints. Idle, it makes no database queries and its connection pool closes idle connections.

Sandboxes

- `sandbox serve [--host H] [--port 9000] [--workspaces DIR]`: the sandbox daemon, run inside each sandbox container. Requires `MASH_SANDBOX_TOKEN`.

Sandbox administration (run on the Docker host; sandboxes are found by the `mash.sandbox.user` label; `--namespace` defaults to `MASH_SANDBOX_NAMESPACE`, then `mash`)

- `sandbox ls [--namespace NS]`: list sandboxes with user, status, uptime, and container name (the volume has the same name).
- `sandbox shell <user_id> (--agent ID | --root) [--namespace NS]`: start the sandbox if it is stopped, then `docker exec -it` a bash shell as that agent's uid in its folder, or as root in `/home/agent/workspaces`. An agent that has not used the sandbox yet has no uid, and the command says so. Needs the docker CLI. History is appended to `/home/agent/workspaces/.admin_history`; admin commands are not in the event log.
- `sandbox stop <user_id> [--namespace NS]`: stop the sandbox now. Its volume is kept, and the user's next call or link starts it again.

Browsers

- `browser serve [--host H] [--port 9000] [--home DIR] [--novnc-dir DIR]`: the browser daemon, run inside each browser container. It starts Xvfb, Chromium, x11vnc and Playwright MCP as its children and restarts any that exits. `--cdp-url` and `--mcp-url` use a Chromium and a Playwright MCP that are already running instead. Requires `MASH_BROWSER_TOKEN`.

Browser administration (run on the Docker host; browsers are found by the `mash.browser.user` label; `--namespace` as for sandboxes)

- `browser ls [--namespace NS]`: list browsers with user, status, uptime, and container name (the volume has the same name).
- `browser view <user_id> [--public-url URL]`: print a live-view link to the user's browser, valid for 15 minutes. Needs `MASH_HOST_SECRET` and `MASH_PUBLIC_URL` (or `--public-url`), the address users reach the host at.
- `browser stop <user_id> [--namespace NS]`: stop the browser now. Its profile is kept, and the user's next browser call starts it again.

Notes

- If no command is given, the CLI prints help.
- A host target runs on the companion; `--agent` targets one agent directly.

## REPL Commands

`/help` lists the commands under four headings. `/` in the input opens the
same list as a menu, filtered as you type, and a command's arguments are
offered after its name.

Session

- `/new`: start a new session without leaving the REPL.
- `/resume`: open the session picker and go on in the session you choose. Neither works while a request runs in the foreground.
- `/compact`: have the agent summarize the session so far. Later requests start from the summary.
- `/usage`: the tokens the session used, one row per agent with its model, runs, input, output and cache tokens. On a host the companion's row comes first and each agent it ran (`↳`) follows, with the runs it made in sessions of their own; a totals row closes the table.
- `/copy`: copy the last answer to the system clipboard. `/copy code` copies its last code block. Where there is no system clipboard, Ctrl-Y pastes the text into the input.

Requests

- `/bg <message>`: submit the message with `mode: bg` and return to the prompt.
- `/bg list`: list the session's `bg` requests with their short id (the first 8 characters of the request id), message, status and `started_by`. A `RunSubAgents` child runs in a session of its own, so it is not listed here; `/bg kill` still cancels it with its parent.
- `/bg switch <id>`: switch a request to `fg`, replay its events from the start, then follow it live and answer its interactions. Switching to a child shows its events and leaves it `bg`.
- `/bg kill <id>`: cancel a request and its children.
- `/subagent`: list the children of the last request that started any, with each one's agent, status, tools, tokens, time and what it was asked. `↺` marks a child that continued an earlier session.
- `/subagent <n>`: show child `n`: what it was asked, then its model steps, tool calls and own answer, read again from its stream. A child still running is followed to its end; Ctrl-C stops reading.
- `/subagent cancel <n>`: cancel child `n`. The other children go on, and the companion sees the cancelled one's status in its result.
- `/retry`: run the last request again as a new request and follow it.
- `/trace [N]`: show trace analysis for the N most recent traces (default 1). Renders timing breakdown, tool stats, and slowest operations for each trace.

Deployment

- `/agents`: the agents the REPL's requests can reach, each with its display name, description, capabilities and usage guidance. On a host these are the host's agents, which the companion routes to; with `--agent` it is that agent.
- `/tools`: the tools and skills of those agents.
- `/workflow` or `/workflow list`: list registered workflows (host-attached only when connected through a host).
- `/workflow run <workflow_id> [dedup_key] [--no-follow] [--input JSON_OBJECT]`: start a workflow run for the REPL's user and follow its steps. `--no-follow` prints the run id and returns.
- `/workflow status <workflow_id> <run_id>`: show workflow run status.
- `/memory`: the user's Cortex profile and entries. `/memory add <text>` pins a fact (`topic: fact` names its topic) and `/memory forget <id>` removes an entry. On a deployment without Cortex it says so.
- `/browser`: the sites the user's browser is signed in to. `/browser forget <site>` signs out of one.

REPL

- `/help`, `/keys`: the commands, and the keys.
- `/feedback <message>`: record a free-form note or bug report about the current session. The message is stored with the host, agent, session, and last request id so app developers can read it back later through the feedback API.
- `/clear`: clear the screen.
- `/exit`: leave the REPL.

`/bg switch` and `/bg kill` take any unique prefix of the request id. `list`
alone, and `switch` or `kill` followed by one id, are subcommands; any other
text after `/bg` is a message.
Ctrl-Z while a request runs switches it to `bg` and frees the foreground. The
REPL follows each running `bg` request you started and prints
`[a1b2c3d4] done` with the response, `failed: <error>`, `cancelled`, or
`needs your input: /bg switch a1b2c3d4`. It reads the session's `bg`
requests when it starts and after each foreground request, so a restarted
REPL picks up the ones still running. Children are not announced; their
results reach you through the request that started them.

A command registered by an app is listed under the `group` it names, or
under More.

### While a request runs

The input stays open while a request runs. `prompt_toolkit` draws everything
below the last finished line: the rows of the running request, the input, and
the status bar. Finished output is printed above them with `rich` and scrolls
like any other terminal output.

- The live rows show the running request: its phase or current tool, elapsed
  time and tokens. Under a `RunSubAgents` call there is one row per child, all
  followed at the same time. A `RunWorkflow` call's row adds the step its run
  is on, and the detail level prints each step as it ends. Queued messages
  are listed below.
- Each finished tool call prints one line, such as `✓ search(q=x) 2.5s`. A
  `RunSubAgents` call prints `Asking 2 agents: energy, review` and then one
  line per child as it ends, with its tool count, tokens and time.
- The answer prints as markdown, one finished block at a time while it
  streams, and once in total. The block still being written is drawn above
  the live rows as it will print, and moves into the scrollback in place
  when it is done.
- A message is printed under the `›` mark when it is sent: a queued message
  when its turn comes.
- A message sent while a request runs is queued and sent when that request
  ends. Up on an empty input takes the last queued message back for editing.
- Ctrl-C clears the input when it has text. With an empty input it cancels
  the running request. At an idle prompt, a second Ctrl-C within two seconds
  leaves the REPL, as does Ctrl-D. Esc also cancels the running request.
- Ctrl-O switches between the default level and the detail level. The detail
  level adds a line per model step (action, tokens, time), each child's own
  tool calls prefixed with its agent id, and a totals line when a request
  ends.
- Ctrl-L clears the screen.
- The status bar shows the target, the number of `bg` requests being watched,
  the session's tokens (its subagent runs included), and the keys that apply.

When stdin is a pipe or a file, the REPL reads one message or command per
line and runs each to its end before reading the next. Nothing is live in
that mode, and a request that asks an interaction is cancelled, since nobody
is there to answer.

Leaving the REPL while a request runs does not cancel it. The REPL prints
`[a1b2c3d4] is still running on the host.`

When a request waits on an interaction, the REPL asks for the answer in place
and puts the draft you were typing back afterwards. The question is printed,
then a menu opens under it (`ask.py`):

- An approval lists Approve, Deny and Skip. The tool call it is about is
  shown under the question, and `View the full call` prints all of its
  arguments.
- A choice lists the options as checkboxes. Space checks options and Enter
  answers with the checked ones, or with the highlighted one when none is
  checked.
- A `browser` handoff prints the live-view link, opens it in your browser,
  and lists Done and Cancel.
- Any other question takes a line of text.

Up, Down, a number or an option's first letter move through a menu, and Enter
answers. Ctrl-C at a question cancels the request that asked. Ctrl-D leaves
the REPL. A child's interaction is asked through the companion's request with
the child's agent id in front. When several are waiting, the question says
how many more there are. A question whose interaction is answered somewhere
else, times out, or belongs to a request that ended closes by itself.

### Input

The input line is a `prompt_toolkit` session (`input.py`). Its editing comes
from the library:

- Enter sends the message. Alt+Enter, Ctrl-J, or `\` then Enter add a line.
- Up and Down move between the lines of a message. On the first or last line
  they step through history, and the unsent draft comes back on the way down.
- Shift+arrows, Shift+Home and Shift+End select. Typing replaces the
  selection and Backspace deletes it.
- Ctrl-W cuts the previous word or the selection, Ctrl-K and Ctrl-U cut to the
  end and start of the line, and Ctrl-Y pastes. Cut text also goes to the
  system clipboard through `pyperclip`; where it finds no clipboard mechanism
  the cut text stays available inside the input.
- Ctrl-_ undoes. Ctrl-X Ctrl-E opens the draft in `$EDITOR`.
- A paste never sends the message. A paste of more than 20 lines shows as
  `[pasted 84 lines #1]` and is sent in full.
- `/` opens the command menu with each command's description, filtered as you
  type.
- History is kept per app in `~/.mash/history/`.

Shift+Enter is the same key as Enter in most terminals, so it sends. A mouse
selection belongs to the terminal: it copies output, and Delete does not erase
it in the input. Shift+Up/Down and Option-as-Meta depend on terminal settings.

Each `/command` invocation is wrapped in `command.start` and
`command.complete`/`command.error` events. The REPL ships them
best-effort to the deployment (`POST /telemetry/command-events`), where they land
in the agent's runtime event log and surface in the admin UI's CLI logs. Shipping
runs on a background thread and never blocks or breaks the session if the host is
slow or unreachable.

## Main Components
- `app.py`: `App`, the top-level parser, command dispatch, and command execution. `__main__.py` runs it with `python -m mash.cli`.
- `oneshot.py`: `mash run`: one request, its answer on stdout, an exit status.
- `client.py`: HTTP client used to talk to hosted Mash agents.
- `session.py`: the REPL loop. It owns the input, follows the foreground request and its children as asyncio tasks, queues messages, and asks interactions.
- `views.py`: the state of one request and `reduce`, which applies a stream event to it and returns the output that event finished. It prints nothing.
- `render.py`: the theme, the lines printed for finished output, and the live rows and status bar.
- `ask.py`: the menu that answers an interaction and picks a session.
- `sessions.py`: the session picker's rows.
- `input.py`: the input line: the `prompt_toolkit` session, the Enter and paste bindings, the `/` menu, and the clipboard.
- `commands.py` and `slash/`: command context, registration, and built-in commands grouped by purpose. A handler may be a plain function, which runs on a thread, or a coroutine function.
- `background.py`: `/bg` parsing, id prefixes, the `list` tree, and the tasks that follow `bg` requests.
- `config.py`: saved connection config.

## Public Exports
- `App`
- `MashHostClient`, `MashHostClientError`
- `Repl`, `Target`
- `CLIContext`
- `Command`, `CommandRegistry`

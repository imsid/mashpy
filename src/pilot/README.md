# Mash Pilot

Pilot is a command-line guide to the Mash codebase, built on the Mash SDK and
shipped in this repo at [`src/pilot/`](.). Its agents specialize in Mash's own
modules — so instead of reading docs or grepping the source, you ask Pilot and
it answers from the actual source tree.

```text
> Summarize how HostBuilder registers pooled agents and host compositions.
> Trace how an accepted request moves through AgentRuntime and RequestEngine.
> When is request.waiting emitted, and what does it mean for a busy session?
```

## Models and keys

The primary `pilot` agent runs **Gemini** (`gemini-3.5-flash`), read from
`GEMINI_API_KEY`. Every subagent runs an **open Gemma model over OpenRouter**
(`google/gemma-4-31b-it`), read from `OPENROUTER_API_KEY`. Both keys are
required. Each model is overridable:

| Variable | Purpose | Default |
|---|---|---|
| `GEMINI_API_KEY` | Key for the primary agent (required) | — |
| `OPENROUTER_API_KEY` | Key for the subagents (required) | — |
| `PILOT_PRIMARY_MODEL` | Override the primary model | `gemini-3.5-flash` |
| `PILOT_SUBAGENT_MODEL` | Override the subagent model | `google/gemma-4-31b-it` |
| `GITHUB_MCP_PAT` | Enable the guide's commit-inspection tools | unset |

## Quick start

```bash
# 1. Start the host — one container, embedded Postgres, Mash source included
docker run -d --name pilot -p 8000:8000 \
  -e GEMINI_API_KEY=... \
  -e OPENROUTER_API_KEY=sk-or-... \
  -v pilot-data:/var/lib/pilot \
  ghcr.io/imsid/mashpy-pilot:latest

# 2. Install the CLI and ask
curl -fsSL https://raw.githubusercontent.com/imsid/mashpy/main/install.sh | sh
pilot repl --host guide
```

Add `-e GITHUB_MCP_PAT=ghp_...` to enable the guide's commit-inspection tools.
The `pilot-data` volume keeps the database durable across restarts.

## The guide team

One agent per module:

| Agent | Owns |
|-------|------|
| `pilot` | Shared/cross-cutting: `core`, `tools`, `skills`, `logging`, `memory` |
| `cli-copilot` | `src/mash/cli` — commands, REPL, terminal rendering |
| `api-copilot` | `src/mash/api` — HTTP routes, FastAPI |
| `mcp-copilot` | `src/mash/mcp` — MCP client/server, transport, tool adaptation |
| `runtime-copilot` | `src/mash/runtime` — request lifecycle, event sourcing, durability |
| `workflow-copilot` | `src/mash/workflows` — step pipelines, DBOS orchestration, resume, run status |

## A workflow it ships

Beyond answering questions, Pilot registers the `pilot-changelog` workflow — a
`CodeStep` reads `CHANGELOG.md` and an `AgentStep` summarizes the most recent
release, scanning the source when an entry is too terse to explain on its own.
It's attached to the `guide` host, so `/workflow run pilot-changelog` runs it
from the REPL. See [`catalog/workflows/`](catalog/workflows/) for the spec.

## Scaffolding your own app

The guide carries `build-mash-agent`, `build-mash-workflow`, and
`build-mash-host` skills so it goes beyond explaining Mash to scaffolding your
application:

```text
> Build me a support agent with a knowledge base search tool and human approval for refunds.
> Scaffold a multi-agent code reviewer with separate agents for security, style, and correctness.
> I need an agent that connects to my MCP server at localhost:3000 and uses Gemini.
```

Use `pilot serve` from a source install to run your own host, or point the CLI
at any Mash deployment with `--api-base-url`. Treat this package as a reference
when structuring your own multi-agent app.

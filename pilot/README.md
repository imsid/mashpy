# Pilot README

`pilot/` defines the Mash Pilot host: one primary codebase guide plus focused module copilots.

## Agent Layout

- `pilot`: primary guide for shared and cross-cutting codebase questions
- `cli-copilot`: specialist for `src/mash/cli`
- `api-copilot`: specialist for `src/mash/api`
- `mcp-copilot`: specialist for `src/mash/mcp`
- `runtime-copilot`: specialist for `src/mash/runtime`
- `workflow-copilot`: specialist for `src/mash/workflows`
- `masher-trace-digest`: built-in workflow for trace digest generation
- `masher-online-eval-curation`: built-in workflow to convert a trace to an online eval

## Prompt Scope

- The primary pilot owns shared and core questions for `src/mash/core`, `src/mash/tools`, `src/mash/skills`, `src/mash/logging`, `src/mash/memory`, and other cross-cutting behavior.
- Runtime-centered questions are delegated to `runtime-copilot`.
- Workflow-centered questions are delegated to `workflow-copilot`.
- Each copilot uses cached `README.md` and `AGENTS.md` files for its module before falling back to targeted `bash` verification.

## Host Composition

`pilot/spec.py` builds the host with one primary agent and the five module copilots above, then enables Masher's workflow-only trace digest worker.

## Pilot REPL

Start the Pilot host with mash
`mash host serve --host-app pilot.spec:build_host --host 127.0.0.1 --port 8001`. Connect to it with
the Pilot remote shell to get Pilot-only slash commands:

```bash
pilot repl
```

Pilot registers `/changelog [N]` on top of the standard Mash REPL commands. On
first use, the command dynamically publishes a changelog skill and workflow
against the primary `pilot` agent, then runs it with `commit_count=N` (default
`5`).

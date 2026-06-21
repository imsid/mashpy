# Contributing to Mash

Thanks for your interest in improving Mash. This guide covers how to report
issues, set up a dev environment, and open a pull request.

## Reporting Issues

- **Bugs and feature requests:** open an issue using the
  [templates](https://github.com/imsid/mashpy/issues/new/choose).
- **Questions and ideas:** use
  [Discussions](https://github.com/imsid/mashpy/discussions).
- **Security vulnerabilities:** do *not* open a public issue — follow
  [SECURITY.md](SECURITY.md) to report privately.

## Pull Requests

1. Fork the repo and create a branch off `main`.
2. Make your change with tests, and keep the PR focused on one thing.
3. Run the test suite (`uv run --extra dev pytest -q tests/mash`) and make sure
   it passes.
4. Update docs / module READMEs if your change affects behavior. **Do not edit
   `CHANGELOG.md`** — it is generated automatically from commit messages.
5. Title the PR as a [Conventional Commit](#commit-conventions) (a CI check
   enforces this) and fill out the template. CI (lint, tests on Python
   3.10–3.12, build) must pass before review.

PRs are **squash-merged**, and the PR title becomes the commit on `main` — so the
title is what drives versioning and the changelog. A maintainer (see
[CODEOWNERS](.github/CODEOWNERS)) is requested for review automatically.

## Commit Conventions

The PR title must follow the
[Conventional Commits](https://www.conventionalcommits.org/) format:

```
<type>(<optional scope>): <description>
```

Common types and their effect on the next release:

| Type | Use for | Version bump |
|---|---|---|
| `feat` | A new feature | minor |
| `fix` | A bug fix | patch |
| `perf` | A performance improvement | patch |
| `docs`, `refactor`, `test`, `build`, `ci`, `chore` | Everything else | none |

A **breaking change** is marked with a `!` (`feat!: drop legacy host API`) or a
`BREAKING CHANGE:` footer. While Mash is pre-1.0 these bump the minor version;
after 1.0.0 they bump the major.

Examples:

```
feat(runtime): add structured output to workflows
fix(cli): handle missing config file gracefully
docs: clarify provider setup in README
feat!: rename AgentSpec.build_llm to build_provider
```

## Releases

Releases are automated by
[release-please](https://github.com/googleapis/release-please). As Conventional
Commits land on `main`, it maintains an open "release PR" that bumps the version
in `pyproject.toml` and updates `CHANGELOG.md`. A maintainer merges that PR to
cut the release — release-please tags it and the package is published to PyPI
automatically. Contributors never bump versions or edit the changelog by hand.

## License of Contributions

Mash is licensed under the [Apache License 2.0](LICENSE). By submitting a
contribution, you agree that it is licensed under the same terms, per section 5
of that license.

## Development Setup

```bash
git clone https://github.com/imsid/mashpy.git
cd mashpy
uv venv
uv sync
source .venv/bin/activate
```

## Running Tests

```bash
uv run --extra dev pytest -q tests/mash
```

Test directories mirror the source layout:

| Change area | Test directory |
|---|---|
| Runtime | `tests/mash/runtime` |
| API | `tests/mash/api` |
| CLI / REPL | `tests/mash/cli` |
| Workflows | `tests/mash/workflows` |

Before submitting broad changes, run the full suite:

```bash
uv run --extra dev pytest -q tests/mash
```

## Admin UI Development

The admin dashboard is a React + Vite app in `src/mash/api/web-admin`. The host
serves a pre-built bundle from `src/mash/api/static/admin`, so changes to the UI
need a rebuild before they show up on the `/admin` route. The Makefile wraps the
workflow:

```bash
make admin-web-install        # once after cloning, or when frontend deps change
make admin-web                # Vite dev server (proxies /api to 127.0.0.1:8000)
make admin-web-build          # production bundle into web-admin/dist
make admin-web-package-sync   # build, then sync dist/ into static/admin/
```

Iterate against the dev server (`make admin-web`) at http://localhost:5174/admin,
pointing it at a running host on port 8000. When you're done, run
`make admin-web-package-sync` and commit the regenerated `static/admin/` bundle
along with your source changes — the host reads those files from disk, so a
refresh of `/admin` picks them up without a code change.

`package-lock.json` is intentionally gitignored; dependencies are tracked in
`web-admin/package.json`, and `make admin-web-install` (i.e. `npm install`)
resolves them locally.

## Repo Structure

```text
src/mash/          Mash package: SDK, runtime, API, CLI, workflows
tests/             Test suites (mirrors src/ layout)
docs/              Product brief, deployment guide, RFCs
Dockerfile         Base image for Mash host deployments
```

## Subsystem Documentation

Use these module READMEs as the source of truth when changing a subsystem:

- [Package overview](src/mash/README.md) — top-level boundaries
- [Runtime](src/mash/runtime/README.md) — host composition, request execution,
  persistence, structured output
- [Workflows](src/mash/workflows/README.md) — code-defined workflows, dynamic
  publishing, task state, DBOS orchestration
- [Skills](src/mash/skills/README.md) — filesystem and inline skills, dynamic
  registration
- [API](src/mash/api/README.md) — HTTP surface, request/response shapes,
  telemetry endpoints
- [CLI](src/mash/cli/README.md) — `mash` commands and REPL slash commands
- [LLM providers](src/mash/core/llm/README.md) — provider adapters, normalized
  contracts, provider-native structured output
- [Masher](src/mash/agents/masher/README.md) — built-in trace digest and online
  eval worker
- [Core](src/mash/core/README.md)
- [Tools](src/mash/tools/README.md)
- [Agents](src/mash/agents/README.md)
- [Memory](src/mash/memory/README.md)
- [MCP](src/mash/mcp/README.md)
- [Logging](src/mash/logging/README.md)

## Request Flow

A Mash request flows through the host API, into an agent runtime, through the
durable request engine, and back out as replayable runtime events:

```mermaid
sequenceDiagram
    participant User
    participant API as mash.api
    participant Pool as AgentPool
    participant Client as AgentClient
    participant Server as AgentServer
    participant Runtime as AgentRuntime
    participant Events as RuntimeStore
    participant Engine as RequestEngine
    participant Agent as mash.core.Agent
    participant Memory as Memory Store

    User->>API: POST /api/v1/agents/{agent_id}/requests
    API->>Pool: get_client(agent_id)
    Pool-->>API: AgentClient
    API->>Client: post_request(...) / stream(...)
    Client->>Server: HTTP request + SSE stream
    Server->>Runtime: submit_request(...) / stream_request_events(...)
    Runtime->>Events: append request.accepted
    Runtime->>Engine: start_request(...)
    Engine->>Agent: think (plan step)
    Agent-->>Engine: action + tool calls

    opt interaction needed (approval or AskUser)
        Engine->>Events: append request.interaction.create
        Events-->>Server: SSE request.interaction.create
        Server-->>Client: SSE request.interaction.create
        Client-->>API: interaction event
        API-->>User: prompt for input
        User->>API: POST .../interaction {interaction_id, response}
        API->>Client: post_interaction(...)
        Client->>Server: POST .../interaction
        Server->>Engine: DBOS.send (resume workflow)
        Engine->>Events: append request.interaction.ack
    end

    Engine->>Agent: act (execute tools) -> observe
    Agent-->>Engine: response + trace + token usage
    Engine->>Memory: save_turn(...)
    Engine->>Events: append request.started / agent.trace / request.completed
    Runtime-->>Server: replay persisted events
    Server-->>Client: SSE runtime events
    Client-->>API: streamed runtime events
    API-->>User: SSE / final payload
```

## RFCs

- [Host-to-Agent Protocol (H2A)](docs/rfcs/host-to-agent-protocol.md)

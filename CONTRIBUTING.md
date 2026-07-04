# Contributing to Mash

Thanks for your interest in improving Mash. This guide covers how to report
issues, set up a dev environment, and open a pull request. Please read our
[Code of Conduct](CODE_OF_CONDUCT.md) before participating.

## Reporting Issues

- **Bugs and feature requests:** open an issue using the
  [templates](https://github.com/imsid/mashpy/issues/new/choose).
- **Questions and ideas:** use
  [Discussions](https://github.com/imsid/mashpy/discussions).
- **Security vulnerabilities:** do *not* open a public issue — follow
  [SECURITY.md](SECURITY.md) to report privately.

## Development Flow (Claude Code)

If you use Claude Code, the `/triage` skill covers the full development flow —
from an informal bug report or feature request through a filed issue and open PR.
Invoke it with `/triage` and describe the problem; the skill guides the rest.

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

The same `v*` tag also triggers the Pilot release workflows: standalone CLI
binaries for macOS and Linux are uploaded to the GitHub Release, and a
multi-arch Docker image is pushed to GHCR. See [Pilot release process](#pilot-release-process)
for details.

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
| Pilot | `tests/pilot` |

Before submitting broad changes, run the full suite:

```bash
uv run --extra dev pytest -q tests/mash
uv run --extra dev pytest -q tests/pilot
```

## Pilot

Pilot lives in `src/pilot/` and is the primary dogfood for the SDK. Every mash
change runs against the pilot test suite in CI, and you can run it locally
during development to see how SDK changes affect a real multi-agent host.

### Running Pilot locally

**Prerequisites:** Docker, an Anthropic or OpenAI API key.

Create `.env` in the repo root with your API key:

```
ANTHROPIC_API_KEY=sk-...
```

Then bring everything up:

```bash
docker compose -f docker-compose.pilot.yml up -d
```

This starts Postgres and the Pilot host together. Once healthy:

```bash
pilot browse               # list the pool and configured hosts
pilot repl --host guide    # enter the default multi-agent composition
```

`pilot` defaults to `http://127.0.0.1:8000`, so no extra flags are needed.
The admin UI is available at http://localhost:8000/admin.
Optionally add `GITHUB_MCP_PAT` (a GitHub personal access token with `repo`
scope) to `.env` to enable the guide's commit-inspection tools.

The repo root is bind-mounted into the container and installed editable
(`pip install -e .`), so the host imports your live working tree — no commit
needed. After editing anything under `src/`, restart the host to reload it:

```bash
docker compose -f docker-compose.pilot.yml restart pilot
```

Rebuild only when dependencies change:

```bash
docker compose -f docker-compose.pilot.yml build pilot
```

### Dogfooding while working on the SDK

When you change something in `src/mash/`, run the pilot tests first to catch
regressions in a real application before the mash suite:

```bash
uv run --extra dev pytest -q tests/pilot
```

You can also start the pilot host and talk to it directly. Because the repo is
mounted and installed editable, the host runs your live working tree — restart
it (`docker compose -f docker-compose.pilot.yml restart pilot`) after a change
to get immediate feedback on whether the agents still behave correctly.

### Adding an agent to the Pilot catalog

Agents live under `src/pilot/catalog/agents/<name>/`. Each package needs a
`spec.py` and an `__init__.py`. Follow these steps:

1. **Create the package.** The `cli` copilot is the smallest complete example.
   Implement the standard `AgentSpec` methods (`get_agent_id`, `build_tools`,
   `build_skills`, `build_llm`, `build_agent_config`).

2. **Write the metadata carefully.** `AgentMetadata.usage_guidance` is what
   the primary reads when routing — vague guidance produces vague delegation.

3. **Register it.** Add a `CatalogEntry` to `CATALOG` in
   `src/pilot/catalog/__init__.py`.

4. **Export from `src/pilot/spec.py`.** Add a re-export for the new spec class
   (import from `catalog.agents.<name>.spec` and add to `__all__`) so test
   files that import from `pilot.spec` can find it.

5. **Degrade gracefully.** Gate optional capabilities (MCP servers,
   credentials) inside the spec method; always register the agent regardless of
   configuration.

6. **Add skills as package data.** Drop markdown files in `src/pilot/skills/`
   and add a glob to `[tool.setuptools.package-data]` in `pyproject.toml` so
   they're included in the wheel and Docker image.

### Pilot release process

Pilot artifacts are built from the **same `v*` tag** that release-please
creates for the mashpy package — no separate tag or version bump required.

Two workflows fire on every `v*` push:

| Workflow | Output |
|----------|--------|
| `release-pilot.yml` | `pilot` standalone binaries for macOS arm64 and Linux x86_64, uploaded to the GitHub Release |
| `docker-pilot.yml` | Multi-arch image (`linux/amd64`, `linux/arm64`) pushed to `ghcr.io/imsid/mashpy-pilot:latest` and `:<version>` |

The `install.sh` in the repo root always fetches the latest binary release
from `imsid/mashpy`. Users install with:

```bash
curl -fsSL https://raw.githubusercontent.com/imsid/mashpy/main/install.sh | sh
```

**First-time setup:** after the first push, set the `mashpy-pilot` GHCR
package to public in **repo Settings → Packages** so `docker pull` works
without authentication.

**Re-tagging a release** (e.g., after fixing the build config):

```bash
git tag -d v0.x.0
git push origin :refs/tags/v0.x.0
gh release delete v0.x.0 --yes
git tag v0.x.0
git push origin v0.x.0
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
src/pilot/         Pilot multi-agent app: catalog, CLI, specs, skills
tests/mash/        Mash test suite (mirrors src/mash/ layout)
tests/pilot/       Pilot test suite
docs/              Product brief, deployment guide, RFCs
Dockerfile         Base image for Mash host deployments
Dockerfile.pilot   Image for the Pilot host
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

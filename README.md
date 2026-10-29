# Mash

[![CI](https://github.com/imsid/mashpy/actions/workflows/ci.yml/badge.svg)](https://github.com/imsid/mashpy/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mashpy.svg)](https://pypi.org/project/mashpy/)
[![Python versions](https://img.shields.io/pypi/pyversions/mashpy.svg)](https://pypi.org/project/mashpy/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Mash is a self-hosted durable runtime for code-authored automations using workflows with agents. A workflow is an
ordered pipeline of typed steps, a step is deterministic Python or one run of a
harnessed agent, and control flow stays in code.

Mash gives you a Python `AgentSpec` contract for defining agents, a
`WorkflowSpec` for authoring step pipelines, a `HostBuilder` that composes both
into a deployable pool, a FastAPI server for deployment, and a CLI/API for
interacting with a running host.

It's designed around [Host-to-Agent Protocol (H2A)](rfcs/host-to-agent-protocol.md) that 
standardizes interactions between user applications and agents. 

## What Mash Provides

- **Workflows**: ordered pipelines of typed steps, durable and observable.
  A step is deterministic Python or one agent run; each step's output threads
  into the next step's input with schema checks at every edge. The pipeline is
  code you can read, diff, test, and replay.
- **Agent harness**: the agent loop runs inside a durable request engine with
  tools, skills, memory, and structured output. Requests are recorded as
  replayable runtime events; retries, restarts, and long-running work just
  work.
- **Sandboxes**: every request acts for a user, and each user gets a sandbox,
  a container where the code the model writes (Python, JavaScript, shell) runs
  as an unprivileged user. It holds a folder per agent, whose files and SQLite
  databases persist across requests, and only that user's files: the agents'
  secrets and the database stay with the host. Pages, files and servers the
  code makes open at links the host serves.
- **Browsers**: each user also gets a browser, a Chromium in its own container
  whose profile keeps the sites the user signed in to. An agent that lists
  sites drives it with Playwright MCP's page tools, on those sites only. When a
  site needs a sign-in, the agent hands the browser to the user through a
  live-view link, and the profile keeps the session for later requests. Mash
  never sees the password, and code in the sandbox has no route to the browser.
- **Frontier and open-source models**: built-in adapters for Anthropic, OpenAI,
  and Gemini, and any open-source model served over a Chat Completions endpoint,
  self-hosted with vLLM or Ollama or hosted on OpenRouter. Each agent picks its
  model in one line of `build_llm()`.
- **Self-hosted interfaces**: HTTP API with streaming, CLI, and interactive REPL,
  all on one Postgres. Deploy locally, in Docker, or on any cloud.
- **Multi-agent composition**: define a primary agent, add specialized subagents,
  and compose workflows behind a single host. Agents delegate to each other
  without a separate coordination layer.
- **Human-in-the-loop**: agents can pause for approval or ask users questions
  mid-execution. Interactions survive host restarts.
- **Observability**: span trees, trace analysis, telemetry API, built-in
  dashboard, and CLI trace inspection. No external APM needed.
- **Synthetic evals**: generate a test dataset and scoring rubric from a
  host's declared capabilities, run experiments that snapshot the live host,
  and compare quality and cost across runs before the first user message.
  Datasets, rubrics, and experiment results live in the built-in dashboard.
  Turn them on with `HostBuilder.enable_evals()`.

```
                  ┌─────────────────────────────────────────┐
                  │          Durable Request                │
                  │                                         │
                  │   ┌─ context ─── memory ──┐             │
                  │   │                       │             │
request ────────► │   │     Agent Loop        │ ──► signals │
(cli/api)         │   │ think → act → observe │      │      │
                  │   │                       │      ▼      │
                  │   └─ tools ───── skills ──┘  structured │
workflow step ──► │      │ ▲                      output    │
(api/cli)         │      │ │ user interaction               │
                  │      │ ▼ (approval / ask-user /         │
                  │      │    browser sign-in)              │
                  │      │                                  │
                  │      │  resumable · replayable          │
                  └──────┼──────────────────────────────────┘
                         ├────────────────────────────┐
                         │ run_code                   │ browser_*
                         ▼                            ▼
                  ┌──────────────────────────┐ ┌──────────────────────────┐
                  │  Sandbox: one per user   │ │  Browser: one per user   │
                  │  workspaces/<agent_id>   │ │  Chromium with a profile │
                  │  files persist           │ │  listed sites only       │
                  │  no secrets · no db      │ │  sign-in via live view   │
                  └──────────────────────────┘ └──────────────────────────┘
```

A request from a user and a step in a workflow ride the same durable loop, and
the code either one runs lands in the sandbox of the user it acts for. An agent
that lists sites drives that user's browser.

See [Mash under the hood](docs/posts/mash-under-the-hood.md) for a deeper look
at each capability, and the [product brief](docs/posts/product-brief.md) for
the pitch.

## Quick Start

**Install:**

```bash
# install the library
uv add mashpy

# install the `mash` CLI on your PATH
uv tool install mashpy 
```

**Define your agents:**

Each agent is an `AgentSpec` subclass. It names itself, picks an LLM, and
declares a system prompt, tools, skills and agent config.

```python
## my_app/agents.py

from mash import AgentSpec
from mash.core.config import AgentConfig
from mash.core.llm import AnthropicProvider
from mash.skills import SkillRegistry
from mash.tools.registry import ToolRegistry


class ConciergeAgent(AgentSpec):
    def get_agent_id(self):
        return "concierge"

    def build_tools(self):
        return ToolRegistry()

    def build_skills(self):
        return SkillRegistry()

    def build_llm(self):
        return AnthropicProvider(app_id="concierge")

    def build_agent_config(self):
        return AgentConfig(
            app_id="concierge",
            system_prompt=(
                "You are the concierge. Answer the user directly, and "
                "delegate research-heavy questions to the research subagent."
            ),
        )


class ResearchAgent(AgentSpec):
    def get_agent_id(self):
        return "research"

    def build_tools(self):
        return ToolRegistry()

    def build_skills(self):
        return SkillRegistry()

    def build_llm(self):
        return AnthropicProvider(app_id="research")

    def build_agent_config(self):
        return AgentConfig(
            app_id="research",
            system_prompt="You handle research-heavy questions in depth.",
        )
```

**Author the workflow:**

The workflow is the automation: an ordered pipeline of typed steps that code
owns end to end. Use a `CodeStep` for deterministic Python and an `AgentStep`
when the work needs an agent.

```python
## my_app/workflows.py

from pydantic import BaseModel

from mash import AgentStep, CodeStep, StepContext, WorkflowSpec


class ResearchRequest(BaseModel):
    topic: str


class ResearchPlan(BaseModel):
    topic: str
    questions: list[str]


class ResearchBrief(BaseModel):
    summary: str
    sources: list[str]


def plan_research(
    request: ResearchRequest,
    _context: StepContext,
) -> ResearchPlan:
    return ResearchPlan(
        topic=request.topic,
        questions=[
            f"What are the key facts about {request.topic}?",
            f"What should a reader understand about {request.topic}?",
        ],
    )


RESEARCH_BRIEF = WorkflowSpec(
    workflow_id="research-brief",
    input_model=ResearchRequest,
    steps=[
        CodeStep(
            step_id="plan",
            run=plan_research,
            input=ResearchRequest,
            output=ResearchPlan,
        ),
        AgentStep(
            step_id="research",
            agent_id="research",
            input=ResearchPlan,
            output=ResearchBrief,
        ),
    ],
)
```

The `CodeStep` output becomes the `AgentStep` input. Mash validates both edges,
runs each step durably, and uses the last step's output as the workflow result.

**Build the pool:**

The pool is the unit of deploy: agents and workflows registered together.

```python
## my_app/host.py

from mash import AgentMetadata, HostBuilder

from .agents import ConciergeAgent, ResearchAgent
from .workflows import RESEARCH_BRIEF


def build_pool():
    pool = (
        HostBuilder()
        .agent(
            ConciergeAgent(),
            metadata=AgentMetadata(
                display_name="Concierge",
                description="Front-door agent that answers users and delegates.",
                capabilities=["conversation", "delegation"],
                usage_guidance="Default entry point for user requests.",
            ),
        )
        .agent(
            ResearchAgent(),
            metadata=AgentMetadata(
                display_name="Research",
                description="Handles research-heavy questions in depth.",
                capabilities=["research", "analysis"],
                usage_guidance="Use for questions that need digging.",
            ),
        )
        .workflow(RESEARCH_BRIEF)
        .build()
    )
    return pool
```

A pool can also be workflows alone. A workflow of pure `CodeStep`s references
no agents, and the resulting pool serves only workflow runs:

```python
def build_pool():
    # EXPORT_METRICS is a WorkflowSpec of pure CodeSteps; no agents needed.
    return HostBuilder().workflow(EXPORT_METRICS).build()
```

**Configure the environment:**

The host needs an LLM key, a Postgres URL for its durable runtime, and a
secret it derives agent and sandbox tokens from. Put them in a `.env` file the
host loads on start:

```bash
# .env
ANTHROPIC_API_KEY=sk-ant-...
MASH_DATABASE_URL=postgresql://user:pass@localhost:5432/mash
MASH_HOST_SECRET=...   # openssl rand -hex 32
```

**Start the host:**

The host runs each agent as its own subprocess. For agents to get the sandbox
tools, it also needs a Docker daemon; it pulls Mash's sandbox image,
`ghcr.io/imsid/mash-sandbox`, on first start. Without Docker the host still
runs, and its agents get no sandbox tools. Agents whose specs list sites with
`browser_sites()` also get the browser tools, and the host pulls Mash's browser
image, `ghcr.io/imsid/mash-browser`, on first start too.

```bash
mash host serve --host-app my_app.host:build_pool --host 127.0.0.1 --port 8000
```

**Connect as a user and browse available agents.** Every request acts for a
user, so the connection saves one:
```bash
mash connect --api-base-url http://127.0.0.1:8000 --user-id u-1
mash browse
```

**Compose an assistant host with primary and subagents:**
```bash
mash compose --host assistant --primary concierge --subagents research \
  --workflows research-brief
```

**Talk to the host or execute `/` commands using Mash repl:**
```bash
mash repl --host assistant
```

## Key Concepts

| Concept | What it is |
|---|---|
| **AgentSpec** | Abstract contract defining one agent (id, tools, skills, LLM, config) |
| **HostBuilder** | Fluent builder that composes agents, workflows, and hosts into a Pool |
| **Pool** | The deployed pool of role-less agents the API server runs; each agent runs as a subprocess of the host |
| **User** | The end user a request acts for, named by a required `user_id`; it scopes the request's conversations, memory, sandbox and browser |
| **Sandbox** | A container per user where agents' code runs, holding that user's workspaces and nothing else |
| **Browser** | A Chromium per user, in its own container, whose profile keeps the sites the user signed in to; agents that list sites with `browser_sites()` drive it on those sites |
| **Host** | A composition over the pool (primary + subagents + workflows), defined in code or dynamically over the API |
| **ToolRegistry** | Register callable tools; built-ins include AskUser and InvokeSubagent, and the host adds `run_code` and `edit_file` when it has sandboxes, `sandbox_put` and `sandbox_get` for specs that declare file roots, and the browser tools for specs that list sites |
| **SkillRegistry** | Markdown instruction bundles loaded on demand via a meta-tool |
| **LLMProvider** | Adapters for Anthropic, OpenAI, and Gemini |
| **OSSCompatibleProvider** | Runs open-source models (Gemma, Qwen, DeepSeek) over any Chat Completions endpoint, self-hosted (vLLM, Ollama) or hosted (OpenRouter); chosen in `build_llm()` like any provider |
| **WorkflowSpec** | Ordered pipeline of typed steps (`CodeStep` / `AgentStep`); runs are durable and observable |
| **Eval / Experiment** | A generated dataset and rubric bound to a host; an experiment runs the dataset against the host, snapshots its composition, and scores results with an LLM judge |
| **Cortex** | Opt-in per-user memory kept by the host and shared by every agent; each request reads its user's, the `cortex-build` workflow builds it on demand, and apps edit it over the API |

## Mushy / Thoughts

See the agent's original exposed thought summaries in **Admin → Activity →
Thoughts**. Filter by Agent and scroll the latest 50 traces with nonempty
summaries. Underlined phrases bring out the language's personality; selecting
one moves Mushy beside the passage. Each trace links to its Admin logs.

Capture is always enabled for runtime agents when their provider supports it
(currently Gemini). The trace sidebar in Logs links directly to that trace's
thoughts, including older traces. There is no separate interpreter, GIF
generation API, or public sharing surface. See
[Mushy](src/mash/mushy/README.md) and [Pilot](src/pilot/README.md#try-mushy-with-pilot).

## Mash Pilot

Pilot is a command-line guide to the Mash codebase, built on the Mash SDK and
shipped in this repo at [`src/pilot/`](src/pilot/). Its `pilot` agent answers
questions from the source and searches the docs site and GitHub in the user's
browser, and its `builder` agent builds Mash apps in the user's sandbox. It also ships the `pilot-changelog` workflow. Run it from a checkout,
with `GEMINI_API_KEY`, `OPENROUTER_API_KEY` and `MASH_HOST_SECRET` in `.env`:

```bash
docker compose -f docker-compose.pilot.yml up -d --build

curl -fsSL https://raw.githubusercontent.com/imsid/mashpy/main/install.sh | sh
pilot repl --host guide
```

See [`src/pilot/README.md`](src/pilot/README.md) for the agents, the keys and
model overrides, the shipped workflow, and building your own app.

## Build with a Coding Agent

This repo includes [`CLAUDE.md`](CLAUDE.md) so coding agents like Claude Code,
Codex, and Cursor can scaffold a Mash-powered agent from a natural language
prompt. Copy it into your project or point your agent at this repo to get
started. The Pilot guide (above) also carries the `build-mash-agent`,
`build-mash-workflow`, and `build-mash-host` skills for interactive
scaffolding from the REPL.

## Documentation

- [Product brief](docs/posts/product-brief.md) — the pitch: why the application-to-agent seam needs a standard
- [Mash under the hood](docs/posts/mash-under-the-hood.md) — what Mash offers and where it fits
- [Synthetic evals](docs/posts/synthetic-evals.md) — datasets, rubrics, experiments, and scoring for the cold-start problem
- [Deployment guide](docs/posts/how-to-deploy.md): agent processes, sandboxes, browsers, Docker, the host on a server
- [Building agent CLIs](docs/posts/building-agent-clis.md) — custom CLI development
- [CLAUDE.md](CLAUDE.md) — full SDK reference for coding agents
- [Package overview](src/mash/README.md) — subsystem boundaries and module guides
- [Contributing](CONTRIBUTING.md) — development setup, tests, repo structure

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup,
tests, and the pull request flow, and [SECURITY.md](SECURITY.md) for reporting
vulnerabilities. Release notes live in [CHANGELOG.md](CHANGELOG.md).

## License

Mash is licensed under the [Apache License 2.0](LICENSE).

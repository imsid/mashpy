# Mash

[![CI](https://github.com/imsid/mashpy/actions/workflows/ci.yml/badge.svg)](https://github.com/imsid/mashpy/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mashpy.svg)](https://pypi.org/project/mashpy/)
[![Python versions](https://img.shields.io/pypi/pyversions/mashpy.svg)](https://pypi.org/project/mashpy/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

Mash is a self-hosted, durable runtime for building agentic applications
where users and agents work together. You write your agents and workflows in
Python, deploy them behind one API on your own Postgres, and connect them to
the places your users already are: a Slack workspace, your product, a
terminal.

## How it works

Your application sends Mash events, one for each thing a user says or does.
A session holds the context of one conversation, so every agent in it knows
what came before. An app holds memory across its sessions, so a new
conversation starts with what earlier ones learned. A policy controls what
agents can access, such as a sandbox to run code in and a browser to work on
websites.

Mash has no concept of a user. The application decides who takes part in a
session, whether one person or a whole team, and everyone in it shares the
same context.

## Example: a team bot in Slack

A team bot is a good fit. Each channel is an app, each thread is a session,
and each message is an event.

```
Slack                                            Mash
───────────────────────────────────────────────  ─────────────────────────────────
#growth                                          app: growth
│                                                (memory for the channel)
│
├─ Thread                                        session
│  │                                             policy: growth
│  │
│  ├─ Alice: @bot pull last week's churn         event → agents
│  ├─ bot:   Run this query on the replica?      approval from an agent
│  │         [Approve] [Deny]
│  ├─ Alice: Approve                             answer to the approval
│  ├─ bot:   Churn was 2.1%, up from 1.8%        response
│  ├─ Bob:   @bot break it down by plan          event in the same session,
│  │                                             with Alice's context
│  └─ bot:   Pro 1.2%, Team 3.4%                 response
│
└─ Thread                                        new session, same app
   ├─ Carol: @bot what drove the Team churn?     event; knows what the
   └─ bot:   ...                                 channel has looked at
```

Alice asks for churn numbers and the bot hands the question to the right
agents. One of them checks with her before it touches the database, and she
approves from the thread. Bob picks up where she left off without repeating
anything. A week later Carol opens a new thread, and the bot already knows
what the channel has been working on.

The [product brief](docs/posts/product-brief.md) has the full pitch.

## What Mash Provides

- **Events, sessions, apps and policies**: one endpoint for every input.
  A retried or redelivered event runs once, a session's events run in order,
  and the app follows each event's SSE stream or reads its state.
- **Agent harness**: the agent loop runs inside a durable request with tools,
  skills, memory and structured output. Requests are recorded as replayable
  runtime events, so restarts and long-running work resume mid-loop.
- **Companion routing**: every event to a host runs on the built-in
  companion, which routes it to the host's agents, in parallel when the work
  splits, or to a workflow, and writes the answer.
- **Human-in-the-loop**: agents pause for approvals, questions and browser
  sign-ins, and the pause survives a host restart. Interactions from the
  companion's subagents come back on the event.
- **Sandboxes**: each policy gets a container where the code the model writes
  (Python, JavaScript, shell) runs, in a folder per agent whose files and
  SQLite databases persist. Pages, files and servers the code makes open at
  links the host serves. The agents' secrets and the database stay with the
  host.
- **Browsers**: each policy gets a Chromium whose profile keeps the sites the
  user signed in to. An agent that lists sites drives it with Playwright MCP's
  page tools, on those sites only. Sign-ins happen through a live-view link,
  and Mash never sees the password.
- **Cortex**: opt-in memory per app, built from the app's sessions by the
  `cortex-build` workflow and shown to every request in the app.
- **Workflows**: ordered pipelines of typed steps, a `CodeStep` of Python or an
  `AgentStep` of one agent run, durable and resumable from the failed step.
- **Frontier and open-source models**: adapters for Anthropic, OpenAI and
  Gemini, and any open-source model served over a Chat Completions endpoint.
  Each agent picks its model in `build_llm()`.
- **API, CLI and REPL**: HTTP with SSE streaming, a `mash` CLI, and a REPL that
  shows the companion's subagents live. `mash.cli.App` ships it as your own
  client.
- **Traces**: every request as OpenTelemetry spans, read by the admin UI and
  `/trace`, and exported to any OTLP backend with `mashpy[otel]`.
- **Synthetic evals**: a generated dataset and rubric for a host, experiments
  that run it through the live host, and comparisons of quality and cost
  across runs. Turn them on with `HostBuilder.enable_evals()`.

See [Mash under the hood](docs/posts/mash-under-the-hood.md) for a deeper look
at each capability.

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


class SupportAgent(AgentSpec):
    def get_agent_id(self):
        return "support"

    def build_tools(self):
        return ToolRegistry()

    def build_skills(self):
        return SkillRegistry()

    def build_llm(self):
        return AnthropicProvider(agent_id="support")

    def build_agent_config(self):
        return AgentConfig(
            agent_id="support",
            system_prompt="You answer product and account questions.",
        )


class ResearchAgent(AgentSpec):
    def get_agent_id(self):
        return "research"

    def build_tools(self):
        return ToolRegistry()

    def build_skills(self):
        return SkillRegistry()

    def build_llm(self):
        return AnthropicProvider(agent_id="research")

    def build_agent_config(self):
        return AgentConfig(
            agent_id="research",
            system_prompt="You handle research-heavy questions in depth.",
        )
```

**Author a workflow (optional):**

A workflow is an ordered pipeline of typed steps, for work that should run as
code. Use a `CodeStep` for Python and an `AgentStep` when a step needs an
agent. The companion can start it when a message calls for it.

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

from .agents import ResearchAgent, SupportAgent
from .workflows import RESEARCH_BRIEF


def build_pool():
    pool = (
        HostBuilder()
        .agent(
            SupportAgent(),
            metadata=AgentMetadata(
                display_name="Support",
                description="Answers product and account questions.",
                capabilities=["product questions", "account help"],
                usage_guidance="Questions about the product or a user's account.",
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
secret it derives agent and sandbox tokens from. Put them in a `.env` file in
the directory you run `mash host serve` from; the CLI loads it on start:

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
image, `ghcr.io/imsid/mash-browser`, the first time one opens a page.

```bash
mash host serve --host-app my_app.host:build_pool --host 127.0.0.1 --port 8000
```

**Create a policy, connect, and browse available agents.** Every message is
an event that names the app posting it and the policy it runs under, so the
connection saves both:
```bash
echo '{"sandbox": true}' | mash policy put dev --file - \
  --api-base-url http://127.0.0.1:8000
mash connect --api-base-url http://127.0.0.1:8000 --app-id my-app --policy-id dev
mash browse
```

**Compose an assistant host from agents and a workflow.** Every event to it
runs on the built-in companion, which routes it:
```bash
mash compose --host assistant --agents support,research \
  --workflows research-brief
```

**Talk to the host or execute `/` commands using Mash repl.** Each message
is an event in the REPL's session:
```bash
mash repl --host assistant
```

**Post an event from your application.** The first event creates the session
and binds it to the host and policy; follow the answer on the event's stream:
```bash
curl -X POST http://127.0.0.1:8000/api/v1/sessions/thread-1/events \
  -H "Content-Type: application/json" \
  -d '{"event_id": "msg-1", "host_id": "assistant", "app_id": "my-app",
       "policy_id": "dev", "message": "what changed in billing last week?"}'

curl -N http://127.0.0.1:8000/api/v1/sessions/thread-1/events/msg-1/stream
```

## Key Concepts

| Concept | What it is |
|---|---|
| **Event and session** | Every input to a host is an event in a session; a session's events run one at a time, each as one request on the companion |
| **App** | The application posting events, named by the `app_id` every event carries; it scopes Cortex and conversation search |
| **Policy** | A named, durable resource many sessions share; it owns the sandbox and the browser profile and sets whether agents get the sandbox, which sites they may open, and how long an interaction may wait |
| **Host** | A composition over the pool (agents + workflows), defined in code or dynamically over the API; the built-in companion runs every event posted to it |
| **AgentSpec** | Abstract contract defining one agent (id, tools, skills, LLM, config) |
| **HostBuilder** | Fluent builder that composes agents, workflows, and hosts into a Pool |
| **Pool** | The deployed pool of agents the API server runs; each agent runs as a subprocess of the host |
| **Sandbox** | A container per policy where agents' code runs, holding that policy's workspaces and nothing else |
| **Browser** | A Chromium per policy, in its own container, whose profile keeps the sites the user signed in to; agents that list sites with `browser_sites()` drive it on those sites |
| **Cortex** | Opt-in per-app memory kept by the host and shared by every agent; each request reads its app's, the `cortex-build` workflow builds it on demand, and apps edit it over the API |
| **WorkflowSpec** | Ordered pipeline of typed steps (`CodeStep` / `AgentStep`); runs are durable and observable |
| **ToolRegistry** | Register callable tools; built-ins include AskUser and the companion's RunSubAgents and RunWorkflow, and the host adds `run_code` and `edit_file` when it has sandboxes, `sandbox_put` and `sandbox_get` for specs that declare file roots, and the browser tools for specs that list sites |
| **SkillRegistry** | Markdown instruction bundles loaded on demand via a meta-tool |
| **LLMProvider** | Adapters for Anthropic, OpenAI, and Gemini |
| **OSSCompatibleProvider** | Runs open-source models (Gemma, Qwen, DeepSeek) over any Chat Completions endpoint, self-hosted (vLLM, Ollama) or hosted (OpenRouter); chosen in `build_llm()` like any provider |
| **Eval / Experiment** | A generated dataset and rubric bound to a host; an experiment runs the dataset against the host, snapshots its composition, and scores results with an LLM judge |

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
questions from the source and searches the docs site and GitHub in the policy's
browser, and its `builder` agent builds Mash apps in the policy's sandbox. It also ships the `pilot-changelog` workflow. Run it from a checkout,
with `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `MASH_HOST_SECRET` and `MASH_API_KEY` in `.env`:

```bash
docker compose -f docker-compose.pilot.yml up -d --build

curl -fsSL https://raw.githubusercontent.com/imsid/mashpy/main/install.sh | sh
echo '{}' | mash policy put me --file - --api-base-url http://127.0.0.1:8000
mash connect --api-base-url http://127.0.0.1:8000 --app-id pilot --policy-id me
pilot repl --host guide
```

See [`src/pilot/README.md`](src/pilot/README.md) for the agents, the keys and
model overrides, the shipped workflow, and building your own app.

## Build with a Coding Agent

This repo includes [`docs/sdk-reference.md`](docs/sdk-reference.md) so coding agents like Claude Code,
Codex, and Cursor can scaffold a Mash-powered agent from a natural language
prompt. Copy it into your project as `CLAUDE.md` or point your agent at this repo to get
started. The Pilot guide (above) also carries the `build-mash-agent`,
`build-mash-workflow`, and `build-mash-host` skills for interactive
scaffolding from the REPL.

## Documentation

An app can ship its own terminal client with `mash.cli.App`: set its name,
default host, deployment URL, app and policy, then expose `app.main` as a
console entrypoint. It includes the live REPL and one-shot commands. See the
[CLI README](src/mash/cli/README.md#ship-your-own-client) for the app kit.

- [Product brief](docs/posts/product-brief.md): what Mash offers, with a Slack team bot as the example
- [Mash under the hood](docs/posts/mash-under-the-hood.md) — what Mash offers and where it fits
- [Synthetic evals](docs/posts/synthetic-evals.md) — datasets, rubrics, experiments, and scoring for the cold-start problem
- [Deployment guide](docs/posts/how-to-deploy.md): agent processes, sandboxes, browsers, Docker, the host on a server
- [Building agent CLIs](docs/posts/building-agent-clis.md) — custom CLI development
- [SDK reference](docs/sdk-reference.md) — full SDK reference for coding agents
- [Package overview](src/mash/README.md) — subsystem boundaries and module guides
- [Contributing](CONTRIBUTING.md) — development setup, tests, repo structure

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup,
tests, and the pull request flow, and [SECURITY.md](SECURITY.md) for reporting
vulnerabilities. Release notes live in [CHANGELOG.md](CHANGELOG.md).

## License

Mash is licensed under the [Apache License 2.0](LICENSE).

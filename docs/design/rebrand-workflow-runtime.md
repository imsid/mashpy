# Rebrand: Self-Hosted Workflow Runtime — Design

Status: proposal
Author: sid (with Claude)
Scope: positioning, object model, README, docs site, blog posts

## Goal

Reposition Mash from "a Python SDK and host runtime for building self-hosted
multi-agent applications" to a self-hosted, durable runtime for code-authored
automations. The thesis: every automation is a workflow, an ordered pipeline of
typed steps governed by code. A step is deterministic Python or one run of a
harnessed agent. Control flow stays in code; the LLM reasons inside a step and
never owns the pipeline unless you deliberately hand it a host.

The positioning statement:

> Mash is a self-hosted runtime for code-authored automations. A workflow is an
> ordered pipeline of typed steps; a step is either deterministic code or a
> harnessed agent. Control flow stays in code. Deploy agents and workflows
> behind one API, durably, on your own Postgres.

Vocabulary rules for all rewritten copy:

- Never "engine". Mash is a runtime one layer above the durable execution
  engine (DBOS). "Runtime", "harness", and "host" are the nouns.
- "Self-hosted" and "durable" appear in the first sentence of every surface
  (README, docs index, PyPI description, product brief).
- No bare "deterministic workflows" claim. The precise claim: deterministic,
  code-owned control flow; nondeterminism contained inside typed steps with
  schema-checked boundaries.
- "Automation" is the umbrella noun; "agents and workflows" names the two
  things you author.

## Object model changes

Workflows become first-class peers of agents in the deploy unit. A host with
only a pool of agents keeps working exactly as today. Four changes, in order of
importance:

### 1. Rename `AgentPool` to `Pool`

`AgentPool` (`src/mash/runtime/host/host.py:29`) already registers workflows
(`register_workflow`, `register_default_workflow`, a `WorkflowRegistry`, a
`WorkflowService`) and serves workflow runs. The name says agents only; the
object does not. Rename the class to `Pool` everywhere in one pass: no alias,
no deprecation shim. Anything that imports `AgentPool` breaks loudly at import
time and gets fixed in the same change.

Call sites (from grep, all mechanical):

- `src/mash/runtime/host/host.py`, `builder.py`, `host/__init__.py`,
  `runtime/__init__.py`
- `src/mash/api/app.py`, `api/main.py`, `api/types.py`, `api/routes/common.py`
- `src/mash/workflows/service.py`, `workflows/dbos.py`
- `src/mash/evals/service.py`
- tests and module READMEs that name the class

`Deployment` was considered (the CLI already says "Show deployment status")
and rejected for now: docs, CLAUDE.md, and the API all say "pool", and the
smallest conceptual change wins. Revisit only if `Pool` proves confusing.

### 2. Workflow-only deploys

`HostBuilder().workflow(spec).build()` with zero `.agent()` calls should be a
supported, tested, documented shape: a pool that serves only workflow runs.
A `WorkflowSpec` of pure `CodeStep`s references no agents, and workflow run
endpoints (`POST /workflow/{id}/run` and friends in
`src/mash/api/routes/workflow.py`) are pool-level, not host-level, so this
likely works today by accident. Make it work on purpose:

- Audit `HostBuilder.build()` and server startup for any assumption of at
  least one user agent (the masher eval agents are always registered, so the
  pool is never literally empty).
- Add a test: build a pool with one code-only workflow and no user agents,
  serve it, run the workflow over the API, assert the run completes.
- `AgentStep` validation already requires the referenced `agent_id` to exist
  in the pool; keep that.
- Document the shape in the workflows README and CLAUDE.md.

Hosts are unchanged. A `Host` still requires a `primary` agent; there is no
workflow-only host, because workflow runs never needed a host in the first
place. Say that explicitly in the docs instead of adding a new host shape.

### 3. Top-level exports

`mash/__init__.py` exports only version metadata today. Export the authoring
surface so the import line shows agents and workflows as peers:

```python
from mash import (
    AgentSpec, AgentMetadata, Host, HostBuilder,
    WorkflowSpec, CodeStep, AgentStep, StepContext,
)
```

Use a lazy `__getattr__` re-export so `import mash` does not drag in DBOS or
provider SDKs. The top-level form is the documented import style everywhere
(README, CLAUDE.md, scaffolds); deep module paths stay valid but drop out of
the docs.

### 4. CLI parity: `mash workflows`

`mash agents` and `mash hosts` exist as top-level subcommands
(`src/mash/cli/main.py`); workflows are only reachable through the REPL
`/workflow` command and `_workflow_rows` in browse output. Add a
`mash workflows` subcommand that lists the deployment's workflows (id, step
count, step kinds), same table treatment as `mash agents`.

### 5. `mash connect` saves the connection only

`mash connect` writes `~/.mash/cli.json` and nothing else: no handshake, no
validation, and an optional `--agent`/`--host` pin that duplicates what
`mash compose` and per-command flags already do. Cut it to connection-only:

- Flags: `--api-base-url` and `--api-key`. Drop `--agent` and `--host` from
  `connect`; targeting lives in per-command flags, `mash compose` (which pins
  the host it defines), and single-target auto-resolution in
  `_resolve_target`.
- Call `client.health()` before saving, so `connect` fails loudly on a wrong
  URL or key and the name stops overpromising.
- `CLIConfig` drops `agent_id`. `host_id` stays because `mash compose` pins
  its composition there.

This also settles workflow-only deploys on the CLI: `connect` has no agent
requirement, so `mash connect` then `mash workflows` and workflow runs work
against a pool with no user agents. The REPL keeps requiring an agent or host
target.

Quick starts in README.md, CLAUDE.md, and the posts show
`mash connect --agent <id>`; the docs PR rewrites those to `mash connect`
followed by a targeted command (`mash repl --agent <id>` or `mash compose`).

### Explicit non-changes

- `HostBuilder`, `Host`, `AgentSpec`, `WorkflowSpec` keep their names and
  shapes.
- No scheduling or trigger primitives in this pass; workflow runs remain
  API/CLI-initiated.
- No changes to the durable engine, the H2A RFC, or API route paths.
- `build_pool()` stays the documented entrypoint convention.

## README

`README.md` keeps its structure (badges, what-mash-provides, diagram, quick
start) and changes framing:

- **Headline** (currently "A Python SDK and host runtime for building
  self-hosted multi-agent applications."): replace with the positioning
  statement, condensed to two sentences. Lead with self-hosted and durable.
- **"What Mash Provides"**: reorder so the list opens with the workflow story:
  1. Workflows: durable typed step pipelines, code as the source of truth
  2. Agent harness: tools, skills, memory, HITL, subagents
  3. Frontier and open-source models
  4. Self-hosted interfaces: API, CLI, REPL, one Postgres
  then observability, evals. Current bullets survive; the order and the
  first bullet's copy change.
- **Quick start**: already flows agents → workflow → pool → serve. Keep the
  order but retitle "Add a workflow" so it reads as the point of the exercise
  rather than an extra. Add one sentence and snippet for the workflow-only
  shape once change 2 lands.
- **Diagram**: the durable-request ASCII diagram stays; extend the left edge
  so `workflow` appears as a peer entry point (it already does) and the
  caption says "a step in a workflow or a request from a user rides the same
  durable loop".

`pyproject.toml` description (currently "SDK for building hosted multi-agent
Mash applications.") becomes: "Self-hosted durable runtime for code-authored
agents and workflows." This is the PyPI tagline; ship it with the next
release. GitHub repo description and topics are a manual step for sid, same
sentence.

## Docs site

- `docs/index.md`: headline and frontmatter description change to match the
  README. The hero line "Build self-hosted agent applications on frontier and
  open source models" becomes the positioning statement; the capability
  paragraph leads with workflows and the harness.
- `docs/posts/product-brief.md` ("Start Here" in nav): rewrite around the new
  framing. Per posts style: no competitor contrasts, state what Mash does
  positively, flat register.
- `CLAUDE.md`: the opening paragraph and Core Concepts list reframe around
  pool = agents + workflows; add the workflow-only scaffold shape; swap
  imports to the new top-level form. The agent scaffold stays first since it
  is still the common case.
- Module READMEs terminology sweep, smallest edits that align the frame:
  `src/mash/README.md`, `src/mash/runtime/README.md` (uses "pool" already),
  `src/mash/workflows/README.md` (tighten the determinism claim to the
  control-flow version), `src/mash/api/README.md`, `src/mash/cli/README.md`.
- `zensical.toml` nav labels only if post titles change; keep nav order.

## Blog posts

One new post plus targeted updates. Follow the posts conventions: concrete
opening scenario, one mermaid diagram, ≤1,900 words, flat register, no
competitor contrasts, no rhetorical questions.

1. **New: `docs/posts/code-as-the-source-of-truth.md`** ("Start Here"
   section, after the product brief). The positioning post. Opens with a
   concrete automation (the README's research-brief pipeline works), shows
   the same automation as a pure-code pipeline, then with an agent step, then
   deployed. The one idea: the pipeline is code you can read, diff, test, and
   replay; the agent is a step with a schema on both edges. Diagram: a
   pipeline with a code step and an agent step, both feeding the durable
   store.
2. **Update `docs/posts/workflows-as-step-pipelines.md`**: add the
   workflow-only deploy shape once change 2 lands; align the intro with the
   first-class framing. No structural rewrite.
3. **Update `docs/posts/product-brief.md`**: covered above; it is the pitch,
   so it carries most of the rebrand weight on the docs site.

## Sequencing

Three PRs, in order, each shippable alone:

1. **Object model**: `Pool` rename (clean cutover, all call sites in one
   commit), workflow-only deploy audit and test, top-level exports,
   `mash workflows` CLI command, connection-only `mash connect`, module
   README and docstring updates that the code changes touch. Regenerate CLI
   docs.
2. **Docs and README**: README, `docs/index.md`, CLAUDE.md, `pyproject.toml`
   description, terminology sweep.
3. **Blog**: new post, product-brief rewrite, workflows post update,
   `zensical.toml` nav entry.

Manual follow-ups for sid: GitHub repo description and topics; PyPI tagline
goes out with the next release automatically.


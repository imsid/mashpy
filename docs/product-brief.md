# Mash Product Brief

Mash is a self-hosted runtime for multi-agent applications.

It is built around the simplicity of the H2A protocol: a small, stable host to
agent boundary for submitting requests, streaming events, and running hosted
agents. On top of that simple interface, Mash provides the runtime
infrastructure needed to turn agent behavior into a real product.

Most agent demos are easy to start and hard to operationalize. The model may
work, but the product breaks down when teams need multiple agents, structured
execution, durable requests, inspectable behavior, or a deployment model they
control themselves. Mash is designed for that step. It is not just a prompt
wrapper or a thin chat backend. It is a self-hosted agent runtime built around
a simple H2A protocol boundary, with the operational capabilities needed to run
real multi-agent systems.

## What Mash Provides

### A Simple Runtime Model for Multi-Agent Systems

Mash lets teams define a primary agent, add specialized subagents, and compose
workflow-only agents behind the same host. That makes it possible to build
systems where general-purpose agents delegate to focused specialists without
introducing a separate coordination layer outside the runtime.

### Durable Hosted Execution

Mash separates the protocol boundary from the runtime machinery behind it. A
request is accepted, executed through a durable request engine, and recorded as
replayable runtime events. That gives teams a stronger operational model than a
single in-memory agent loop and makes retries, restarts, and long-running work
much easier to manage.

### Human-in-the-Loop Interactions

Mash supports durable agent-to-user interactions as part of the hosted runtime.
An agent can pause mid-execution to request approval before a sensitive tool
runs, or ask the user a question when it needs information to continue. These
interactions are durable: the runtime can restart, and the waiting agent resumes
exactly where it left off when the user responds — whether that takes seconds or
hours.

Tool developers gate execution behind user consent by setting a single attribute
on the tool definition. Agents ask users questions by calling a built-in tool
that the runtime intercepts and translates into a durable interaction. Both paths
use the same protocol-level interaction events and the same host-to-client
response flow, so the operational model stays simple regardless of who initiates
the interaction.

### Structured Outputs Across the System

Mash supports structured outputs as a first-class capability. They are not
limited to workflows. The same structured-output path can be used for ordinary
agent requests as well as workflow tasks, which makes it easier to build typed
integrations, machine-readable agent responses, and predictable downstream
automation.

### Context, Memory, Tools, Skills, and Signals

Mash provides the runtime building blocks that power the agent loop itself:
context management, memory, local and remote tools, skills, and end-of-loop
signals.

That includes:

- context compaction to keep long-running sessions usable
- memory search tools to recover relevant prior information
- local and remote tool execution inside the hosted runtime
- skills for reusable agent behaviors
- signals collected at the end of each loop to capture structured state and outcomes

This gives teams a practical way to move beyond text-in, text-out behavior and
build agents that can act with context, preserve useful state, and expose
durable outputs from each completed loop.

### Workflows for Structured Execution

Mash workflows are ordered sequences of tasks. Each task runs through a
registered agent, can be paired with a skill, and can require a structured
output. That structured output becomes the task's persisted state for future
runs and downstream steps.

Mash supports both code-defined workflows and dynamic workflow and skill
registration, so teams can define workflows in host code or publish them at
runtime. This makes workflows a concrete execution model for repeatable,
stateful agent tasks rather than a loose orchestration layer.

### Observability Built Into the Runtime

Mash includes runtime-level telemetry and replayable runtime events so teams can
inspect how a request moved through the system, not just what the final answer
was. The telemetry UI gives operators a request and trace view, and memory
search is available alongside that runtime observability so teams can inspect
both execution behavior and stored conversational context. This matters when
agents become operational software and developers need traceability, debugging,
and a clear view of runtime behavior.

### Built-In Eval and Trace Digest Workflows

Mash includes Masher, a built-in workflow-only worker for trace processing.

Masher can:

- summarize a trace into a digest that captures the key request, execution flow, and outcome
- generate a normalized online eval record from that trace for downstream analysis and curation

This gives teams an immediate built-in path for runtime analysis and eval
generation without first building a separate post-processing pipeline.

### Self-Hosted Interfaces for Use and Operations

Mash exposes agents over HTTP, supports streaming responses, includes a CLI and
REPL, and fits naturally into local, server, and containerized deployments. The
same platform can be used to develop an agent system, integrate it into product
surfaces, and operate it in a controlled environment. Checkout [Crew Agent](https://github.com/imsid/crew/blob/main/docs/product.md) for an implementation.

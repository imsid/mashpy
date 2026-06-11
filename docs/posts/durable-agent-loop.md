---
title: The Durable Agent Loop
description: Why the Mash request engine checkpoints plan, tool call, and commit as separate steps instead of running the agent loop in one piece.
date: 2026-06-10
author: imsid
tags:
  - internals
  - durability
---

# The Durable Agent Loop

Start a request that takes a dozen steps, and halfway through, `kill -9` the host process. When the host comes back up, the request finishes from where it left off: completed tool calls stay completed, the loop resumes at its last checkpoint, and a client streaming events watches it happen. This post is about the design that makes that work.

## The loop itself is simple

At the core of every Mash agent is the think–act–observe loop in `mash/core/agent.py`. Stripped of logging, it reads like the textbook version:

```python
# src/mash/core/agent.py: Agent.run() (trimmed)
for step in range(self.config.max_steps):
    plan = await self.plan_step(context)      # think: one LLM call → an Action
    results = await self.act(plan.action)     # act: execute the planned tool calls
    commit = self.commit_step(                # observe: fold results into context,
        context, plan.action, results,        # decide whether we're done
        step_index=step,
    )
    context = commit.context
    if commit.done:
        break
```

`Agent.run()` is real and used as the in-memory execution path. The hosted runtime never calls it, though, and the reason why is most of this post.

## Why the engine doesn't call `run()`

The hosted runtime executes each request as a [DBOS](https://docs.dbos.dev) workflow, a function whose progress is checkpointed to Postgres so it can resume after a crash. DBOS resumes a workflow by replaying it from the last completed step.

Now suppose the workflow had one step, `Agent.run()`. A crash at step 9 of 12 means the last completed checkpoint is the start of the request, so recovery replays the whole turn. Every LLM call gets paid for again, and worse, every tool call executes again. If the agent had already sent a message or deployed a service, replay does it a second time.

So the unit of durability has to be smaller than the loop. The agent exposes its phases as separate methods (`plan_step`, `execute_step_tool_call`, `commit_step`), and the workflow wraps each one in its own checkpoint:

```python
# src/mash/runtime/engine/workflow.py: execute_request_workflow (trimmed)
while True:
    loop_index = int(workflow_state.get("loop_index") or 0)

    workflow_state = await retry_transient(
        lambda: DBOS.run_step_async(
            {"name": f"step.plan.{loop_index}"},
            plan_request_step, ...,
        )
    )

    for call_index, tool_call in enumerate(tool_calls):
        existing_results = list(workflow_state.get("result_payloads") or [])
        if call_index < len(existing_results):
            continue                      # already ran before the crash; skip
        workflow_state = await _run_tool_call_for_workflow(
            ..., loop_index=loop_index, call_index=call_index, tool_call=...,
        )

    workflow_state = await DBOS.run_step_async(
        {"name": f"step.commit.{loop_index}"},
        commit_request_step, ...,
    )

    if bool(workflow_state.get("done")):
        # persist the final turn, complete the request
        return
```

Every checkpoint has a stable name like `step.plan.3` or `tool.call.3.1`, and each tool call is its own checkpoint. The `continue` in the middle is what crash recovery hinges on: completed tool results live in `result_payloads`, so on resume, tool calls that already finished are skipped by index, and execution proceeds from the first one that didn't.

```mermaid
flowchart TD
    L[context.load] --> P["step.plan.N (one LLM call)"]
    P --> T0["tool.call.N.0"]
    T0 --> T1["tool.call.N.1 …"]
    T1 --> C["step.commit.N (observe, decide done)"]
    C -- "not done" --> P2["step.plan.N+1"]
    C -- "done" --> F["turn.persist → request.complete"]
    P2 -.-> C

    style P fill:#1a7f37,color:#fff
    style T0 fill:#1a7f37,color:#fff
    style T1 fill:#1a7f37,color:#fff
    style C fill:#1a7f37,color:#fff
```

Every green box is a durable checkpoint. A crash between any two of them resumes at the boundary, so the most you can lose is the one step that was in flight. Losing an in-flight plan step is acceptable: re-planning costs one extra LLM call and has no side effects.

## What travels between checkpoints

DBOS replays the workflow function, so anything the loop needs must flow through the step return values. The workflow carries one state dict between checkpoints:

| Field | What it holds |
|---|---|
| `context` | the serialized model context, updated after every plan and commit |
| `loop_index` | which step of the loop we're on |
| `action` | the currently planned action (tool calls to run) |
| `result_payloads` | completed tool results for the current step; the resume cursor |
| `aggregate_usage`, `tool_usage` | token accounting across the run |
| `done` | whether `commit_step` declared the run terminal |

All of this is execution state. Nothing here is written as a conversation turn until the run completes; partial progress lives only in workflow state and the event log. That boundary is the subject of the next post.

## Three layers of failure handling

Crash recovery is one of three failure layers, and they cover different things:

| Failure | Handled by | You do |
|---|---|---|
| Transient error (rate limit, timeout, network blip) | `retry_transient()`: in-process retry with exponential backoff and jitter, 3 attempts | nothing |
| Retries exhausted, or terminal error (bad API key, context overflow) | workflow emits `request.error` with `error_code` and `retryable` | inspect; call `POST .../resume` if it's worth retrying |
| Process crash (OOM, `kill -9`, hardware) | DBOS finds the orphaned workflow on next startup and replays from the last checkpoint | nothing |

The first layer wraps the two hot paths, LLM planning and tool execution, and decides what's transient by pattern-matching the error:

```python
# src/mash/runtime/errors.py (trimmed)
_RETRYABLE_PATTERNS = (
    (("rate_limit", "429", "too many requests"), "rate_limit_exceeded"),
    (("timeout", "timed out", "deadline exceeded"), "timeout"),
    (("connection", "network", "dns", "socket"), "network_error"),
    ...
)
_TERMINAL_PATTERNS = (
    (("authentication", "unauthorized", "401", ...), "auth_error"),
    (("context_length_exceeded",), "context_length_exceeded"),
    ...
)
```

Unknown errors default to retryable, because abandoning recoverable work costs more than one extra failed attempt.

The third layer has one operational subtlety: a crashed process emits no `request.error`, because nothing was alive to emit it, and the event stream just goes quiet. `GET .../request/{id}/status` covers this case by querying the DBOS workflow state directly. A status of `pending` means the request will be auto-recovered on startup; `failed` means it needs a resume call.

## The trade

The design has a real cost. Context must round-trip through workflow state on every step, and the loop had to be expressed as three primitives instead of one method. What it buys is at-most-once execution: a tool call runs no more than once per request, no matter how many retries or restarts happen around it. For an agent whose tools write to the world, that property is what makes it safe to run unattended.

One loose end remains: workflow state holds the in-flight request, but finished turns and the event log each live in a store of their own.

*Next: [Two Stores](two-stores.md).*

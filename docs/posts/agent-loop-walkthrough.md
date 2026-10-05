---
title: Agent Loop Walkthrough
description: An interactive, step-by-step view of the Mash agent loop as a DBOS workflow, with the workflow state, event log, and crash recovery at each step.
date: 2026-10-04
author: imsid
hide:
  - toc
tags:
  - internals
  - durability
  - interactive
---

# Agent Loop Walkthrough

A request to a Mash agent runs as one DBOS workflow inside the host process. This page steps through that workflow for five kinds of request. Each entry in the timeline is one operation in [`execute_request_workflow`](https://github.com/imsid/mashpy/blob/main/src/mash/runtime/engine/workflow.py): a checkpointed DBOS step, a durable wait, or code that runs at workflow scope.

## Where DBOS runs

DBOS is a Python library. The host process imports it and launches it the first time an agent runtime opens: [`ensure_dbos_ready`](https://github.com/imsid/mashpy/blob/main/src/mash/runtime/engine/dbos.py) constructs DBOS with `system_database_url` set to `MASH_DATABASE_URL`, registers the request workflow, and calls `DBOS.launch()`. No separate DBOS server or worker runs. Workflows execute as tasks on the host's event loop.

DBOS keeps its state in the same Postgres database as the Mash tables. Three of its tables come up in the walkthrough:

| Table | What it holds for a request |
|---|---|
| `workflow_status` | One row per request workflow, `PENDING` until the workflow function returns, then `SUCCESS` or `ERROR` |
| `operation_outputs` | The return value of every completed step, keyed by workflow id and step order |
| `notifications` | Messages sent with `DBOS.send`, which is how interaction replies reach a waiting workflow |

Each request runs the workflow function `mash.runtime.execute_request` under the workflow id `{agent_id}:{request_id}`. Every step takes the previous `workflow_state` dict as an argument and returns a new one, and DBOS records that return value. Events go to a separate place: each step appends rows to `runtime_event_log` with a dedupe key, and the SSE stream reads them from there.

## Using the walkthrough

Pick a scenario, then step with **Next** and **Prev** or the arrow keys. Clicking a timeline entry jumps to it. The map at the top highlights the parts of the process and the tables each step touches. The two panels on the right show the workflow variables after the step and the rows in `runtime_event_log` so far, with each row's dedupe key and the event name the stream sends.

**Crash during this step** kills the host process at the current step and plays out the restart: `DBOS.launch()` recovery, replay of the recorded steps, and the re-run of the step that was in flight. One crash per run; **Reset** clears it.

<div data-mash-viz="agent-loop"><noscript>This walkthrough needs JavaScript.</noscript></div>

Ids, token counts, and tool names in the walkthrough are illustrative. Step names, event types, dedupe keys, and defaults are the ones in the code.

## Kinds of operation

| Label | Code | On replay |
|---|---|---|
| DBOS step | `DBOS.run_step_async({"name": ...}, fn, ...)` | Returns the recorded output without calling `fn` |
| DBOS recv | `DBOS.recv_async(topic, timeout_seconds=...)` | Returns the recorded message, or waits again if none was received |
| workflow scope | Called directly in the workflow function, as `InvokeSubagent` is | Runs again; the deterministic child id attaches it to the existing child workflow |
| plain code | Loop control, the approval-denied results | Runs again and computes the same values from replayed inputs |

## Step names

These are all the step names `execute_request_workflow` can record. `{loop}` is `loop_index`, `{call}` is the tool call's position in the action, and `{attempt}` counts interaction attempts, which only goes past 0 after a cancel and resume.

| Step | Function | Retried by `retry_transient` |
|---|---|---|
| `request.start` | `start_request_trace` | No |
| `context.load` | `load_request_context` | No |
| `step.plan.{loop}` | `plan_request_step` | Yes |
| `interaction.open.{loop}.{attempt}` / `interaction.ack.{loop}.{attempt}` | `open_interaction` / `emit_interaction_ack` | No |
| `tool.call.{loop}.{call}` | `run_step_tool_call` | Yes |
| `tool.batch.{loop}.{call}` | `run_step_tool_batch` | Yes |
| `ask_user.open.{loop}.{attempt}` / `ask_user.ack.{loop}.{attempt}` | `open_interaction` / `emit_interaction_ack` | No |
| `step.commit.{loop}` | `commit_request_step` | No |
| `structured_output.finalize` | `finalize_structured_output` | No |
| `turn.persist` | `persist_completed_turn` | No |
| `request.complete` | `complete_request` | No |
| `request.fail` | `fail_request` | No |

`retry_transient` retries errors that `classify_error` marks transient (rate limits, timeouts, network errors, overloaded providers) up to 3 times, with backoff starting at 1 second, capped at 30 seconds, with jitter. `structured_output.finalize` runs only when the caller asked for structured output and no tool produced it. `request.fail` runs when any exception escapes the loop: it appends `runtime.request.failed`, which the stream sends as `request.error`, and re-raises so DBOS marks the workflow `ERROR`.

The [H2A envelope walkthrough](h2a-envelope-walkthrough.md) covers how a request reaches this workflow and how its events get back to the caller. [The Durable Agent Loop](durable-agent-loop.md) covers cancel, resume, and rerun.

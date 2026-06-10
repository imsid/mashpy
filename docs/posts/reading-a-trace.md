---
title: Reading a Trace
description: From flat runtime events to a span tree to a deterministic latency analysis, computed straight from the runtime's own event log.
date: 2026-06-10
author: imsid
tags:
  - internals
  - observability
---

# Reading a Trace

Every post in this series ended up at the same place: events in an append-only log. This one is about reading them back out — specifically, turning the flat event stream of one request into the answer operations work actually needs: where the time went.

The pipeline has three stages, all deterministic:

```mermaid
flowchart LR
    E["RuntimeEvent list\n(flat, ordered)"] --> T["build_span_tree()\nhierarchy"]
    T --> A["analyze_trace()\ntiming, tools, steps"]
    A --> S1["/trace (REPL)"]
    A --> S2["GET /telemetry/trace/analysis"]
    A --> S3["telemetry UI waterfall"]
```

All three stages are pure computation over event timestamps and payloads — run `analyze_trace` twice on the same trace and you get the same numbers, which is precisely the property you want from the thing you'll use to argue about latency.

## From events to spans

`build_span_tree(events)` in `runtime/events/spans.py` folds the flat list into a hierarchy. The vocabulary is small:

```python
class SpanKind(str, Enum):
    TRACE = "trace"
    COLD_START = "cold_start"
    CONTEXT_LOAD = "context_load"
    STEP = "step"
    THINK = "think"
    TOOL_CALL = "tool_call"
    SUBAGENT_CALL = "subagent_call"
```

A typical request becomes:

```text
TRACE                                    4.81s
├── COLD_START                           120ms   accepted → trace started
├── CONTEXT_LOAD                          85ms   history + summary loaded
├── STEP 0
│   ├── THINK                            1.4s    (tokens in payload)
│   └── TOOL_CALL  bash                  640ms
├── STEP 1
│   ├── THINK                            1.1s
│   └── SUBAGENT_CALL  cli-copilot       1.2s
└── STEP 2
    └── THINK                            260ms   final response
```

Two of these kinds measure gaps. `COLD_START` is the gap between `request.accepted` and `trace.started` — the cost of the engine picking the request up, which the [lifecycle post](request-lifecycle.md) showed are two separate events for exactly this reason. And whatever time remains inside the trace that no span claims gets reported as **idle** — gaps between steps, scheduling overhead, anything unaccounted. A request that "feels slow" with fast thinks and fast tools shows its problem in cold start or idle — host-side numbers.

## From spans to answers

`analyze_trace(tree)` reduces the tree to the standard interrogation set:

- **Timing breakdown** — total duration split across cold start, context load, think, tool, subagent, and idle, each with absolute and percentage values. The one-line summary of where a request spent its life.
- **Per-tool stats** — call count, total/avg/max/min latency, error count, sorted by total time so the most expensive tool reads first.
- **Per-step breakdown** — think vs. tool vs. overhead for each loop iteration; the shape tells you whether a request was slow because of one bad step or uniformly heavy.
- **Slowest operations** — top spans by duration, the straight-to-the-point list.

Delegation costs flow through the tree. A `SUBAGENT_CALL` span links the child's trace, and analysis recurses into it up to three levels deep — a slow primary request whose time went into a subagent's tool call attributes the cost to that tool. The mirrored `subagent.*` events from [the composition post](composing-agents.md) are what make the stitching possible.

## Three doors to the same numbers

The analysis renders in three places, all backed by the same computation:

In the REPL, `/trace` analyzes the most recent trace (or `/trace 5` for the last five) — summary line, timing table with bars, tool stats, slowest operations, right in the terminal. This is the development-loop door: send a message, feel the lag, type `/trace`, see the breakdown while the request is still fresh.

Over HTTP, `GET /api/v1/telemetry/trace/analysis?agent_id=…&session_id=…&trace_id=…` returns the span tree and the full analysis dict in one call. This is the integration door — dashboards, alerts, regression checks in CI — and the trace id to query for is sitting on each turn in memory, since [trace id doubles as turn id](two-stores.md).

In the browser, `/telemetry` renders the waterfall: collapsible spans with proportional bars, a phase-distribution summary on top, tool and step tables below. The investigation door, for when you're comparing several traces and a terminal table stops scaling.

For continuous use, Masher — the built-in workflow-only agent from the composition post — packages the same analysis into scheduled workflows: `masher-trace-digest` emits a full diagnostic snapshot per trace, and `masher-online-eval-curation` writes eval records with latency context. Both run incrementally over the event log, checkpointed by [task state](workflows-and-task-state.md), which makes them safe on a schedule.

## The series, closed

This post is the payoff of the first one. The event log was designed as the record of what happened, and spans and analysis read that record directly — the same events that drive SSE replay drive the latency breakdown.

That's the full tour of the internals: a request becomes events, the engine executes it in checkpoints, two stores keep record and memory apart, tools and interactions and providers plug into the loop through narrow contracts, and hosts compose agents and workflows out of the same parts. From here, the [CLI guide](building-agent-clis.md) puts a custom shell on top of what you've built, and the [deploy guide](how-to-deploy.md) takes it to production.

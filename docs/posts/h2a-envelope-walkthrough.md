---
title: H2A Envelope Walkthrough
description: An interactive view of the Host-to-Agent protocol in Mash, following submit, stream, and interaction calls hop by hop through both bindings, with the mapping from stored events to wire events.
date: 2026-10-04
author: imsid
hide:
  - toc
tags:
  - internals
  - h2a
  - interactive
---

# H2A Envelope Walkthrough

The [Host-to-Agent Protocol (H2A)](../rfcs/host-to-agent-protocol.md) defines four roles: a user application, a host, a client per agent, and the agent. A request crosses them with three operations: `post_request` to submit, `stream_response` to read the event stream, and `post_interaction` to answer a paused request. This page follows each operation hop by hop through the Mash code.

Mash has two bindings for these roles. A pool deploy (`mash host serve`) puts every agent behind one FastAPI app, and the pool gives each agent an [`InProcessAgentClient`](https://github.com/imsid/mashpy/blob/main/src/mash/runtime/client.py), so the host-to-client and client-to-agent hops are Python calls. The HTTP binding serves one agent per [`AgentServer`](https://github.com/imsid/mashpy/blob/main/src/mash/runtime/server.py) and reaches it with an `AgentClient` over HTTP and SSE. Both end at the same `AgentRuntime` methods.

## Following a request

Pick a binding and an operation, then step through the hops. The lanes show which role is handling the request, and the panel on the right shows the payload or call at that hop.

<div data-mash-viz="h2a-flow"><noscript>This walkthrough needs JavaScript.</noscript></div>

In a pool deploy the API routes live under `/api/v1` and wrap responses in `{"data": ...}`. `session_id` is required on every submit route. A submit returns as soon as the request's DBOS workflow has started; the [agent loop walkthrough](agent-loop-walkthrough.md) picks up from there.

## Stored events and wire events

The runtime stores every event in `runtime_event_log` under a `runtime.*` type. When a client reads the stream, [`to_public_event`](https://github.com/imsid/mashpy/blob/main/src/mash/runtime/requests.py) turns each row into the event the caller sees. Lifecycle events get their own wire names with fixed fields. Every other event type goes out as `agent.trace`, with the original type in `event_type` and the step's `loop_index` and `step_key`.

Select a row to see the frame it produces. Below the table, the sample stream plays the frames for the parallel tools request from the agent loop walkthrough in order.

<div data-mash-viz="h2a-events"><noscript>This walkthrough needs JavaScript.</noscript></div>

`request.completed`, `request.error`, and `request.cancelled` are terminal, and the pool's events route closes the stream after any of them. The runtime decides terminality from the `runtime_request` row that the store updates in the same transaction as the lifecycle event, so a stream that ends has always delivered its terminal frame.

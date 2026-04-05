# Host-to-Agent Protocol (H2A)

Status: Draft

Version: 0.2.0

Last Updated: 2026-04-04

## 1. Overview

The Host-to-Agent Protocol (H2A) defines an interoperability protocol for interaction between host applications and agents.

In the H2A model, each addressable agent is exposed as a runtime endpoint and has an associated client used to communicate with it. A host keeps track of the available clients and uses them to interact with one or more agents on behalf of user applications.

The protocol semantics are defined independently of any specific transport. This document defines HTTP plus Server-Sent Events (SSE) as the first standard transport binding.

H2A assumes a user-facing application interacts with a host, and the host acts as the bridge to one or more agents. H2A therefore standardizes the host-to-client and client-to-agent lifecycle that user-facing hosts depend on, rather than agent internals or tool interoperability.

## 2. Goals, Non-Goals, And Positioning

### 2.1 Goals

- Define a deterministic host-to-agent request lifecycle.
- Define canonical request and event envelopes.
- Allow multiple transport bindings while preserving one protocol core.
- Support host-managed deployments that expose one or more agents behind one host.
- Leave room for future extensions without making them part of the core topology.

### 2.2 Non-Goals

- H2A does not standardize an agent reasoning model.
- H2A does not standardize model-provider APIs.
- H2A does not standardize tool invocation protocols or external context access.
- H2A does not require peer-to-peer agent meshes.
- H2A does not define a universal UI protocol.

### 2.3 Positioning

H2A is complementary to MCP. MCP is primarily concerned with tool, resource, and context interoperability between AI applications and external systems. H2A is concerned with host-to-agent execution, lifecycle, and transport semantics.

H2A is adjacent to agent-to-agent protocols such as A2A. H2A is host-to-agent first. Agent-to-agent delegation MAY be layered on top of H2A in future revisions, but that is not the primary v1 topology.

## 3. Architecture And Conformance

### 3.1 Roles

H2A defines the following roles:

- `User Application`: a UI, client, automation, or orchestration surface that talks to a host.
- `Host`: the protocol participant that exposes H2A operations, manages sessions, and brokers access to one or more agents.
- `Client`: a protocol-facing component associated with one agent and used by a host to invoke that agent over H2A transports.
- `Agent`: an execution target selected by a host and exposed as an addressable runtime endpoint.

### 3.2 Topology

H2A is host-centric in v1:

- A host MAY expose one agent or many agents.
- A user application SHOULD address the host, not agents directly.
- A host MUST expose stable `agent_id` values for addressable agents.
- Each addressable agent MUST have an associated client.
- A host MUST be able to resolve `agent_id` to the associated client.
- A host-facing client MUST target exactly one agent at a time.
- Agent-to-agent relationships are out of core scope.

In a typical deployment:

- an agent is exposed over one or more concrete transports such as HTTP plus SSE
- a client communicates with exactly one agent endpoint
- the host tracks the set of available clients and selects the appropriate client for a requested `agent_id`

Illustrative runtime topology:

```mermaid
flowchart TD
    U["User Application"] --> H["Host"]
    H --> R["Client Registry"]
    R --> C1["Client for Agent A"]
    R --> C2["Client for Agent B"]
    C1 --> A1["Agent A Runtime Endpoint"]
    C2 --> A2["Agent B Runtime Endpoint"]
    A1 --> E1["Agent Execution Loop"]
    A2 --> E2["Agent Execution Loop"]
```

### 3.3 Conformance

An implementation claiming H2A conformance MUST implement the H2A request flow defined by this specification and at least one supported transport binding.

H2A conformance is defined in terms of hosts, agents, clients, and protocol behavior rather than a separate deployment boundary. A host MAY embed transport servers internally, including running one per-agent HTTP server in the same process.

## 4. Protocol Structure

At a high level, H2A standardizes three communication standards:

- `User Application -> Host`
- `Host -> Client`
- `Client -> Agent`

### 4.1 User Application To Host

The user application talks to a host that accepts request submission and exposes streamed request events.

### 4.2 Host To Client

The host resolves `agent_id` to the associated client and uses that client to create requests and consume request streams for one agent.

### 4.3 Client To Agent

The client talks directly to the per-agent runtime endpoint that accepts HTTP requests and emits SSE events.

## 5. Lifecycle

H2A request flow proceeds through the three communication standards in sequence.

### 5.1 User Application To Host

The user application submits work to the host using a host-facing request operation such as `submit_request`.

The host-facing submission includes:

- `agent_id`
- `message`
- optional `session_id`

The host returns a host-facing request identifier after the downstream agent request has been accepted.

After submission, the user application consumes the resulting lifecycle from the host using a host-facing stream operation such as `stream_request_events`.

### 5.2 Host To Client

The host resolves the requested `agent_id` to the associated client before request submission begins.

The host submits a request to the resolved client using:

- `message`
- optional `session_id`

### 5.3 Client To Agent

The client turns the host request into a transport request for exactly one agent runtime.

If `session_id` is omitted or empty, the agent runtime resolves the request into its default session before execution begins. The runtime returns a `request.accepted` payload containing `request_id`, `agent_id`, `session_id`, and `status`.

### 5.4 Agent Execution And Streaming

After acceptance, the runtime executes the request and exposes request lifecycle state through a streamed event sequence. The client consumes those events and the host relays them back to the user application.

Illustrative request flow:

```mermaid
sequenceDiagram
    participant App as User Application
    participant Host
    participant Client as Agent Client
    participant Server as Agent Server
    participant Store as State Store

    App->>Host: submit_request(agent_id, message, session_id)
    Host->>Host: Resolve agent_id to associated client
    Host->>Client: post_request(message, session_id)
    Client->>Server: POST /agent/{agent_id}/request
    Server-->>Client: request.accepted
    Client-->>Host: request.accepted
    Host-->>App: request_id
    App->>Host: stream_request_events(agent_id, request_id)
    Host-->>App: SSE request.accepted
    Server->>Server: Start request task
    Server->>Store: Persist turn state
    Server-->>Client: request.waiting? / request.started / agent.trace / request.completed
    Client-->>Host: Stream events
    Host-->>App: SSE request.waiting? / request.started / agent.trace / request.completed
```

### 5.5 Completion And Retention

Request execution ends with exactly one terminal event: `request.completed` or `request.error`.

The current runtime retains accepted requests and buffered events for an implementation-defined time window and count bound. The runtime binding guarantees live streaming of request events, but does not currently define a separate retained request resource or replay API.

## 6. Host To Client Interaction

This section defines the second communication standard: `Host -> Client`.

At this layer, H2A centers interaction on two operations:

- `post_request`
- `stream_response`

These two operations are sufficient for a host to submit work to one agent and observe the resulting lifecycle until completion.

### 6.1 `post_request`

`post_request` creates one asynchronous request for one agent.

Inputs:

- `message: string`
- optional `session_id: string`

Rules:

- The client MUST submit `post_request` as an HTTP `POST`.
- The request body MUST be a JSON object.
- `message` MUST be present and MUST be a non-empty string.
- `session_id` MAY be omitted.
- If `session_id` is omitted or empty, the agent runtime MUST resolve the request into its default session before execution begins.
- Same-session overlap MUST be accepted rather than rejected. If execution cannot begin immediately because another request for the same `session_id` is in flight, the runtime MAY emit a later `request.waiting` event.

The server response MUST be `202 Accepted` with a JSON object containing:

- `request_id: string`
- `agent_id: string`
- `session_id: string`
- `status: "accepted"`

Example:

```json
{
  "message": "Plan the next release.",
  "session_id": "sess_123"
}
```

Accepted response:

```json
{
  "request_id": "req_123",
  "agent_id": "planner",
  "session_id": "sess_123",
  "status": "accepted"
}
```

### 6.2 `stream_response`

`stream_response` consumes the event stream for one previously accepted request.

Inputs:

- `request_id: string`

Rules:

- The client MUST open `stream_response` as an HTTP `GET` against the request stream endpoint for that `request_id`.
- The server MUST respond with `200 OK` and `Content-Type: text/event-stream` when the request exists.
- Each event MUST be encoded as an SSE event with `event:` and `data:` lines.
- The `data:` payload MUST be a JSON object.
- The client MUST yield events in the order received.
- The stream terminates after exactly one terminal event: `request.completed` or `request.error`.

Example SSE frame:

```text
event: request.started
data: {"request_id":"req_123","agent_id":"planner","session_id":"sess_123","status":"started"}
```

Optional waiting frame:

```text
event: request.waiting
data: {"request_id":"req_123","agent_id":"planner","session_id":"sess_123","status":"waiting","reason":"session_busy"}
```

### 6.3 Event Contract

H2A v1 defines this canonical event sequence:

- `request.accepted`
- optional `request.waiting`
- `request.started`
- zero or more `agent.trace`
- terminal `request.completed` or `request.error`

Rules:

- `request.accepted` MUST be the first event for a request.
- `request.waiting` MUST be non-terminal.
- `request.waiting` indicates that the request has been accepted but is blocked behind another in-flight request for the same `session_id`.
- `request.started` MUST be emitted before any `agent.trace` or terminal event.
- Exactly one terminal event MUST be emitted.

### 6.4 Client Contract

For one addressable agent:

- one client MUST target exactly one `agent_id`
- one client MUST use exactly one base URL for that agent server
- `post_request` and `stream_response` together form the complete asynchronous request contract

H2A v1 does not define:

- request cancellation
- request replay or resume
- idempotent request submission

## 7. Client To Agent HTTP Server

This section defines the third communication standard: `Client -> Agent`.

Each agent is exposed by one HTTP server that handles health checks, request creation, and request event streaming.

### 7.1 Endpoint Shape

The HTTP plus SSE binding defines these endpoints for one agent:

- `GET /health`
- `POST /agent/{agent_id}/request`
- `GET /agent/{agent_id}/request/{request_id}`

Equivalent route shapes are permitted if they preserve the same semantics.

### 7.2 POST Handling

For `POST /agent/{agent_id}/request`, the server MUST:

1. match the route for the target `agent_id`
2. read the request body as JSON
3. reject non-object JSON bodies
4. validate that `message` is a non-empty string
5. validate that `session_id`, if present, is a string
6. call the runtime request-submission operation
7. return `202 Accepted` with the accepted payload

Validation failures MUST return a JSON error object.

Example transport error:

```json
{
  "error": {
    "code": "INVALID_REQUEST",
    "message": "message is required"
  }
}
```

### 7.3 GET Handling

For `GET /agent/{agent_id}/request/{request_id}`, the server MUST:

1. validate the route and extract `request_id`
2. reject missing or unknown request ids
3. establish an SSE response with `Content-Type: text/event-stream`
4. fetch buffered request events from the runtime in order
5. write each event as one SSE frame
6. stop after the runtime reports completion or the client disconnects

If no new events are available and the request is not yet complete, the server MAY emit SSE keep-alive comments.

### 7.4 Runtime Requirements Behind The Server

The runtime attached to one agent HTTP server MUST provide:

- request submission
- request existence checks
- ordered event streaming for one request

The server/runtime boundary MUST preserve request ordering and terminal-event semantics.

The runtime execution model MAY vary internally, but the current H2A reference behavior is:

- every accepted request creates an execution task immediately
- requests sharing the same `session_id` are serialized
- different sessions MAY run concurrently up to an implementation-defined limit
- the runtime MAY emit `request.waiting` when same-session contention delays execution start

## 8. Per-Agent HTTP Runtime Binding

H2A is per-agent at the transport boundary.

### 8.1 One Server Per Agent

- Each addressable agent MUST be exposed through its own HTTP server instance or an equivalent per-agent endpoint surface.
- Each server instance MUST bind exactly one runtime and exactly one `agent_id`.
- A host with multiple agents MUST maintain one client per agent server.
- Separate processes or containers are not required by the protocol. An implementation MAY run per-agent servers in the same process.

### 8.2 Server Startup

When an agent runtime starts its HTTP server:

- it MUST bind a host and port
- it MUST associate the bound server with exactly one `agent_id`
- it MUST return a base URL that clients can use for subsequent `post_request` and `stream` calls

### 8.3 Binding Summary

The resulting interaction model is:

1. the host resolves `agent_id` to the associated client
2. the client calls `post_request`
3. the per-agent HTTP server accepts the request
4. the client calls `stream`
5. the per-agent HTTP server streams the lifecycle for that request

## 9. Error Model

Transport and validation errors MUST be returned as JSON objects of the form:

```json
{
  "error": {
    "code": "INVALID_REQUEST",
    "message": "message is required"
  }
}
```

Request execution failures after acceptance MUST be emitted as `request.error` events rather than converted into a different HTTP response.

Recommended HTTP status codes:

- `200 OK` for successful stream establishment
- `200 OK` for successful health checks
- `202 Accepted` for successful request submission
- `400 Bad Request` for validation failures
- `404 Not Found` for unknown routes or request ids
- `500 Internal Server Error` for unexpected transport failures

## 10. Related Protocols

This section is informative.

### 10.1 MCP

MCP is complementary to H2A. MCP focuses on tools, resources, prompts, and external context interoperability and includes explicit initialization and capability negotiation. H2A focuses on host-to-agent execution, lifecycle, and transport semantics.

### 10.2 A2A

Google A2A is the closest adjacent public protocol. It emphasizes agent-to-agent communication, task lifecycle, structured messages, artifacts, and long-running execution. H2A is narrower and host-to-agent first.

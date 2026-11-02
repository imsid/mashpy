# Engine

`src/mash/runtime/engine` runs an agent's requests durably in its process. It
starts one loop per accepted request, continues the loop after a crash or a
resume, and stays out of the way of two neighbours:

- the core loop in [`src/mash/core/agent.py`](../../core/agent.py), which owns
  what a step does
- the request event log in [`../events/`](../events), which owns what a client
  sees

A request's durable state is its row in `runtime_request` (status and
checkpoint) plus its events. The engine keeps nothing else, so a restarted
process loses nothing. An idle engine makes no queries.

## Files

- [`engine.py`](./engine.py): `RequestEngine`. Starts, recovers, cancels and
  continues requests, and stores interaction answers. One per `AgentRuntime`.
- [`loop.py`](./loop.py): `RequestRun`, one request's loop and its checkpoint.
- [`steps.py`](./steps.py): the steps the loop runs, and the events each one
  appends.

## Request lifecycle

1. [`../requests.py`](../requests.py) mints the request id and the trace id and
   appends `invoke_agent.start`, which records the request `running` in
   `runtime_request` and carries the message and request metadata.
2. It calls `engine.start_request(...)`, which starts a `RequestRun` as an
   asyncio task.
3. The loop runs the steps below, saving its checkpoint after each one.
4. It ends with `invoke_agent.end`: `ok`, `error`, or `cancelled` (appended by
   the cancel path).

## Checkpoint

`runtime_request.checkpoint` is one JSON object, overwritten after every step:

- `trace_id`: the request's trace, read from `invoke_agent.start`
- `state`: the loop state the steps thread through each other:
  - `context`: the serialized `Context`
  - `compaction`: compaction metadata from context loading
  - `loop_index`: the current agent step
  - `aggregate_usage`, `tool_usage`: token and tool usage so far
  - `action`: the model's planned action for this step, or `None` before planning
  - `result_payloads`: the tool results this step has so far
  - `interaction_response`: the answer to the action's interaction, once it has one
  - `signals`, `done`: set by the commit
- `memo`: small results inside the current step, by name: an interaction's id
  and deadline (`<prefix>.open.<loop>.<attempt>`), a browser handoff's link,
  the saved turn (`turn.persist`). Cleared when the step commits.

The loop reads the checkpoint to know where it is:

| Checkpoint | Next |
|---|---|
| none | `request.start`, then `context.load` |
| `action` is `None` | plan |
| an action, fewer results than tool calls | the remaining calls |
| results for every call | commit |
| `done` | finalize structured output, save the turn, complete |

A step that was running when the process died runs again. Its events are
deduped by key, so a rerun appends nothing twice.

## Steps

1. `start_request_trace`: reads the trace id from `invoke_agent.start` and
   appends `mash.context_load.start`.
2. `load_request_context`: loads history (compacting first if needed), builds
   the system prompt with the host snapshot and the Cortex profile, and
   appends `mash.context_load.end`. Its output is the first `state`, so a
   recovered request sees the context it first saw.
3. `plan_request_step`: appends `chat.start`, calls `Agent.plan_step(...)`,
   and appends `chat.end` with the model's turn and its usage under
   `attributes`. An action whose tools need approval gets an `interaction`.
4. Interaction, when the action has one: see below.
5. Tool calls, in order. A maximal run of consecutive parallel-safe calls runs
   concurrently in one `run_step_tool_batch`; any other call runs alone in
   `run_step_tool_call`. Each call appends `execute_tool.start` and
   `execute_tool.end`. Three tools get special handling:
   - `AskUser` becomes an interaction, and its answer the tool result.
   - `browser_handoff` asks the host for a live-view link (memoized), opens a
     `browser` interaction, and ends the handoff once it is answered.
   - `RunSubAgents` runs under the prefix `{request_id}-sub-{loop}-{call}`.
     The tool derives each child's id from it, so a rerun resubmits the same
     ids and gets back the children already running.
6. `commit_request_step`: calls `Agent.commit_step(...)`, which decides
   whether the request is done, and appends `mash.step.end`. It clears
   `action` and `result_payloads` and advances `loop_index`.
7. Once done: `finalize_structured_output` when a schema was requested and no
   tool set it, `persist_completed_turn` (the only write to conversation
   memory), and `complete_request`.

An exception from any step ends the request with `fail_request`, whose payload
is `classify_error(...)`: `error`, `error_type`, `error_code`, `retryable`.
Planning and tool steps first retry transient errors in process with
`retry_transient(...)` from [`../errors.py`](../errors.py).

## Interactions

An interaction's answer is its `mash.interaction.end` event, keyed
`interaction.ack.<id>`.

- The loop opens one in a memoized step that mints the id, appends
  `mash.interaction.start`, and records the deadline.
- It waits on an in-process event until the deadline, then reads the log.
- `engine.answer_interaction(...)`, behind the agent server's interaction
  route, appends the answer and wakes the loop. An answer to an interaction
  that already has one, a timeout or a cancel raises `InteractionClosedError`
  (`409`).
- At the deadline the loop appends the type's default answer with
  `timed_out`. An answer and a timeout that race write under the same key, so
  the first row wins and both sides read it.

After a crash the loop finds the interaction in `memo`, reads its answer from
the log if one arrived, and otherwise waits out the rest of the deadline.

## Cancel and resume

`engine.cancel_request(...)` acks an open interaction as cancelled, appends
the cancelled `invoke_agent.end`, and sets the run's cancel flag. The step
running at the time finishes and checkpoints; the loop stops before the next.

`resume_request(...)` in [`../requests.py`](../requests.py) accepts a
cancelled or failed request: it appends `mash.request.resumed`, which records
the request `running` and gives the new attempt terminal dedupe keys of its
own, then calls `engine.continue_request(...)`. The loop continues from the
checkpoint. If the request was cancelled while waiting on an interaction, it
opens a new one under the next attempt number.

## Recovery

`engine.open()` reads `runtime_request` for the agent's requests that are
`running` and continues each from its checkpoint. A request continued more
than `MAX_RECOVERIES` (10) times fails with `error_type: recovery_limit`, so a
request that crashes its process every time stops. `runtime_request.recoveries`
counts them.

`engine.close()` cancels the running loops and leaves their requests
`running`, so the next `open()` continues them.

## Events

Each event is named after the span it bounds (see [`../../otel/`](../../otel)).
A successful request:

1. `invoke_agent.start`
2. `mash.context_load.start`, `mash.context_load.end`
3. per step: `chat.start`, `chat.end`, any `mash.interaction.start` /
   `mash.interaction.end` pairs, any `execute_tool.start` / `execute_tool.end`
   pairs, `mash.step.end`
4. `invoke_agent.end` with `status: ok`

`chat.delta` rows sit between `chat.start` and `chat.end` when streaming is on.
The runtime store wakes a request's stream waiters in memory when it appends,
since the same process writes and serves the stream.

## Design rules

- Loop semantics change in [`src/mash/core/agent.py`](../../core/agent.py).
- What is checkpointed, and when, changes in [`loop.py`](./loop.py).
- Streaming and replay change in [`../requests.py`](../requests.py) and
  [`../events/`](../events).
- Conversation memory gets only the final turn.
- Every event a step appends has a dedupe key, so a step can run twice.

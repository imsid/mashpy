# Engine

`src/mash/runtime/engine` runs an agent's requests durably in its process. It
starts one loop per accepted request, continues the loop after a crash or a
resume, and stays out of the way of two neighbours:

- the core loop in [`src/mash/core/agent.py`](../../core/agent.py), which owns
  what a step does
- the request event log in [`../events/`](../events), which owns what a client
  sees

A request's durable state is its row in `runtime_request` (its status) plus
its events. The loop keeps its working state in memory and the event log is
its only record, so a restarted process loses nothing. An idle engine makes no
queries.

## Files

- [`engine.py`](./engine.py): `RequestEngine`. Starts, recovers, cancels and
  continues requests, and stores interaction answers. One per `AgentRuntime`.
- [`loop.py`](./loop.py): `RequestRun`, one request's loop.
- [`steps.py`](./steps.py): the steps the loop runs, and the events each one
  appends.
- [`rebuild.py`](./rebuild.py): a request's loop state, rebuilt from its
  events.

## Request lifecycle

1. [`../requests.py`](../requests.py) mints the request id and the trace id and
   appends `invoke_agent.start`, which records the request `running` in
   `runtime_request` and carries the message and request metadata.
2. It calls `engine.start_request(...)`, which starts a `RequestRun` as an
   asyncio task.
3. The loop runs the steps below; each appends its output as events.
4. It ends with `invoke_agent.end`: `ok`, `error`, or `cancelled` (appended by
   the cancel path).

## Loop state

The steps thread one dict through each other, held in memory:

- `context`: the serialized `Context`
- `compaction`: compaction metadata from context loading
- `loop_index`: the current agent step
- `aggregate_usage`, `tool_usage`: token and tool usage so far
- `action`: the model's planned action for this step, or `None` before planning
- `result_payloads`: the tool results this step has so far
- `interaction_response`: the answer to the action's interaction, once it has one
- `signals`, `done`: set by the commit

Every part of it is in the events, and `rebuild_request_state` puts it back
together for a recovered request:

| Event | What the rebuild takes from it |
|---|---|
| `mash.context_load.end` | the system prompt as rendered; for a request the companion routed, the board position read (`board.up_to`), otherwise the trace ids of the replayed turns and the compaction |
| `invoke_agent.start` | the host, app, policy and parent, from the request metadata |
| `chat.start`, `chat.end` | each step's assistant message, action and usage |
| `execute_tool.end` | each tool result, the loop's own (AskUser, browser handoffs, denied calls) included |
| `mash.step.end` | each committed step |

The rebuild uses the functions the running loop uses (`apply_plan`,
`commit_state`), so it arrives at the state the loop held. Work with no end
event runs again:

| Events | Next |
|---|---|
| no `mash.context_load.end` | `context.load` |
| no `chat.end` for the step | plan |
| a call with no `execute_tool.end` | that call; finished calls in its batch do not run again |
| results for every call, no `mash.step.end` | commit |
| `done` | finalize structured output, save the turn, complete |

The structured output finalizer is not recorded, so a recovered request runs
it again. Saving a turn twice stores it once. Every event has a dedupe key,
so a rerun appends nothing twice.

## Steps

1. `start_request_trace`: reads the trace id from `invoke_agent.start` and
   appends `mash.context_load.start`.
2. `load_request_context`: builds the system prompt with the caller's context
   and the Cortex profile, then loads the board (a request the companion
   routed) or the agent's own session history (compacting first if needed).
   It appends `mash.context_load.end` with the system prompt and either the
   board position read (`board.up_to`) or the trace ids of the replayed turns
   and the compaction, so a recovered request builds the context it first saw.
3. `plan_request_step`: appends `chat.start`, calls `Agent.plan_step(...)`,
   and appends `chat.end` with the model's turn and its usage under
   `attributes`. An action whose tools need approval gets an `interaction`.
4. Interaction, when the action has one: see below.
5. Tool calls, in order. A maximal run of consecutive parallel-safe calls runs
   concurrently in one `run_step_tool_batch`; any other call runs alone in
   `run_step_tool_call`. Each call appends `execute_tool.start` and
   `execute_tool.end`, and so does every result the loop makes itself.
   Three tools get special handling:
   - `AskUser` becomes an interaction, and its answer the tool result.
   - `browser_handoff` brings the request's tab to the front, opens a
     `browser` interaction, and ends the handoff once it is answered.
   - `RunSubAgents` runs under the prefix `{request_id}-sub-{loop}-{call}`.
     The tool derives each child's id from it, so a rerun resubmits the same
     ids and gets back the children already running.
6. `commit_request_step`: `commit_state` calls `Agent.commit_step(...)`, which
   decides whether the request is done; then it appends `mash.step.end`. It
   clears `action` and `result_payloads` and advances `loop_index`.
7. Once done: `finalize_structured_output` when a schema was requested and no
   tool set it, `persist_completed_turn` (the turn's write to conversation
   memory: a board entry for a routed request), and `complete_request`.

The companion's requests take another path through `RequestRun`: `route.py`
compacts the board when it is full, records the board position, classifies
the message with one structured output call (`chat.end` 0), and starts the
routed request, runs a workflow, answers `about` or asks the user to pick an
agent. Its events carry dedupe keys like the loop's (`context.loaded`,
`route.classify.completed`, `route.workflow_input.completed`), so a recovered
companion request reads its choice back.

An exception from any step ends the request with `fail_request`, whose payload
is `classify_error(...)`: `error`, `error_type`, `error_code`, `retryable`.
Before that, a request with no parent saves its user message as a failed turn
with `persist_failed_turn`: no reply, and the error in its metadata. Replay
shows it to the model in a `<failed_request>` block before the next user
message, so "try again" refers to it. A subagent's or workflow task's failure
saves no turn.
Planning and tool steps first retry transient errors in process with
`retry_transient(...)` from [`../errors.py`](../errors.py).

## Interactions

An interaction's answer is its `mash.interaction.end` event, keyed
`interaction.ack.<id>`.

- The loop opens one under an id derived from its place in the request and
  appends `mash.interaction.start` with its timeout: the type's timeout,
  capped by the request policy's `wait_max_seconds`. A browser handoff has no
  timeout of its own, so its timeout is the cap, and with no cap it has none.
  The deadline is the stored open time plus the stored timeout.
- It waits on an in-process event until the deadline, then reads the log.
- `engine.answer_interaction(...)`, behind the agent server's interaction
  route, checks the answer against the interaction's `schema`, appends it and
  wakes the loop. An answer the schema does not allow raises
  `InvalidInteractionResponseError` (`400`); an answer to an interaction that
  already has one, a timeout or a cancel raises `InteractionClosedError`
  (`409`). The tool phase runs approval-gated tools only on `"approve"`.
- At the deadline the loop appends the type's default answer with
  `timed_out`. An answer and a timeout that race write under the same key, so
  the first row wins and both sides read it.

After a crash the loop derives the same id, finds the interaction open, reads
its answer from the log if one arrived, and otherwise waits out the rest of
the same deadline.

## Cancel

`engine.cancel_request(...)` acks an open interaction as cancelled, appends
the cancelled `invoke_agent.end`, and sets the run's cancel flag. The step
running at the time finishes and appends its events; the loop stops before
the next.
A cancelled or failed request stays ended: nothing resumes or reruns it. The
host cancels a request when its event is cancelled.

## Recovery

`engine.open()` reads `runtime_request` for the agent's requests that are
`running` and continues each from its events. A request continued more
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
- What the events record, and how the state is rebuilt from them, changes in
  [`steps.py`](./steps.py) and [`rebuild.py`](./rebuild.py) together, with
  the rebuild tests.
- Streaming and replay change in [`../requests.py`](../requests.py) and
  [`../events/`](../events).
- Conversation memory gets only the final turn.
- Every event a step appends has a dedupe key, so a step can run twice.

/* Interactive agent loop walkthrough for docs/posts/agent-loop-interactive.md.
   Every step name, event type, dedupe key and default below is taken from
   src/mash/runtime/engine/workflow.py and src/mash/runtime/engine/steps.py.
   Payload values (ids, token counts, tool names) are illustrative. */
(function () {
  "use strict";

  var GH = "https://github.com/imsid/mashpy/blob/main/src/mash/";
  var AGENT = "assistant";
  var RID = "req-7f3a";
  var WFID = AGENT + ":" + RID;
  var TRACE = "tr-91c2";
  var ITR = "itr_4be19c0a7d21";

  function code(s) { return "<code>" + s + "</code>"; }
  function link(label, path) {
    return '<a href="' + GH + path + '">' + code(label) + "</a>";
  }

  var F_WORKFLOW = link("execute_request_workflow", "runtime/engine/workflow.py");
  var F_STEPS = "runtime/engine/steps.py";

  var RETRY =
    "retry_transient around the step: up to 3 retries for transient errors, " +
    "backoff from 1s capped at 30s, with jitter";

  function ev(type, dedupe, wire) {
    return { type: type, dedupe: dedupe, wire: wire || "agent.trace" };
  }

  /* ---------- frame builders ---------- */

  function submitFrame() {
    return {
      name: "submit_request",
      kind: "code",
      tag: "before DBOS",
      call: "AgentRuntime.submit_request(message, session_id)",
      fn: link("_submit_request_inner", "runtime/requests.py"),
      records: "Nothing yet. DBOS writes the workflow_status row as it starts the workflow.",
      body:
        "<p>The API route resolves the agent's " + code("InProcessAgentClient") +
        " from the pool and calls " + code("AgentRuntime.submit_request") +
        ". The runtime mints " + code("request_id") + ", appends " +
        code("runtime.request.accepted") + " (which also creates the " +
        code("runtime_request") + " row in state " + code("running") +
        "), and calls the engine.</p>" +
        "<p>" + code("DBOSRequestEngine.start_request") + " calls " +
        code("DBOS.start_workflow_async") + " under " +
        code('SetWorkflowID("' + WFID + '")') + ". DBOS inserts a " +
        code("workflow_status") + " row in state " + code("PENDING") +
        " with the workflow inputs and starts " + F_WORKFLOW +
        " as a task on the host's event loop. The caller gets " +
        code("request_id") + " back without waiting for the agent.</p>",
      facts: [
        ["Workflow", code("mash.runtime.execute_request")],
        ["Workflow id", code("{agent_id}:{request_id}") + " = " + code(WFID)],
        ["Inputs", code("agent_id, request_id, message, session_id, request_metadata")],
      ],
      events: [ev("runtime.request.accepted", "request.accepted", "request.accepted")],
      state: { workflow_id: '"' + WFID + '"' },
      map: ["user", "api", "pool", "runtime", "dbos", "pg-status", "pg-events"],
    };
  }

  function startFrame() {
    return {
      name: "request.start",
      kind: "step",
      tag: "run_step_async",
      call: 'DBOS.run_step_async({"name": "request.start"}, start_request_trace, ...)',
      fn: link("start_request_trace", F_STEPS),
      retry: "none",
      records: code('"' + TRACE + '"') + " (the trace_id)",
      body:
        "<p>The first checkpointed step. It mints " + code("trace_id") +
        " with " + code("uuid4()") + " and appends " +
        code("runtime.trace.started") + ", which the stream publishes as " +
        code("request.started") + ".</p>" +
        "<p>The id is generated inside a step, so a replayed workflow reads the " +
        "recorded value from " + code("operation_outputs") +
        " and every later event and the saved turn carry the same " +
        code("trace_id") + ".</p>",
      events: [ev("runtime.trace.started", "request.started", "request.started")],
      state: { trace_id: '"' + TRACE + '"' },
      map: ["dbos", "wf", "pg-ops", "pg-events"],
      crashBeforeWrites: true,
    };
  }

  function contextFrame() {
    return {
      name: "context.load",
      kind: "step",
      tag: "run_step_async",
      call: 'DBOS.run_step_async({"name": "context.load"}, load_request_context, ...)',
      fn: link("load_request_context", F_STEPS),
      retry: "none",
      records: "The starting " + code("workflow_state") + " dict",
      body:
        "<p>Builds the model context for the turn: the system prompt (resolved " +
        "from the host snapshot and any per-request " + code("context") +
        "), the session's replayable history from " + code("memory_turns") +
        " (" + code("conversation_history_turns") + ", default 3), and the new " +
        "user message. If " + code("compaction_token_threshold") +
        " is set and the session has crossed it, compaction runs here first.</p>" +
        "<p>The step returns the starting " + code("workflow_state") +
        ". From here on every step takes the previous state as an argument and " +
        "returns a new one, and DBOS records each version. The host snapshot is " +
        "part of this output, so a recovered request keeps the composition it " +
        "was submitted with.</p>",
      events: [ev("runtime.context.loaded", "context.loaded")],
      state: {
        loop_index: "0",
        context: '"system + 6 history msgs + user"',
        action: "null",
        result_payloads: "[]",
        done: "false",
        aggregate_usage: "{input: 0, output: 0}",
      },
      map: ["dbos", "wf", "pg-mem", "pg-ops", "pg-events"],
    };
  }

  function planFrame(i, o) {
    var body =
      "<p>Appends " + code("runtime.llm.think.started") +
      ", builds a turn agent, and calls " + code("Agent.plan_step") +
      ". That sends one " + code("LLMRequest") +
      " (system prompt, messages, tool definitions) to the provider.</p>";
    if (o.calls) {
      body +=
        "<p>The response carries " + o.calls.length + " tool call" +
        (o.calls.length > 1 ? "s" : "") + ", so the action type is " +
        code("tool_call") + ". For each call the step stores " +
        code("parallel_safe") + " from " + code("Agent._is_parallel_safe") +
        ": true unless the tool sets " + code("parallel_safe = False") +
        " or " + code("requires_approval = True") + ".</p>";
    } else {
      body +=
        "<p>The response is text with stop reason " + code("end_turn") +
        " and no tool calls, so " + code("_parse_response_to_action") +
        " returns a " + code("finish") + " action.</p>";
    }
    if (o.approval) {
      body +=
        "<p>" + code(o.approval) + " has " + code("requires_approval = True") +
        ", so the step attaches " +
        code('action.interaction = {type: "approval", timeout_seconds: 300}') +
        ". The workflow opens an interaction before it runs any call in this action.</p>";
    }
    if (o.extra) body += o.extra;
    return {
      name: "step.plan." + i,
      kind: "step",
      tag: "run_step_async",
      call:
        'retry_transient(lambda: DBOS.run_step_async({"name": "step.plan.' + i +
        '"}, plan_request_step, ...))',
      fn: link("plan_request_step", F_STEPS) + " → " +
        link("Agent.plan_step", "core/agent.py"),
      retry: RETRY,
      records: code("workflow_state") + " with " + code("action") + " set",
      body: body,
      events: [
        ev("runtime.llm.think.started", "llm.think.started." + i),
        ev("runtime.llm.think.completed", "llm.think." + i),
      ],
      state: {
        loop_index: String(i),
        action: o.action,
        result_payloads: "[]",
        aggregate_usage: o.usage,
      },
      map: ["dbos", "wf", "agent", "llm", "pg-ops", "pg-events"],
      rerunNote: "The provider call is made again.",
    };
  }

  function toolFrame(i, j, call, reason, results) {
    return {
      name: "tool.call." + i + "." + j,
      kind: "step",
      tag: "run_step_async",
      call:
        'retry_transient(lambda: DBOS.run_step_async({"name": "tool.call.' + i +
        "." + j + '"}, run_step_tool_call, ...))',
      fn: link("run_step_tool_call", F_STEPS),
      retry: RETRY,
      records: code("workflow_state") + " with the result appended to " +
        code("result_payloads"),
      body:
        "<p>" + reason + " It runs alone as its own step. " +
        code("run_step_tool_call") + " builds a fresh turn agent and calls " +
        code("execute_step_tool_call") + ", bracketed by " +
        code("runtime.tool.call.started") + " and " +
        code("runtime.tool.call.completed") + ".</p>" +
        "<p>Calls run in index order. A serial call is a barrier: nothing before " +
        "it and nothing after it runs at the same time.</p>",
      events: [
        ev("runtime.tool.call.started", "tool.call.started." + i + "." + call.id),
        ev("runtime.tool.call.completed", "tool.call." + i + "." + call.id),
      ],
      state: { result_payloads: results },
      map: ["dbos", "wf", "agent", "tools", "pg-ops", "pg-events"],
      rerunNote: "The tool body runs again.",
    };
  }

  function batchFrame(i, start, calls, results) {
    var names = calls.map(function (c) { return code(c.name); }).join(" and ");
    var events = [];
    calls.forEach(function (c) {
      events.push(ev("runtime.tool.call.started", "tool.call.started." + i + "." + c.id));
    });
    calls.forEach(function (c) {
      events.push(ev("runtime.tool.call.completed", "tool.call." + i + "." + c.id));
    });
    return {
      name: "tool.batch." + i + "." + start,
      kind: "step",
      tag: "run_step_async",
      call:
        'retry_transient(lambda: DBOS.run_step_async({"name": "tool.batch.' + i +
        "." + start + '"}, run_step_tool_batch, ...))',
      fn: link("run_step_tool_batch", F_STEPS),
      retry: RETRY,
      records: code("workflow_state") + " with all " + calls.length +
        " results appended in call order",
      body:
        "<p>" + names + " are consecutive parallel-safe calls, so the workflow " +
        "gathers them into one DBOS step. Inside it, " + code("_run_bounded") +
        " starts " + code("min(max_parallel_tools, " + calls.length + ")") +
        " workers (" + code("max_parallel_tools") + " defaults to 8) and each call " +
        "builds its own turn agent.</p>" +
        "<p>The batch is atomic for recovery: DBOS records one output for the " +
        "whole step, so either every result is recorded or the batch runs " +
        "again. A failing tool becomes an error result and does not stop the " +
        "other calls.</p>",
      events: events,
      state: { result_payloads: results },
      map: ["dbos", "wf", "agent", "tools", "pg-ops", "pg-events"],
      rerunNote:
        "Every call in the batch runs again, including calls that had finished " +
        "before the crash.",
    };
  }

  function commitFrame(i, done) {
    var body = done
      ? "<p>For a " + code("finish") + " action, " + code("Agent.commit_step") +
        " collects signals, marks the context complete, and returns " +
        code("done = true") + ". The workflow leaves the " + code("while True") +
        " loop.</p>"
      : "<p>" + code("Agent.commit_step") + " folds the step into context. For a " +
        code("tool_call") + " action, " + code("observe") +
        " appends the tool results as one tool message. " + code("done") +
        " stays false and " + code("loop_index") + " moves to " + (i + 1) +
        ", so the workflow goes back to planning.</p>" +
        "<p>When the agent needs another step and " +
        code("step_index + 1 >= max_steps") + " (default 30), " +
        code("commit_step") + " raises " + code("MaxStepsExhaustedError") +
        " and the request fails.</p>";
    var state = {
      loop_index: String(done ? i : i + 1),
      action: "null",
      result_payloads: "[]",
      done: done ? "true" : "false",
    };
    return {
      name: "step.commit." + i,
      kind: "step",
      tag: "run_step_async",
      call: 'DBOS.run_step_async({"name": "step.commit.' + i + '"}, commit_request_step, ...)',
      fn: link("commit_request_step", F_STEPS) + " → " +
        link("Agent.commit_step", "core/agent.py"),
      retry: "none",
      records: code("workflow_state") + " with the updated context and " + code("done"),
      body: body,
      events: [ev("runtime.step.completed", "step.completed." + i)],
      state: state,
      map: ["dbos", "wf", "agent", "pg-ops", "pg-events"],
    };
  }

  function persistFrame() {
    return {
      name: "turn.persist",
      kind: "step",
      tag: "run_step_async",
      call: 'DBOS.run_step_async({"name": "turn.persist"}, persist_completed_turn, ...)',
      fn: link("persist_completed_turn", F_STEPS),
      retry: "none",
      records: "The turn payload (response text, signals, token totals)",
      body:
        "<p>Writes the finished turn to " + code("memory_turns") +
        " through " + code("store.save_turn") + ", keyed by " + code("trace_id") +
        ". A normal request is saved with " + code("replayable = true") +
        ", so later requests in the session see it as history. Workflow task " +
        "and subagent turns are saved with " + code("replayable = false") + ".</p>" +
        "<p>Intermediate steps are never saved as turns. Only the completed " +
        "request is.</p>",
      events: [ev("runtime.turn.persisted", "turn.persisted")],
      state: {},
      map: ["dbos", "wf", "pg-mem", "pg-ops", "pg-events"],
      crashBeforeWrites: true,
    };
  }

  function completeFrame() {
    return {
      name: "request.complete",
      kind: "step",
      tag: "run_step_async",
      call: 'DBOS.run_step_async({"name": "request.complete"}, complete_request, ...)',
      fn: link("complete_request", F_STEPS),
      retry: "none",
      records: "None (the step returns nothing)",
      body:
        "<p>Appends " + code("runtime.request.completed") +
        " with lifecycle " + code("completed") + ". The store updates the " +
        code("runtime_request") + " row in the same transaction, and the stream " +
        "publishes " + code("request.completed") + " as the terminal event.</p>" +
        "<p>The dedupe key ends in the resume attempt number (" +
        code("request.completed.0") + " here). Then the workflow function " +
        "returns and DBOS sets the " + code("workflow_status") + " row to " +
        code("SUCCESS") + ".</p>",
      events: [ev("runtime.request.completed", "request.completed.0", "request.completed")],
      state: { done: "true" },
      map: ["dbos", "wf", "pg-status", "pg-events"],
    };
  }

  function openFrame(prefix, i, type, prompt, timeout) {
    return {
      name: prefix + ".open." + i + ".0",
      kind: "step",
      tag: "run_step_async",
      call:
        'DBOS.run_step_async({"name": "' + prefix + ".open." + i +
        '.0"}, open_interaction, ...)',
      fn: link("open_interaction", F_STEPS),
      retry: "none",
      records: code('"' + ITR + '"') + " (the interaction_id)",
      body:
        "<p>Mints " + code("interaction_id") + " inside the step and appends " +
        code("runtime.interaction.create") + ". The stream publishes it as " +
        code("request.interaction.create") + " with " + code('type: "' + type + '"') +
        ", the prompt " + code(prompt) + ", a response schema, and " +
        code("timeout_seconds: " + timeout) + ".</p>" +
        "<p>The trailing " + code(".0") + " is the attempt number. Minting the id " +
        "inside a step means a replay hands the same id to both the caller and " +
        "the " + code("recv") + " that follows.</p>",
      events: [
        ev("runtime.interaction.create", "interaction.create." + ITR, "request.interaction.create"),
      ],
      state: {},
      map: ["dbos", "wf", "pg-ops", "pg-events"],
    };
  }

  function recvFrame(type, timeout, response) {
    var dflt = type === "approval" ? code('"deny"') : type === "choice" ? code("[]") : code('""');
    return {
      name: "DBOS.recv_async",
      kind: "recv",
      tag: "durable wait",
      call: 'DBOS.recv_async("' + ITR + '", timeout_seconds=' + timeout + ")",
      fn: link("_run_interaction", "runtime/engine/workflow.py"),
      retry: "none",
      records: "The received message (DBOS records recv like a step)",
      body:
        "<p>The workflow waits here for a message on topic " + code(ITR) +
        ". No step is open while it waits.</p>" +
        "<p>The reply comes in as " +
        code("POST /api/v1/agent/" + AGENT + "/request/" + RID + "/interaction") +
        " with " + code("{interaction_id, response}") + ". " +
        code("InProcessAgentClient.post_interaction") + " calls " +
        code('DBOS.send_async("' + WFID + '", response, topic=interaction_id)') +
        ". DBOS stores the message in " + code("notifications") +
        " and " + code("recv_async") + " returns it: " + code(response) + ".</p>" +
        "<p>If " + timeout + " seconds pass first, " + code("recv_async") +
        " returns " + code("None") + " and the workflow uses the default for " +
        code(type) + ", which is " + dflt + ".</p>",
      events: [],
      state: { interaction_response: response },
      map: ["user", "api", "pool", "dbos", "wf", "pg-notif", "pg-ops"],
    };
  }

  function ackFrame(prefix, i, response, extra, state) {
    return {
      name: prefix + ".ack." + i + ".0",
      kind: "step",
      tag: "run_step_async",
      call:
        'DBOS.run_step_async({"name": "' + prefix + ".ack." + i +
        '.0"}, emit_interaction_ack, ...)',
      fn: link("emit_interaction_ack", F_STEPS),
      retry: "none",
      records: "None",
      body:
        "<p>Appends " + code("runtime.interaction.ack") +
        " carrying the response " + code(response) + ". The stream publishes " +
        code("request.interaction.ack") + " so the caller can clear the prompt.</p>" +
        (extra || ""),
      events: [
        ev("runtime.interaction.ack", "interaction.ack." + ITR, "request.interaction.ack"),
      ],
      state: state || {},
      map: ["dbos", "wf", "pg-ops", "pg-events"],
    };
  }

  function denyFrame() {
    return {
      name: "denied results",
      kind: "code",
      tag: "workflow code",
      call: "approval_denied = True",
      fn: F_WORKFLOW,
      retry: "none",
      records: "Nothing. Plain workflow code recomputes the same value on replay.",
      body:
        "<p>The response was " + code('"deny"') + ", so the workflow skips the " +
        "tools. For every call in the action it builds an error result in " +
        "workflow code, with no DBOS step: content " +
        code('"Tool execution denied by user (deny)"') + ", " +
        code("is_error: true") + ", " + code("metadata: {denied: true}") + ".</p>" +
        "<p>The denial covers the whole action. The model reads the error " +
        "results on the next plan step and decides what to do.</p>",
      events: [],
      state: { result_payloads: "[deploy: error, denied]" },
      map: ["wf"],
    };
  }

  function subagentFrame() {
    var child = [
      "request.start",
      "context.load",
      "step.plan.0",
      "tool.batch.0.0",
      "step.commit.0",
      "step.plan.1",
      "step.commit.1",
      "turn.persist",
      "request.complete",
    ];
    return {
      name: "InvokeSubagent",
      kind: "wf",
      tag: "workflow scope",
      call:
        'with bound_subagent_request_id("' + RID + '-sub-0-0"): run_step_tool_call(...)',
      fn: link("_run_tool_call_for_workflow", "runtime/engine/workflow.py"),
      retry: "none",
      records:
        "No step output for this call. The child workflow has its own checkpoints.",
      body:
        "<p>This call stays at workflow scope with no " + code("run_step_async") +
        " wrapper, because DBOS only starts child workflows from workflow " +
        "context, not from inside a step.</p>" +
        "<p>The workflow binds a child request id built from the parent id, the " +
        "loop index, and the call index: " + code(RID + "-sub-0-0") + ". The " +
        code("InvokeSubagent") + " tool submits to the " + code("research") +
        " runtime, whose engine calls " + code("DBOS.start_workflow_async") +
        ". That starts a child workflow with id " +
        code("research:" + RID + "-sub-0-0") + ", running the same loop with its own steps:</p>" +
        '<ol class="mv-nested">' +
        child.map(function (n) { return "<li>" + code(n) + "</li>"; }).join("") +
        "</ol>" +
        "<p>The child runs in the parent's session, and its turn is saved with " +
        code("replayable = false") + ". The parent's tool streams the child's " +
        "events until a terminal event, and the child's answer becomes the tool " +
        "result. The parent appends " + code("runtime.subagent.call.completed") +
        " in place of " + code("runtime.tool.call.completed") + ".</p>",
      events: [
        ev("runtime.tool.call.started", "tool.call.started.0.call_s1"),
        ev("runtime.subagent.call.completed", "tool.call.0.call_s1"),
      ],
      state: { result_payloads: "[InvokeSubagent: ok, research answer]" },
      map: ["dbos", "wf", "agent", "tools", "pg-status", "pg-ops", "pg-events"],
    };
  }

  /* ---------- scenarios ---------- */

  var USAGE0 = "{input: 2140, output: 96}";
  var USAGE01 = "{input: 4650, output: 284}";

  var SCENARIOS = {
    direct: {
      label: "Direct answer",
      blurb: "The model answers in one plan step. No tools.",
      build: function () {
        return [
          submitFrame(),
          startFrame(),
          contextFrame(),
          planFrame(0, {
            action: '{type: "finish", assistant_text: "..."}',
            usage: "{input: 2140, output: 188}",
          }),
          commitFrame(0, true),
          persistFrame(),
          completeFrame(),
        ];
      },
    },
    tools: {
      label: "Parallel tools",
      blurb:
        "Two parallel-safe calls run as one batch step, then a serial call runs alone.",
      build: function () {
        var a = { id: "call_a1", name: "search_docs" };
        var b = { id: "call_b2", name: "read_file" };
        var c = { id: "call_c3", name: "write_note" };
        return [
          submitFrame(),
          startFrame(),
          contextFrame(),
          planFrame(0, {
            calls: [a, b, c],
            action:
              '{type: "tool_call", tool_calls: [search_docs (parallel_safe), ' +
              "read_file (parallel_safe), write_note (parallel_safe: false)]}",
            usage: USAGE0,
          }),
          batchFrame(0, 0, [a, b], "[search_docs: ok, read_file: ok]"),
          toolFrame(
            0,
            2,
            c,
            code("write_note") + " sets " + code("parallel_safe = False") + ".",
            "[search_docs: ok, read_file: ok, write_note: ok]"
          ),
          commitFrame(0, false),
          planFrame(1, {
            action: '{type: "finish", assistant_text: "..."}',
            usage: USAGE01,
          }),
          commitFrame(1, true),
          persistFrame(),
          completeFrame(),
        ];
      },
    },
    approval: {
      label: "Approval",
      blurb:
        "A tool with requires_approval pauses the request until the caller answers.",
      options: ["approve", "deny"],
      build: function (opt) {
        var d = { id: "call_d4", name: "deploy" };
        var frames = [
          submitFrame(),
          startFrame(),
          contextFrame(),
          planFrame(0, {
            calls: [d],
            approval: "deploy",
            action:
              '{type: "tool_call", tool_calls: [deploy (parallel_safe: false)], ' +
              'interaction: {type: "approval", timeout_seconds: 300}}',
            usage: USAGE0,
          }),
          openFrame("interaction", 0, "approval", '"Approve execution of: deploy?"', 300),
          recvFrame("approval", 300, '"' + opt + '"'),
          ackFrame("interaction", 0, '"' + opt + '"'),
        ];
        if (opt === "approve") {
          frames.push(
            toolFrame(
              0,
              0,
              d,
              "The caller approved. Approval-gated tools are never parallel-safe.",
              "[deploy: ok]"
            )
          );
        } else {
          frames.push(denyFrame());
        }
        frames.push(
          commitFrame(0, false),
          planFrame(1, {
            action: '{type: "finish", assistant_text: "..."}',
            usage: USAGE01,
          }),
          commitFrame(1, true),
          persistFrame(),
          completeFrame()
        );
        return frames;
      },
    },
    askuser: {
      label: "AskUser",
      blurb:
        "The model calls AskUser. The workflow turns it into an interaction instead of running the tool.",
      build: function () {
        var answer = '["staging"]';
        return [
          submitFrame(),
          startFrame(),
          contextFrame(),
          planFrame(0, {
            calls: [{ id: "call_q5", name: "AskUser" }],
            action:
              '{type: "tool_call", tool_calls: [AskUser {question: "Which ' +
              'environment?", options: ["staging", "prod"]} (parallel_safe: false)]}',
            usage: USAGE0,
            extra:
              "<p>" + code("AskUser") + " sets " + code("parallel_safe = False") +
              ", so it always runs as a serial call.</p>",
          }),
          openFrame("ask_user", 0, "choice", '"Which environment?"', 3600),
          recvFrame("choice", 3600, answer),
          ackFrame(
            "ask_user",
            0,
            answer,
            "<p>The workflow then builds the tool result itself: " +
              code('{tool_name: "AskUser", content: "staging"}') + ". " +
              code("AskUserTool.execute") + " never runs. " +
              code("_run_tool_call_for_workflow") +
              " intercepts the call by name before any tool step. Options make " +
              "the interaction a " + code("choice") + "; without options it is " +
              code("info") + ". The timeout comes from " +
              code("ASK_USER_DEFAULT_TIMEOUT_SECONDS") + " (3600).</p>",
            { result_payloads: '[AskUser: "staging"]' }
          ),
          commitFrame(0, false),
          planFrame(1, {
            action: '{type: "finish", assistant_text: "..."}',
            usage: USAGE01,
          }),
          commitFrame(1, true),
          persistFrame(),
          completeFrame(),
        ];
      },
    },
    subagent: {
      label: "Subagent",
      blurb:
        "The primary delegates to a subagent, which runs as a child DBOS workflow.",
      build: function () {
        return [
          submitFrame(),
          startFrame(),
          contextFrame(),
          planFrame(0, {
            calls: [{ id: "call_s1", name: "InvokeSubagent" }],
            action:
              '{type: "tool_call", tool_calls: [InvokeSubagent {agent_id: ' +
              '"research", ...} (parallel_safe: false)]}',
            usage: USAGE0,
          }),
          subagentFrame(),
          commitFrame(0, false),
          planFrame(1, {
            action: '{type: "finish", assistant_text: "..."}',
            usage: USAGE01,
          }),
          commitFrame(1, true),
          persistFrame(),
          completeFrame(),
        ];
      },
    },
  };

  /* ---------- crash and recovery ---------- */

  function canCrash(frame) {
    return !frame.synthetic && (frame.kind === "step" || frame.kind === "recv" || frame.kind === "wf");
  }

  function withCrash(frames, k) {
    var f = frames[k];
    var before = frames.slice(0, k);
    var out = before.slice();

    var crashBody;
    var crashEvents = [];
    if (f.kind === "recv") {
      crashBody =
        "<p>The host process dies while the workflow is parked on " +
        code("recv") + ". The interaction prompt is already in " +
        code("runtime_event_log") + " and the " + code("workflow_status") +
        " row stays " + code("PENDING") + ".</p>";
    } else if (f.kind === "wf") {
      crashBody =
        "<p>The host process dies while the child workflow is running. Parent " +
        "and child are separate " + code("PENDING") + " rows in " +
        code("workflow_status") + ", each with the checkpoints it had recorded.</p>";
      crashEvents = f.events.slice(0, 1);
    } else if (f.crashBeforeWrites) {
      crashBody =
        "<p>The host process dies while this step is running, before its " +
        "writes commit. DBOS has no recorded output for it, and the " +
        code("workflow_status") + " row stays " + code("PENDING") + ".</p>";
    } else {
      crashBody =
        "<p>The host process dies while this step is running. Its events " +
        "already reached " + code("runtime_event_log") + ", but DBOS never " +
        "recorded the step's output in " + code("operation_outputs") +
        ". The " + code("workflow_status") + " row stays " + code("PENDING") + ".</p>";
      crashEvents = f.events.slice();
    }
    crashBody +=
      "<p>The SSE stream stops. Everything already in the event log stays " +
      "readable, and a client that opens the events route again gets the log " +
      "replayed from the first event.</p>";

    out.push({
      name: f.name,
      kind: "crash",
      tag: "process killed",
      synthetic: true,
      call: f.call,
      fn: f.fn,
      retry: f.retry,
      records: "Nothing. The step did not finish.",
      body: crashBody,
      events: crashEvents,
      state: {},
      map: ["wf"],
    });

    out.push({
      name: "DBOS.launch()",
      kind: "replay",
      tag: "process restart",
      synthetic: true,
      call: "ensure_dbos_ready(MASH_DATABASE_URL)",
      fn: link("ensure_dbos_ready", "runtime/engine/dbos.py"),
      retry: "none",
      records: "Nothing new",
      body:
        "<p>The host starts again. Opening the runtime calls " +
        code("DBOSRequestEngine.open()") + ", which calls " +
        code("ensure_dbos_ready()") + ". That constructs DBOS with " +
        code('{"name": "mash", "system_database_url": MASH_DATABASE_URL}') +
        ", registers " + code("mash.runtime.execute_request") + ", and calls " +
        code("DBOS.launch()") + ". Registration happens before launch so " +
        "recovery can resolve the workflow function.</p>" +
        "<p>On launch DBOS finds the " + code("PENDING") + " rows in " +
        code("workflow_status") + " and calls each workflow function again with " +
        "its recorded inputs. Here that is " +
        code('execute_request_workflow("' + AGENT + '", "' + RID + '", ...)') + ".</p>",
      events: [],
      state: {},
      map: ["runtime", "dbos", "pg-status"],
    });

    var replayed = before.filter(function (b) { return b.kind !== "code" || b.name !== "submit_request"; });
    var items = replayed.map(function (b) {
      if (b.kind === "wf") {
        return "<li>" + code(b.name) + ": runs again at workflow scope under the " +
          "same child id and attaches to the existing child workflow</li>";
      }
      if (b.kind === "code") {
        return "<li>" + code(b.name) + ": plain workflow code, recomputed from replayed values</li>";
      }
      return "<li>" + code(b.name) + "</li>";
    });
    out.push({
      name: "replay",
      kind: "replay",
      tag: "from operation_outputs",
      synthetic: true,
      call: "execute_request_workflow(...) from the first line",
      fn: F_WORKFLOW,
      retry: "none",
      records: "Nothing new",
      body:
        "<p>The workflow function runs from the top. Each " +
        code("run_step_async") + " and " + code("recv_async") +
        " that already has a recorded output returns it without running its " +
        "function, so these make no LLM call, run no tool, and append no event:</p>" +
        "<ul>" + items.join("") + "</ul>" +
        "<p>Loop control between steps is plain Python over the replayed " +
        code("workflow_state") + ", so the workflow reaches " + code(f.name) +
        " again with the same arguments.</p>",
      events: [],
      state: {},
      map: ["dbos", "wf", "pg-ops"],
    });

    var note;
    var rerunEvents = f.events.map(function (e) {
      var copy = { type: e.type, dedupe: e.dedupe, wire: e.wire };
      if (crashEvents.some(function (c) { return c.dedupe === e.dedupe; })) copy.deduped = true;
      return copy;
    });
    if (f.kind === "recv") {
      note =
        "The open step replayed and returned the same " + code("interaction_id") +
        ", so no second " + code("request.interaction.create") + " is emitted. " +
        code("recv_async") + " waits on the same topic. A reply sent while the " +
        "process was down is already in " + code("notifications") +
        " and is delivered as soon as the wait starts.";
    } else if (f.kind === "wf") {
      note =
        "The call runs again with the same child id " + code(RID + "-sub-0-0") +
        ". Starting a workflow under an existing id attaches to it, so the " +
        "child continues from its own checkpoints and no second child starts. " +
        "The repeated " + code("tool.call.started") + " append returns the existing row.";
    } else if (f.crashBeforeWrites) {
      note = "No recorded output, so DBOS runs this step again from the start.";
    } else {
      note =
        "No recorded output, so DBOS runs this step again. Each event append " +
        "matches an existing row on " + code("(request_id, dedupe_key)") +
        " and returns it, so the log gets no duplicates." +
        (f.rerunNote ? " " + f.rerunNote : "");
    }
    var rerun = {};
    Object.keys(f).forEach(function (key) { rerun[key] = f[key]; });
    rerun.synthetic = true;
    rerun.tag = f.tag + ", re-run";
    rerun.events = rerunEvents;
    rerun.body = '<div class="mv-note is-replay">' + note + "</div>" + f.body;
    out.push(rerun);

    return out.concat(frames.slice(k + 1));
  }

  /* ---------- rendering ---------- */

  var STATE_ORDER = [
    "workflow_id",
    "trace_id",
    "loop_index",
    "context",
    "action",
    "result_payloads",
    "interaction_response",
    "done",
    "aggregate_usage",
  ];

  var KIND_LABEL = {
    step: "DBOS step",
    recv: "DBOS recv",
    wf: "workflow scope",
    code: "plain code",
    replay: "recovery",
    crash: "crash",
  };

  function mapHTML() {
    function node(id, label, sub) {
      return '<div class="mv-node" data-node="' + id + '">' + label +
        (sub ? "<small>" + sub + "</small>" : "") + "</div>";
    }
    var arrow = '<span class="mv-arrow" aria-hidden="true">→</span>';
    return (
      '<div class="mv-map" aria-label="Where each part runs">' +
      '<div class="mv-zone"><div class="mv-zone-title">Host process ' +
      code("mash host serve") + "</div>" +
      '<div class="mv-row">' +
      node("user", "Caller", "HTTP") + arrow +
      node("api", "API routes", code("/api/v1")) + arrow +
      node("pool", "Pool", code("InProcessAgentClient")) + arrow +
      node("runtime", "AgentRuntime", code("DBOSRequestEngine")) +
      "</div>" +
      '<div class="mv-dbos-box"><div class="mv-zone-title">DBOS library, launched in this process</div>' +
      '<div class="mv-row">' +
      node("dbos", "DBOS executor", "checkpoints, recovery") + arrow +
      node("wf", "Request workflow", code("execute_request_workflow")) + arrow +
      node("agent", "Turn agent", code("core/agent.py")) + arrow +
      node("tools", "Tools", code("ToolRegistry")) +
      "</div></div></div>" +
      '<div class="mv-zone"><div class="mv-zone-title">Postgres ' +
      code("MASH_DATABASE_URL") + "</div>" +
      '<div class="mv-tables">' +
      '<div class="mv-row"><span class="mv-arrow">DBOS</span>' +
      node("pg-status", code("workflow_status")) +
      node("pg-ops", code("operation_outputs")) +
      node("pg-notif", code("notifications")) + "</div>" +
      '<div class="mv-row"><span class="mv-arrow">Mash</span>' +
      node("pg-events", code("runtime_event_log"), code("runtime_request")) +
      node("pg-mem", code("memory_turns")) + "</div>" +
      '<div class="mv-row"><span class="mv-arrow">Outside</span>' +
      node("llm", "LLM provider API") + "</div>" +
      "</div></div></div>"
    );
  }

  function init(root) {
    if (root.dataset.mvReady) return;
    root.dataset.mvReady = "1";
    root.classList.add("mviz");

    var keys = Object.keys(SCENARIOS);
    var current = { scenario: "tools", option: null, index: 0, crashAt: null };
    var frames = [];
    var timer = null;

    root.innerHTML =
      '<div class="mv-toolbar">' +
      '<div class="mv-tabs" role="tablist" aria-label="Scenario">' +
      keys.map(function (k) {
        return '<button type="button" role="tab" data-scenario="' + k + '">' +
          SCENARIOS[k].label + "</button>";
      }).join("") +
      "</div>" +
      '<div class="mv-controls">' +
      '<button type="button" data-act="reset">Reset</button>' +
      '<button type="button" data-act="prev" aria-label="Previous step">← Prev</button>' +
      '<span class="mv-counter" aria-live="polite"></span>' +
      '<button type="button" data-act="next" aria-label="Next step">Next →</button>' +
      '<button type="button" data-act="play">Play</button>' +
      '<button type="button" class="mv-crash-btn" data-act="crash">Crash during this step</button>' +
      "</div></div>" +
      '<div class="mv-subtoggle" hidden></div>' +
      '<p class="mv-blurb"></p>' +
      '<div class="mv-legend">' +
      ["step", "recv", "wf", "code", "crash", "replay"].map(function (k) {
        return '<span><span class="mv-badge mv-kind-' + k + '">' + KIND_LABEL[k] + "</span></span>";
      }).join("") +
      "</div>" +
      mapHTML() +
      '<div class="mv-main">' +
      '<div class="mv-panel"><div class="mv-panel-head"><span>Workflow ' +
      code(WFID) + '</span></div><div class="mv-panel-body"><ol class="mv-timeline"></ol></div></div>' +
      '<div class="mv-panel mv-detail"><div class="mv-panel-body"></div></div>' +
      '<div class="mv-side">' +
      '<div class="mv-panel"><div class="mv-panel-head"><span>Workflow variables</span><span>recorded with each step</span></div>' +
      '<div class="mv-panel-body"><ul class="mv-state"></ul></div></div>' +
      '<div class="mv-panel"><div class="mv-panel-head"><span>runtime_event_log</span><span>→ wire event</span></div>' +
      '<div class="mv-panel-body"><ol class="mv-events"></ol></div></div>' +
      "</div></div>";

    var el = {
      tabs: root.querySelectorAll("[data-scenario]"),
      sub: root.querySelector(".mv-subtoggle"),
      blurb: root.querySelector(".mv-blurb"),
      counter: root.querySelector(".mv-counter"),
      timeline: root.querySelector(".mv-timeline"),
      detail: root.querySelector(".mv-detail .mv-panel-body"),
      state: root.querySelector(".mv-state"),
      events: root.querySelector(".mv-events"),
      prev: root.querySelector('[data-act="prev"]'),
      next: root.querySelector('[data-act="next"]'),
      play: root.querySelector('[data-act="play"]'),
      crash: root.querySelector('[data-act="crash"]'),
      nodes: root.querySelectorAll("[data-node]"),
    };

    function rebuild() {
      var sc = SCENARIOS[current.scenario];
      if (sc.options && !current.option) current.option = sc.options[0];
      frames = sc.build(current.option);
      if (current.crashAt !== null) frames = withCrash(frames, current.crashAt);
    }

    function stop() {
      if (timer) { clearInterval(timer); timer = null; }
      el.play.textContent = "Play";
    }

    function stateAt(idx) {
      var s = {};
      var changed = {};
      for (var i = 0; i <= idx; i++) {
        var patch = frames[i].state || {};
        Object.keys(patch).forEach(function (k) {
          if (s[k] !== patch[k]) {
            s[k] = patch[k];
            if (i === idx) changed[k] = true;
          }
        });
      }
      return { values: s, changed: changed };
    }

    function eventsAt(idx) {
      // A deduped append returns the existing row, so it marks that row
      // instead of adding one.
      var rows = [];
      var bySeq = {};
      for (var i = 0; i <= idx; i++) {
        (frames[i].events || []).forEach(function (e) {
          if (e.deduped && bySeq[e.dedupe]) {
            bySeq[e.dedupe].hitFrame = i;
          } else {
            var row = { seq: rows.length + 1, e: e, frame: i };
            bySeq[e.dedupe] = row;
            rows.push(row);
          }
        });
      }
      return rows;
    }

    function render() {
      var sc = SCENARIOS[current.scenario];
      var f = frames[current.index];

      el.tabs.forEach(function (t) {
        t.setAttribute("aria-selected", t.dataset.scenario === current.scenario ? "true" : "false");
      });

      if (sc.options) {
        el.sub.hidden = false;
        el.sub.innerHTML =
          "<span>Caller responds:</span>" +
          sc.options.map(function (o) {
            return '<button type="button" data-option="' + o + '" aria-pressed="' +
              (o === current.option) + '">' + o + "</button>";
          }).join("");
      } else {
        el.sub.hidden = true;
        el.sub.innerHTML = "";
      }

      el.blurb.textContent = sc.blurb +
        (current.crashAt !== null ? " A crash was injected; Reset clears it." : "");

      el.counter.textContent = current.index + 1 + " / " + frames.length;
      el.prev.disabled = current.index === 0;
      el.next.disabled = current.index === frames.length - 1;
      el.crash.disabled = current.crashAt !== null || !canCrash(f);

      el.timeline.innerHTML = frames.map(function (fr, i) {
        var cls = "mv-kind-" + fr.kind + (i > current.index ? " is-future" : "");
        return '<li><button type="button" data-index="' + i + '" class="' + cls + '"' +
          (i === current.index ? ' aria-current="step"' : "") + ">" +
          '<span class="mv-tl-name">' + fr.name + "</span>" +
          '<span class="mv-tl-tag">' + fr.tag + "</span></button></li>";
      }).join("");
      var active = el.timeline.querySelector('[aria-current="step"]');
      if (active && active.scrollIntoView) {
        var box = el.timeline;
        var top = active.offsetTop - box.offsetTop;
        if (top < box.scrollTop || top > box.scrollTop + box.clientHeight - 30) {
          box.scrollTop = Math.max(0, top - 40);
        }
      }

      var facts = [["DBOS call", '<span class="mv-mono">' + f.call + "</span>"], ["Runs", f.fn]];
      if (f.retry) facts.push(["Retry", f.retry]);
      facts.push(["Recorded", f.records]);
      (f.facts || []).forEach(function (x) { facts.push(x); });
      el.detail.innerHTML =
        '<span class="mv-badge mv-kind-' + f.kind + '">' + KIND_LABEL[f.kind] + "</span>" +
        "<h4>" + f.name + "</h4>" +
        '<dl class="mv-facts">' +
        facts.map(function (x) { return "<dt>" + x[0] + "</dt><dd>" + x[1] + "</dd>"; }).join("") +
        "</dl>" + f.body;

      var st = stateAt(current.index);
      var stKeys = STATE_ORDER.filter(function (k) { return k in st.values; });
      el.state.innerHTML = stKeys.length
        ? stKeys.map(function (k) {
            return '<li class="' + (st.changed[k] ? "is-changed" : "") + '"><span class="mv-k">' +
              k + '</span><span class="mv-v">' + st.values[k] + "</span></li>";
          }).join("")
        : '<li class="mv-empty">Nothing yet</li>';

      var rows = eventsAt(current.index);
      el.events.innerHTML = rows.length
        ? rows.map(function (r) {
            var meta = "dedupe " + r.e.dedupe + ' · <span class="mv-ev-wire">' + r.e.wire + "</span>";
            var hit = r.hitFrame !== undefined;
            if (hit) meta += " · appended again on re-run, existing row returned";
            var cls = r.frame === current.index || r.hitFrame === current.index ? "is-new" : "";
            return '<li class="' + cls + '"><span class="mv-seq">' + r.seq + "</span><span>" +
              '<span class="mv-ev-type">' + r.e.type + "</span><br>" +
              '<span class="mv-ev-meta">' + meta + "</span></span></li>";
          }).join("")
        : '<li class="mv-empty">No events yet</li>';
      el.events.scrollTop = el.events.scrollHeight;

      var activeNodes = f.map || [];
      el.nodes.forEach(function (n) {
        n.classList.toggle("is-active", activeNodes.indexOf(n.dataset.node) !== -1);
      });
    }

    function go(i) {
      current.index = Math.max(0, Math.min(frames.length - 1, i));
      render();
    }

    root.addEventListener("click", function (e) {
      var t = e.target.closest("button");
      if (!t || !root.contains(t)) return;
      if (t.dataset.scenario) {
        stop();
        current.scenario = t.dataset.scenario;
        current.option = null;
        current.crashAt = null;
        rebuild();
        go(0);
      } else if (t.dataset.option) {
        stop();
        current.option = t.dataset.option;
        current.crashAt = null;
        var keep = current.index;
        rebuild();
        go(keep);
      } else if (t.dataset.index) {
        stop();
        go(parseInt(t.dataset.index, 10));
      } else if (t.dataset.act === "prev") {
        stop();
        go(current.index - 1);
      } else if (t.dataset.act === "next") {
        stop();
        go(current.index + 1);
      } else if (t.dataset.act === "reset") {
        stop();
        current.crashAt = null;
        rebuild();
        go(0);
      } else if (t.dataset.act === "crash") {
        stop();
        current.crashAt = current.index;
        rebuild();
        go(current.index);
      } else if (t.dataset.act === "play") {
        if (timer) { stop(); return; }
        if (current.index === frames.length - 1) go(0);
        el.play.textContent = "Pause";
        timer = setInterval(function () {
          if (current.index >= frames.length - 1) { stop(); return; }
          go(current.index + 1);
        }, 1800);
      }
    });

    root.addEventListener("keydown", function (e) {
      if (e.target.closest("input, textarea")) return;
      if (e.key === "ArrowRight") { stop(); go(current.index + 1); e.preventDefault(); }
      if (e.key === "ArrowLeft") { stop(); go(current.index - 1); e.preventDefault(); }
    });

    rebuild();
    render();
  }

  function boot() {
    var roots = document.querySelectorAll('[data-mash-viz="agent-loop"]');
    for (var i = 0; i < roots.length; i++) init(roots[i]);
  }

  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(boot);
  } else if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
